"""Fee-aware event backtester for the one-minute setup rules.

Replays completed candles through ``signals.evaluate`` exactly as the live
watcher does, fills entries at the next candle's open capped at the limit price,
exits on the stop (with slippage), the target, or the session cutoff, and nets
fees per order. Pure functions over bar frames; no broker involved.
"""

from dataclasses import dataclass
from datetime import time
from typing import Optional

import pandas as pd

from signals import BUY_SIGNALS, SignalConfig, evaluate
from trader import RiskLimits


HISTORY_BARS = 800
SESSION_START = time(9, 30)


@dataclass(frozen=True)
class CostModel:
    fee_per_order: float
    slippage_fraction: float


@dataclass(frozen=True)
class Trade:
    entry_at: pd.Timestamp
    exit_at: pd.Timestamp
    entry_price: float
    exit_price: float
    stop: float
    target: float
    reason: str


@dataclass(frozen=True)
class Summary:
    trades: int
    win_rate: float
    gross_per_share: float
    net: float
    exits: dict[str, int]


@dataclass(frozen=True)
class _Open:
    entry_at: pd.Timestamp
    entry_price: float
    stop: float
    target: float


def resample_bars(bars: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate one-minute bars to ``minutes`` candles stamped by their start."""
    if minutes == 1:
        return bars
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = bars.resample(f"{minutes}min", label="left", closed="left").agg(agg).dropna()
    return out.loc[out.index.time >= SESSION_START]


def split_sessions(bars: pd.DataFrame, train_sessions: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """First ``train_sessions`` trading days and the remainder."""
    days = bars.index.normalize().unique()
    boundary = days[train_sessions]
    return bars.loc[bars.index < boundary], bars.loc[bars.index >= boundary]


def _close_position(position: _Open, bar: pd.Series, at: pd.Timestamp, limits: RiskLimits, costs: CostModel) -> Optional[Trade]:
    if bar["low"] <= position.stop:
        return Trade(position.entry_at, at, position.entry_price, position.stop * (1 - costs.slippage_fraction), position.stop, position.target, "stop")
    if bar["high"] >= position.target:
        return Trade(position.entry_at, at, position.entry_price, position.target, position.stop, position.target, "target")
    if at.time() >= limits.cutoff:
        return Trade(position.entry_at, at, position.entry_price, float(bar["close"]) * (1 - costs.slippage_fraction), position.stop, position.target, "cutoff")
    return None


def simulate(
    bars: pd.DataFrame, benchmark_bars: pd.DataFrame, config: SignalConfig, limits: RiskLimits, costs: CostModel
) -> list[Trade]:
    """Replay the rules over ``bars`` and return every completed trade, one share each."""
    trades: list[Trade] = []
    position: Optional[_Open] = None
    index = bars.index
    benchmark_index = benchmark_bars.index
    for i in range(config.minimum_history_bars, len(index)):
        at = index[i]
        if position is not None:
            closed = _close_position(position, bars.iloc[i], at, limits, costs)
            if closed is not None:
                trades.append(closed)
                position = None
            continue
        if not config.session_start <= at.time() < limits.last_entry or i + 1 >= len(index):
            continue
        j = benchmark_index.searchsorted(at, side="right")
        setup = evaluate(bars.iloc[max(0, i - HISTORY_BARS) : i + 1], benchmark_bars.iloc[max(0, j - HISTORY_BARS) : j], config)
        if setup.signal in BUY_SIGNALS and setup.stop is not None and setup.target is not None:
            limit = setup.price * (1 + limits.entry_buffer_fraction)
            entry = min(float(bars.iloc[i + 1]["open"]), limit)
            position = _Open(index[i + 1], entry, setup.stop, setup.target)
    return trades


def summarize(trades: list[Trade], quantity: int, costs: CostModel) -> Summary:
    """Per-share expectancy and the net result at ``quantity`` shares after fees."""
    if not trades:
        return Summary(trades=0, win_rate=0.0, gross_per_share=0.0, net=0.0, exits={})
    per_share = [trade.exit_price - trade.entry_price for trade in trades]
    gross = sum(per_share)
    wins = sum(1 for value in per_share if value > 0)
    exits: dict[str, int] = {}
    for trade in trades:
        exits[trade.reason] = exits.get(trade.reason, 0) + 1
    return Summary(
        trades=len(trades),
        win_rate=wins / len(trades),
        gross_per_share=round(gross / len(trades), 6),
        net=quantity * gross - 2 * costs.fee_per_order * len(trades),
        exits=exits,
    )
