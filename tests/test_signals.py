"""Behavior tests for the one-minute signal engine on real SPCX and QQQ bars."""

from pathlib import Path

import pandas as pd

from bars_csv import load_bars_csv
from signals import DEFAULT_CONFIG, completed_minute_bars, evaluate, long_levels, short_levels


FIXTURES = Path(__file__).parent / "fixtures"
NEW_YORK = "America/New_York"


def load_bars(symbol: str) -> pd.DataFrame:
    return load_bars_csv(FIXTURES / f"{symbol}_1m.csv")


def bars_until(symbol: str, end: str) -> pd.DataFrame:
    frame = load_bars(symbol)
    return frame.loc[frame.index <= pd.Timestamp(end, tz=NEW_YORK)]


def test_completed_minute_bars_drops_the_current_minute():
    bars = load_bars("SPCX")
    now = pd.Timestamp("2026-09-16 10:31:30", tz=NEW_YORK)

    completed = completed_minute_bars(bars, now)

    assert completed.index[-1] == pd.Timestamp("2026-09-16 10:30", tz=NEW_YORK)


def test_evaluate_scores_a_full_session_candle():
    setup = evaluate(
        bars_until("SPCX", "2026-09-15 14:59"),
        bars_until("QQQ", "2026-09-15 14:59"),
        DEFAULT_CONFIG,
    )

    assert setup.timestamp == pd.Timestamp("2026-09-15 14:59", tz=NEW_YORK)
    assert setup.total_checks == 7
    assert len(setup.buy_checks) == 7 and len(setup.sell_checks) == 7
    assert setup.buy_score == sum(setup.buy_checks.values())
    assert setup.sell_score == sum(setup.sell_checks.values())
    assert 0 < setup.rsi < 100
    assert setup.signal in {"STRONG BUY", "BUY SETUP", "STRONG SELL", "SELL SETUP", "HOLD"}


def test_evaluate_holds_after_the_afternoon_cutoff():
    setup = evaluate(
        bars_until("SPCX", "2026-09-15 15:58"),
        bars_until("QQQ", "2026-09-15 15:58"),
        DEFAULT_CONFIG,
    )

    assert setup.signal == "HOLD (TIME FILTER)"


def test_evaluate_reports_warm_up_early_in_the_session():
    setup = evaluate(
        bars_until("SPCX", "2026-09-15 09:40"),
        bars_until("QQQ", "2026-09-15 09:40"),
        DEFAULT_CONFIG,
    )

    assert setup.signal == "HOLD (WARMING UP 11/31)"


def test_long_levels_use_recent_low_and_two_to_one_reward():
    levels = long_levels(100.0, pd.Series([98.0, 97.5, 98.2]), 2.0)

    assert levels.stop == 97.5
    assert levels.target == 105.0


def test_short_levels_mirror_long_levels():
    levels = short_levels(100.0, pd.Series([101.0, 102.5, 101.8]), 2.0)

    assert levels.stop == 102.5
    assert levels.target == 95.0
