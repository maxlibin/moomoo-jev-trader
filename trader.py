"""Pure trade logic shared by the live executor and the backtest: sizing, entry plans, exits.

Long only. An entry is a limit order slightly above the signal candle's close,
sized so a stop-out loses ``risk_fraction`` of the account, capped by
``max_position_fraction`` of the account and by cash. Exits happen on the live
price crossing the stop or target, or at the session cutoff.
"""

import math
from dataclasses import dataclass
from datetime import time
from typing import Mapping, Optional

import pandas as pd

from settings import count, fraction, non_negative, overrides_from_env, positive
from signals import BUY_SIGNALS, Setup


@dataclass(frozen=True)
class RiskLimits:
    risk_fraction: float
    max_position_fraction: float
    max_trades_per_day: int
    max_daily_loss_fraction: float
    last_entry: time
    cutoff: time
    entry_buffer_fraction: float
    entry_timeout_seconds: int
    fee_per_order: float
    min_gain_to_fee_ratio: float
    sizing_equity_cap: float
    max_quote_age_seconds: float


DEFAULT_LIMITS = RiskLimits(
    risk_fraction=0.01,
    max_position_fraction=0.20,
    max_trades_per_day=6,
    max_daily_loss_fraction=0.02,
    last_entry=time(15, 45),
    cutoff=time(15, 50),
    entry_buffer_fraction=0.001,
    entry_timeout_seconds=60,
    fee_per_order=1.10,
    min_gain_to_fee_ratio=3.0,
    sizing_equity_cap=5_000.0,
    max_quote_age_seconds=60.0,
)


ENV_LIMIT_FIELDS = {
    "RISK_FRACTION": ("risk_fraction", fraction),
    "MAX_POSITION_FRACTION": ("max_position_fraction", fraction),
    "MAX_TRADES_PER_DAY": ("max_trades_per_day", count),
    "MAX_DAILY_LOSS_FRACTION": ("max_daily_loss_fraction", fraction),
    "ENTRY_TIMEOUT_SECONDS": ("entry_timeout_seconds", count),
    "FEE_PER_ORDER": ("fee_per_order", non_negative),
    "MIN_GAIN_TO_FEE_RATIO": ("min_gain_to_fee_ratio", non_negative),
    "SIZING_EQUITY_CAP": ("sizing_equity_cap", positive),
    "MAX_QUOTE_AGE_SECONDS": ("max_quote_age_seconds", positive),
}


def limits_from_env(env: Mapping[str, str], base: RiskLimits) -> RiskLimits:
    """``base`` with any of the ENV_LIMIT_FIELDS variables present in ``env`` applied; a value outside its safe range refuses to start."""
    return overrides_from_env(env, ENV_LIMIT_FIELDS, base)


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    quantity: int
    limit_price: float
    stop: float
    target: float
    candle: pd.Timestamp


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    quantity: int
    entry_price: float
    stop: float
    target: float
    opened_at: pd.Timestamp


def position_size(
    portfolio_value: float, cash: float, price: float, stop: float, risk_fraction: float, max_position_fraction: float
) -> int:
    """Whole shares so a stop-out loses ``risk_fraction`` of the portfolio, capped by position size and cash."""
    risk_per_share = price - stop
    by_risk = math.floor(risk_fraction * portfolio_value / risk_per_share)
    by_position = math.floor(max_position_fraction * portfolio_value / price)
    by_cash = math.floor(cash / price)
    return max(0, min(by_risk, by_position, by_cash))


def entry_block_reason(
    setup: Setup, equity: float, cash: float, trades_today: int, daily_pnl: float, limits: RiskLimits
) -> Optional[str]:
    """Why a BUY signal cannot be traded under the limits, or None when it can.

    Sizing treats the account as no larger than ``limits.sizing_equity_cap`` so a
    simulated account trades like the balance the user would really fund.
    """
    equity = min(equity, limits.sizing_equity_cap)
    cash = min(cash, limits.sizing_equity_cap)
    if setup.signal not in BUY_SIGNALS:
        return f"{setup.signal} is not a buy signal"
    if setup.stop is None or setup.target is None:
        return "signal has no valid stop"
    if trades_today >= limits.max_trades_per_day:
        return f"daily cap of {limits.max_trades_per_day} trades reached"
    if daily_pnl <= -limits.max_daily_loss_fraction * equity:
        return f"daily loss limit of {limits.max_daily_loss_fraction:.0%} reached ({daily_pnl:.2f})"
    if setup.timestamp.time() >= limits.last_entry:
        return f"candle at {setup.timestamp.strftime('%H:%M')} is past the {limits.last_entry.strftime('%H:%M')} last-entry time"
    quantity = position_size(equity, cash, setup.price, setup.stop, limits.risk_fraction, limits.max_position_fraction)
    if quantity < 1:
        return (
            f"size is 0 shares: {limits.max_position_fraction:.0%} of equity {equity:.2f} is "
            f"{limits.max_position_fraction * equity:.2f}, below one share at {setup.price:.2f}"
        )
    expected_gain = (setup.target - setup.price) * quantity
    round_trip_fee = 2 * limits.fee_per_order
    if expected_gain < limits.min_gain_to_fee_ratio * round_trip_fee:
        return (
            f"expected gain {expected_gain:.2f} on {quantity} shares is below {limits.min_gain_to_fee_ratio:.0f}x "
            f"the {round_trip_fee:.2f} round-trip fee"
        )
    return None


def plan_entry(
    setup: Setup, symbol: str, equity: float, cash: float, trades_today: int, daily_pnl: float, limits: RiskLimits
) -> Optional[TradePlan]:
    """A limit buy for a BUY signal that passes every daily limit, otherwise None."""
    if entry_block_reason(setup, equity, cash, trades_today, daily_pnl, limits) is not None:
        return None
    equity = min(equity, limits.sizing_equity_cap)
    cash = min(cash, limits.sizing_equity_cap)
    quantity = position_size(equity, cash, setup.price, setup.stop, limits.risk_fraction, limits.max_position_fraction)
    return TradePlan(
        symbol=symbol,
        quantity=quantity,
        limit_price=round(setup.price * (1 + limits.entry_buffer_fraction), 2),
        stop=setup.stop,
        target=setup.target,
        candle=setup.timestamp,
    )


def exit_reason(position: OpenPosition, price: Optional[float], now: pd.Timestamp, cutoff: time) -> Optional[str]:
    """Why an open position should be closed now, or None to keep holding.

    The stop and target need a fresh live price; the session cutoff does not,
    so a quote outage or a stale feed cannot leave a position open overnight.
    """
    if price is not None and price <= position.stop:
        return "stop"
    if price is not None and price >= position.target:
        return "target"
    if now.time() >= cutoff:
        return "cutoff"
    return None
