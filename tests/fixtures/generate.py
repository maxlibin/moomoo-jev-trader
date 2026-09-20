"""Deterministic synthetic one-minute bars for the test suite; nothing here is vendor market data.

Prices follow a seeded random walk driven by one shared market factor with a
few intraday trend impulses per session, so the setup rules find breakouts to
trade and the benchmark check co-moves. Both symbols are written from the same
seed, so re-running this script reproduces the committed files byte for byte::

    python tests/fixtures/generate.py
"""

import math
import random
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


FIXTURES = Path(__file__).parent
NEW_YORK = "America/New_York"
SESSIONS = ("2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16")
MINUTES_PER_SESSION = 390
SEED = 20260910
IMPULSES_PER_SESSION = 3
IMPULSE_MINUTES = (12, 24)
IMPULSE_DRIFT = 0.0007
IMPULSE_VOLUME_MULTIPLIER = 2.4
FACTOR_SIGMA = 0.0005


@dataclass(frozen=True)
class Symbol:
    name: str
    start_price: float
    beta: float
    noise_sigma: float
    base_volume: int


SYMBOLS = (
    Symbol("SPCX", 150.0, 1.3, 0.0007, 240_000),
    Symbol("QQQ", 705.0, 1.0, 0.0002, 300_000),
)


def market_factor(rng: random.Random) -> tuple[list[float], list[bool]]:
    """Per-minute factor returns for every session and whether each minute sits inside a trend impulse."""
    returns: list[float] = []
    impulse: list[bool] = []
    for _ in SESSIONS:
        drift = [0.0] * MINUTES_PER_SESSION
        for _ in range(IMPULSES_PER_SESSION):
            length = rng.randint(*IMPULSE_MINUTES)
            start = rng.randint(35, MINUTES_PER_SESSION - length - 20)
            sign = 1 if rng.random() < 0.65 else -1
            for minute in range(start, start + length):
                drift[minute] = sign * IMPULSE_DRIFT
        for minute in range(MINUTES_PER_SESSION):
            returns.append(drift[minute] + rng.gauss(0.0, FACTOR_SIGMA))
            impulse.append(drift[minute] != 0.0)
    return returns, impulse


def intraday_volume_shape(minute: int) -> float:
    """U-shaped volume profile: heavy at the open and close, lighter around midday."""
    x = minute / (MINUTES_PER_SESSION - 1)
    return 0.6 + 1.8 * (x - 0.5) ** 2 + 0.9 * math.exp(-x * 25)


def bars_for(symbol: Symbol, factor: list[float], impulse: list[bool], rng: random.Random) -> pd.DataFrame:
    index = []
    for session in SESSIONS:
        start = pd.Timestamp(f"{session} 09:30", tz=NEW_YORK)
        index.extend(start + pd.Timedelta(minutes=minute) for minute in range(MINUTES_PER_SESSION))
    rows = []
    close = symbol.start_price
    for position, (factor_return, in_impulse) in enumerate(zip(factor, impulse)):
        minute = position % MINUTES_PER_SESSION
        change = symbol.beta * factor_return + rng.gauss(0.0, symbol.noise_sigma)
        bar_open = round(close * (1 + rng.gauss(0.0, 0.00015)), 2)
        close = round(close * (1 + change), 2)
        wick = abs(rng.gauss(0.0, 0.0004))
        high = round(max(bar_open, close) * (1 + wick), 2)
        low = round(min(bar_open, close) * (1 - wick), 2)
        volume_scale = intraday_volume_shape(minute) * math.exp(rng.gauss(0.0, 0.35))
        if in_impulse:
            volume_scale *= IMPULSE_VOLUME_MULTIPLIER
        rows.append((bar_open, high, low, close, int(symbol.base_volume * volume_scale)))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex(index, name="datetime"))


def generate() -> dict[str, pd.DataFrame]:
    rng = random.Random(SEED)
    factor, impulse = market_factor(rng)
    return {symbol.name: bars_for(symbol, factor, impulse, random.Random(SEED + len(symbol.name))) for symbol in SYMBOLS}


def main() -> None:
    for name, frame in generate().items():
        path = FIXTURES / f"{name}_1m.csv"
        frame.to_csv(path)
        print(f"{name}: {len(frame)} bars from {frame.index[0]} to {frame.index[-1]} -> {path}")


if __name__ == "__main__":
    main()
