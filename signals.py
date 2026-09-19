"""One-minute setup engine shared by the live watcher, the backtest, and the dashboard.

Every function is pure: bars go in, a frozen result comes out. Only completed
one-minute candles may be passed in; use ``completed_minute_bars`` to drop the
minute that is still forming. Bars use lowercase ``open, high, low, close,
volume`` columns on a timezone-aware New York index stamped by candle start.
"""

from dataclasses import dataclass
from datetime import time
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class SignalConfig:
    """Tunable thresholds for the seven checks and the risk levels."""

    fast_ema: int
    slow_ema: int
    rsi_period: int
    rsi_buy_low: float
    rsi_buy_high: float
    rsi_sell_low: float
    rsi_sell_high: float
    volume_lookback: int
    volume_multiplier: float
    breakout_lookback: int
    stop_lookback: int
    reward_ratio: float
    strong_score: int
    setup_score: int
    session_start: time
    session_end: time
    minimum_session_bars: int
    minimum_history_bars: int


DEFAULT_CONFIG = SignalConfig(
    fast_ema=9,
    slow_ema=21,
    rsi_period=14,
    rsi_buy_low=55.0,
    rsi_buy_high=70.0,
    rsi_sell_low=30.0,
    rsi_sell_high=45.0,
    volume_lookback=20,
    volume_multiplier=1.5,
    breakout_lookback=30,
    stop_lookback=10,
    reward_ratio=3.0,
    strong_score=7,
    setup_score=5,
    session_start=time(9, 35),
    session_end=time(15, 50),
    minimum_session_bars=31,
    minimum_history_bars=100,
)

BUY_SIGNALS = frozenset({"STRONG BUY", "BUY SETUP"})
SELL_SIGNALS = frozenset({"STRONG SELL", "SELL SETUP"})


@dataclass(frozen=True)
class Levels:
    stop: float
    target: float


@dataclass(frozen=True)
class Setup:
    """Result of evaluating the latest completed one-minute candle."""

    timestamp: pd.Timestamp
    price: float
    ema_fast: float
    ema_slow: float
    vwap: float
    rsi: float
    relative_volume: float
    buy_checks: dict[str, bool]
    sell_checks: dict[str, bool]
    buy_score: int
    sell_score: int
    total_checks: int
    signal: str
    stop: Optional[float]
    target: Optional[float]


def completed_minute_bars(bars: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    """Keep only candles whose minute has fully elapsed at ``now``."""
    return bars.loc[bars.index < now.floor("min")]


def ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def rsi_series(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI, matching what charting platforms display."""
    delta = close.diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    losses = -delta.clip(upper=0).ewm(alpha=1 / period, adjust=False).mean()
    relative_strength = gains / losses.where(losses != 0)
    rsi = 100 - (100 / (1 + relative_strength))
    rsi = rsi.mask((losses == 0) & (gains > 0), 100.0)
    return rsi.mask((losses == 0) & (gains == 0), 50.0)


def session_vwap(session: pd.DataFrame) -> pd.Series:
    typical = (session["high"] + session["low"] + session["close"]) / 3
    cumulative_volume = session["volume"].cumsum()
    return (typical * session["volume"]).cumsum() / cumulative_volume.where(cumulative_volume > 0)


def latest_session(bars: pd.DataFrame) -> pd.DataFrame:
    return bars.loc[bars.index.date == bars.index[-1].date()]


def indicator_frame(bars: pd.DataFrame, config: SignalConfig) -> pd.DataFrame:
    """Latest session bars with EMA, RSI, and VWAP columns for charting and scoring."""
    history = bars.assign(
        ema_fast=ema(bars["close"], config.fast_ema),
        ema_slow=ema(bars["close"], config.slow_ema),
        rsi=rsi_series(bars["close"], config.rsi_period),
    )
    session = latest_session(history)
    return session.assign(vwap=session_vwap(session))


def long_levels(price: float, recent_lows: pd.Series, reward_ratio: float) -> Levels:
    stop = float(recent_lows.min())
    return Levels(stop=stop, target=price + reward_ratio * (price - stop))


def short_levels(price: float, recent_highs: pd.Series, reward_ratio: float) -> Levels:
    stop = float(recent_highs.max())
    return Levels(stop=stop, target=price - reward_ratio * (stop - price))


def _trend_up(fast: pd.Series, slow: pd.Series) -> bool:
    return bool(fast.iloc[-1] > slow.iloc[-1] and fast.iloc[-2] > slow.iloc[-2] and fast.iloc[-1] > fast.iloc[-2])


def _trend_down(fast: pd.Series, slow: pd.Series) -> bool:
    return bool(fast.iloc[-1] < slow.iloc[-1] and fast.iloc[-2] < slow.iloc[-2] and fast.iloc[-1] < fast.iloc[-2])


def _classify(buy_score: int, sell_score: int, config: SignalConfig) -> str:
    if buy_score >= config.strong_score:
        return "STRONG BUY"
    if buy_score >= config.setup_score:
        return "BUY SETUP"
    if sell_score >= config.strong_score:
        return "STRONG SELL"
    if sell_score >= config.setup_score:
        return "SELL SETUP"
    return "HOLD"


def _levels_for(signal: str, price: float, session: pd.DataFrame, config: SignalConfig) -> Optional[Levels]:
    if signal in BUY_SIGNALS:
        levels = long_levels(price, session["low"].tail(config.stop_lookback), config.reward_ratio)
        return levels if levels.stop < price else None
    if signal in SELL_SIGNALS:
        levels = short_levels(price, session["high"].tail(config.stop_lookback), config.reward_ratio)
        return levels if levels.stop > price else None
    return None


def evaluate(bars: pd.DataFrame, benchmark_bars: pd.DataFrame, config: SignalConfig) -> Setup:
    """Score the latest completed candle against the seven checks and attach risk levels."""
    required = {"open", "high", "low", "close", "volume"}
    missing = required.difference(bars.columns)
    if missing:
        raise ValueError(f"bars are missing required columns {sorted(missing)}; got {list(bars.columns)}")

    frame = indicator_frame(bars, config)
    benchmark = indicator_frame(benchmark_bars, config)
    latest = frame.iloc[-1]
    price = float(latest["close"])
    base = dict(
        timestamp=frame.index[-1],
        price=price,
        ema_fast=float(latest["ema_fast"]),
        ema_slow=float(latest["ema_slow"]),
        vwap=float(latest["vwap"]),
        rsi=float(latest["rsi"]),
        relative_volume=0.0,
        buy_checks={},
        sell_checks={},
        buy_score=0,
        sell_score=0,
        total_checks=7,
        stop=None,
        target=None,
    )

    if len(bars) < config.minimum_history_bars or len(benchmark_bars) < config.minimum_history_bars:
        history = min(len(bars), len(benchmark_bars))
        return Setup(**base, signal=f"HOLD (HISTORY {history}/{config.minimum_history_bars})")
    if len(frame) < config.minimum_session_bars:
        return Setup(**base, signal=f"HOLD (WARMING UP {len(frame)}/{config.minimum_session_bars})")

    prior = frame.iloc[:-1]
    prior_volume = float(prior["volume"].tail(config.volume_lookback).mean())
    relative_volume = float(latest["volume"] / prior_volume) if prior_volume > 0 else 0.0
    breakout_high = float(prior["high"].tail(config.breakout_lookback).max())
    breakout_low = float(prior["low"].tail(config.breakout_lookback).min())
    previous_close = float(prior["close"].iloc[-1])
    volume_ok = relative_volume >= config.volume_multiplier

    buy_checks = {
        "1m trend": _trend_up(frame["ema_fast"], frame["ema_slow"]),
        "1m candle": bool(price > latest["open"] and price > previous_close),
        "above VWAP": price > float(latest["vwap"]),
        "1m RSI": config.rsi_buy_low <= float(latest["rsi"]) < config.rsi_buy_high,
        "1m volume": volume_ok,
        "30m breakout": price > breakout_high,
        "benchmark trend": _trend_up(benchmark["ema_fast"], benchmark["ema_slow"]),
    }
    sell_checks = {
        "1m trend": _trend_down(frame["ema_fast"], frame["ema_slow"]),
        "1m candle": bool(price < latest["open"] and price < previous_close),
        "below VWAP": price < float(latest["vwap"]),
        "1m RSI": config.rsi_sell_low < float(latest["rsi"]) <= config.rsi_sell_high,
        "1m volume": volume_ok,
        "30m breakdown": price < breakout_low,
        "benchmark trend": _trend_down(benchmark["ema_fast"], benchmark["ema_slow"]),
    }
    buy_score = sum(buy_checks.values())
    sell_score = sum(sell_checks.values())
    scored = {
        **base,
        "relative_volume": relative_volume,
        "buy_checks": buy_checks,
        "sell_checks": sell_checks,
        "buy_score": buy_score,
        "sell_score": sell_score,
    }

    candle_time = frame.index[-1].time()
    if not config.session_start <= candle_time <= config.session_end:
        return Setup(**scored, signal="HOLD (TIME FILTER)")

    signal = _classify(buy_score, sell_score, config)
    levels = _levels_for(signal, price, frame, config)
    if levels is None:
        return Setup(**scored, signal=signal)
    return Setup(**{**scored, "stop": levels.stop, "target": levels.target}, signal=signal)
