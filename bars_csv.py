"""Read and write one-minute bar files shared by the history downloader, the backtest, and tests."""

from pathlib import Path

import pandas as pd


NEW_YORK = "America/New_York"


def save_bars_csv(bars: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bars.rename_axis("datetime").to_csv(path)


def load_bars_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, index_col="datetime")
    frame.index = pd.to_datetime(frame.index, utc=True).tz_convert(NEW_YORK)
    return frame.sort_index()
