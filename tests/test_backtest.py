"""Fee-aware event backtester on the synthetic one-minute fixture bars."""

from pathlib import Path

import pandas as pd
import pytest

from backtest import CostModel, Trade, resample_bars, simulate, split_sessions, summarize
from bars_csv import load_bars_csv
from signals import DEFAULT_CONFIG
from trader import DEFAULT_LIMITS


FIXTURES = Path(__file__).parent / "fixtures"
NEW_YORK = "America/New_York"
COSTS = CostModel(fee_per_order=0.50, slippage_fraction=0.0003)


def fixture(symbol: str) -> pd.DataFrame:
    return load_bars_csv(FIXTURES / f"{symbol}_1m.csv")


def test_simulate_trades_the_fixture_days_with_modelled_fills():
    trades = simulate(fixture("SPCX"), fixture("QQQ"), DEFAULT_CONFIG, DEFAULT_LIMITS, COSTS)

    assert len(trades) > 0
    for trade in trades:
        assert trade.reason in {"stop", "target", "cutoff"}
        assert trade.exit_at > trade.entry_at
        assert trade.entry_at.time() <= DEFAULT_LIMITS.last_entry
        if trade.reason == "stop":
            assert trade.exit_price < trade.stop
        if trade.reason == "cutoff":
            assert trade.exit_at.time() >= DEFAULT_LIMITS.cutoff


def test_summarize_nets_out_fees_per_order():
    when = pd.Timestamp("2026-09-15 10:00", tz=NEW_YORK)
    trades = [
        Trade(when, when + pd.Timedelta(minutes=5), 100.0, 101.0, 99.5, 101.0, "target"),
        Trade(when, when + pd.Timedelta(minutes=9), 100.0, 99.4, 99.5, 101.0, "stop"),
    ]

    summary = summarize(trades, 10, COSTS)

    assert summary.trades == 2
    assert summary.win_rate == 0.5
    assert summary.gross_per_share == 0.2
    assert summary.net == pytest.approx(10 * 0.4 - 4 * 0.50)
    assert summary.exits == {"target": 1, "stop": 1}


def test_resample_bars_to_five_minutes_keeps_session_boundaries():
    five = resample_bars(fixture("SPCX"), 5)

    day = five.loc["2026-09-15"]
    assert day.index[0].time().strftime("%H:%M") == "09:30"
    assert day.index[-1].time().strftime("%H:%M") == "15:55"
    assert len(day) == 78
    assert day["volume"].sum() == fixture("SPCX").loc["2026-09-15"]["volume"].sum()


def test_split_sessions_divides_by_trading_day():
    train, test = split_sessions(fixture("SPCX"), 3)

    assert train.index.normalize().nunique() == 3
    assert test.index.normalize().nunique() == 2
    assert train.index[-1] < test.index[0]
