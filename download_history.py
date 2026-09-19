"""Download one-minute history from Moomoo OpenD into ``data/`` for the backtest.

Run with the tradingagents environment while OpenD is running::

    python download_history.py

Covers the last BACKTEST_TRADING_DAYS trading days (default 20). Moomoo applies
a rolling quota on how many symbols you can pull history for, so the files are
kept and reused by ``run_backtest.py``.
"""

import os
from datetime import timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from bars_csv import save_bars_csv
from moomoo_feed import MoomooFeed


DATA_DIR = Path(__file__).parent / "data"
NEW_YORK = "America/New_York"


def history_dates(trading_days: int) -> tuple[str, str]:
    """First and last calendar dates covering the last ``trading_days`` weekdays before today."""
    today = pd.Timestamp.now(tz=NEW_YORK).normalize()
    sessions = pd.bdate_range(end=today - timedelta(days=1), periods=trading_days)
    return sessions[0].strftime("%Y-%m-%d"), sessions[-1].strftime("%Y-%m-%d")


def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    symbols = [os.getenv("WATCH_SYMBOL", "SPCX").upper(), os.getenv("WATCH_BENCHMARK", "QQQ").upper()]
    start, end = history_dates(int(os.getenv("BACKTEST_TRADING_DAYS", "20")))
    feed = MoomooFeed(os.getenv("MOOMOO_HOST", "127.0.0.1"), int(os.getenv("MOOMOO_PORT", "11111")))
    feed.connect(symbols)
    try:
        for symbol in symbols:
            bars = feed.history_minute_bars(symbol, start, end)
            path = DATA_DIR / f"{symbol}_1m.csv"
            save_bars_csv(bars, path)
            print(f"{symbol}: {len(bars)} one-minute bars from {bars.index[0]} to {bars.index[-1]} -> {path}")
    finally:
        feed.close()


if __name__ == "__main__":
    main()
