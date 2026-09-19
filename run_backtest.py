"""Walk-forward backtest of the setup rules on the Moomoo history in ``data/``.

Run with the tradingagents environment after ``download_history.py``::

    python run_backtest.py

Candidate rule sets are scored on the first BACKTEST_TRAIN_SESSIONS sessions
(default 12). The best candidate by net result at BACKTEST_SHARES shares is then
scored on the remaining sessions it never saw. Fees come from FEE_PER_ORDER.
"""

import os
from dataclasses import replace
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from backtest import CostModel, resample_bars, simulate, split_sessions, summarize
from bars_csv import load_bars_csv
from signals import DEFAULT_CONFIG, SignalConfig
from trader import DEFAULT_LIMITS, limits_from_env


DATA_DIR = Path(__file__).parent / "data"
FIVE_MINUTE = replace(DEFAULT_CONFIG, volume_lookback=12, breakout_lookback=6, minimum_session_bars=7)


def candidates() -> dict[str, tuple[int, SignalConfig]]:
    """Rule sets to compare: timeframe, score threshold, stop width, and reward ratio."""
    out: dict[str, tuple[int, SignalConfig]] = {}
    for minutes, base in ((1, DEFAULT_CONFIG), (5, FIVE_MINUTE)):
        for score in (5, 6, 7):
            for stop_lookback, reward_ratio in ((3, 2.0), (10, 2.0), (10, 3.0)):
                name = f"{minutes}m score>={score} stop{stop_lookback} rr{reward_ratio:.0f}"
                out[name] = (minutes, replace(base, setup_score=score, stop_lookback=stop_lookback, reward_ratio=reward_ratio))
    return out


def load(symbol: str) -> pd.DataFrame:
    path = DATA_DIR / f"{symbol}_1m.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing; run download_history.py first")
    return load_bars_csv(path)


def line(name: str, summary) -> str:
    return (
        f"{name:<28} trades {summary.trades:>3} | win {summary.win_rate:>4.0%} | "
        f"gross/share {summary.gross_per_share:+.3f} | net {summary.net:+8.2f} | {summary.exits}"
    )


def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    symbol = os.getenv("WATCH_SYMBOL", "SPCX").upper()
    benchmark = os.getenv("WATCH_BENCHMARK", "QQQ").upper()
    shares = int(os.getenv("BACKTEST_SHARES", "20"))
    train_sessions = int(os.getenv("BACKTEST_TRAIN_SESSIONS", "12"))
    limits = limits_from_env(os.environ, DEFAULT_LIMITS)
    costs = CostModel(fee_per_order=limits.fee_per_order, slippage_fraction=0.0003)

    bars, benchmark_bars = load(symbol), load(benchmark)
    train, test = split_sessions(bars, train_sessions)
    train_benchmark, test_benchmark = split_sessions(benchmark_bars, train_sessions)
    print(
        f"{symbol}: {train.index.normalize().nunique()} training sessions, {test.index.normalize().nunique()} test sessions, "
        f"{shares} shares, fee {costs.fee_per_order:.2f}/order, slippage {costs.slippage_fraction:.2%}"
    )

    print("\nTraining sessions")
    results = {}
    for name, (minutes, config) in candidates().items():
        trades = simulate(resample_bars(train, minutes), resample_bars(train_benchmark, minutes), config, limits, costs)
        results[name] = summarize(trades, shares, costs)
        print(line(name, results[name]))

    best = max(results, key=lambda name: results[name].net)
    minutes, config = candidates()[best]
    print(f"\nBest on training: {best}")
    print("\nSame rules on the unseen test sessions")
    held_out = summarize(simulate(resample_bars(test, minutes), resample_bars(test_benchmark, minutes), config, limits, costs), shares, costs)
    print(line(best, held_out))
    verdict = "PASS: positive net on unseen sessions" if held_out.net > 0 and held_out.trades >= 5 else "FAIL: no edge on unseen sessions"
    print(f"\n{verdict}")


if __name__ == "__main__":
    main()
