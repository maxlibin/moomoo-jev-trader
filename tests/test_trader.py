"""Pure trade logic: sizing, entry plans, exit reasons, and the executor's decisions."""

from dataclasses import replace
from datetime import time
from pathlib import Path

import pandas as pd

from bars_csv import load_bars_csv
from executor import CancelEntry, Enter, Exit, ExecutionState, Nothing, PendingOrder, decide
from signals import DEFAULT_CONFIG, evaluate
from state import LiveQuote, SignalStore, Snapshot
from trader import DEFAULT_LIMITS, OpenPosition, exit_reason, plan_entry, position_size


FIXTURES = Path(__file__).parent / "fixtures"
NEW_YORK = "America/New_York"


def bars_until(symbol: str, end: str) -> pd.DataFrame:
    frame = load_bars_csv(FIXTURES / f"{symbol}_1m.csv")
    return frame.loc[frame.index <= pd.Timestamp(end, tz=NEW_YORK)]


def buy_setup():
    setup = evaluate(bars_until("SPCX", "2026-09-15 14:59"), bars_until("QQQ", "2026-09-15 14:59"), DEFAULT_CONFIG)
    return replace(setup, signal="BUY SETUP", stop=setup.price - 1.00, target=setup.price + 3.00)


def snapshot_of(setup) -> Snapshot:
    return Snapshot("SPCX", "QQQ", setup.timestamp + pd.Timedelta(seconds=62), setup, None, None)


def quote_at(price: float, when: pd.Timestamp) -> LiveQuote:
    return LiveQuote("SPCX", price, when, None, when, None)


def test_position_size_is_capped_by_max_position_fraction():
    # 1% of 100k at 0.40 risk is 250 shares; 20% cap at 150 is 133 shares.
    assert position_size(100_000, 100_000, 150.0, 149.6, 0.01, 0.20) == 133


def test_position_size_is_capped_by_cash():
    assert position_size(100_000, 3_000, 150.0, 149.6, 0.01, 1.0) == 20


def test_plan_entry_builds_a_limit_order_with_levels():
    setup = buy_setup()

    plan = plan_entry(setup, "SPCX", 100_000, 100_000, 0, 0.0, DEFAULT_LIMITS)

    assert plan is not None
    assert plan.symbol == "SPCX"
    assert plan.limit_price == round(setup.price * (1 + DEFAULT_LIMITS.entry_buffer_fraction), 2)
    assert plan.stop == setup.stop and plan.target == setup.target
    sized_on = min(100_000, DEFAULT_LIMITS.sizing_equity_cap)
    assert plan.quantity == position_size(sized_on, sized_on, setup.price, setup.stop, DEFAULT_LIMITS.risk_fraction, DEFAULT_LIMITS.max_position_fraction)


def test_plan_entry_refuses_when_daily_limits_are_hit():
    setup = buy_setup()

    assert plan_entry(setup, "SPCX", 100_000, 100_000, DEFAULT_LIMITS.max_trades_per_day, 0.0, DEFAULT_LIMITS) is None
    assert plan_entry(setup, "SPCX", 100_000, 100_000, 0, -0.02 * 100_000, DEFAULT_LIMITS) is None
    assert plan_entry(replace(setup, signal="HOLD"), "SPCX", 100_000, 100_000, 0, 0.0, DEFAULT_LIMITS) is None


def test_exit_reason_checks_stop_target_then_cutoff():
    opened = pd.Timestamp("2026-09-15 14:00", tz=NEW_YORK)
    position = OpenPosition("SPCX", 100, 150.0, 149.6, 150.8, opened)

    assert exit_reason(position, 149.55, opened + pd.Timedelta(minutes=1), time(15, 50)) == "stop"
    assert exit_reason(position, 150.85, opened + pd.Timedelta(minutes=1), time(15, 50)) == "target"
    assert exit_reason(position, 150.2, pd.Timestamp("2026-09-15 15:50", tz=NEW_YORK), time(15, 50)) == "cutoff"
    assert exit_reason(position, 150.2, opened + pd.Timedelta(minutes=1), time(15, 50)) is None


def test_decide_enters_once_per_candle_and_then_waits():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    state = ExecutionState.fresh()

    decision = decide(state, snapshot, quote_at(setup.price, snapshot.published_at), snapshot.published_at, 100_000, 100_000, DEFAULT_LIMITS)
    assert isinstance(decision, Enter)

    acted = replace(state, last_candle=setup.timestamp)
    assert isinstance(decide(acted, snapshot, quote_at(setup.price, snapshot.published_at), snapshot.published_at, 100_000, 100_000, DEFAULT_LIMITS), Nothing)


def test_step_jev_gate_can_wait_or_reject_without_placing_an_order():
    from executor import Account, step

    class Broker:
        def __init__(self):
            self.buys = 0

        def account(self):
            return Account(100_000, 100_000)

        def buy_limit(self, *args):
            self.buys += 1
            return "unexpected"

    setup = buy_setup()
    snapshot = snapshot_of(setup)
    store = SignalStore()
    store.publish(snapshot)
    store.publish_quote(quote_at(setup.price, snapshot.published_at))
    broker = Broker()

    waiting = step(
        ExecutionState.fresh(), store, broker, "SPCX", snapshot.published_at, DEFAULT_LIMITS,
        entry_gate=lambda snapshot, now: (None, "review pending"),
    )
    assert waiting.last_candle is None
    assert "review pending" in waiting.last_skip

    rejected = step(
        waiting, store, broker, "SPCX", snapshot.published_at, DEFAULT_LIMITS,
        entry_gate=lambda snapshot, now: (False, "Jev rejected"),
    )
    assert rejected.last_candle is None  # a later five-second review may approve the same candle
    assert "Jev rejected" in rejected.last_skip
    assert broker.buys == 0


def test_decide_exits_an_open_position_on_the_live_price():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    position = OpenPosition("SPCX", 100, setup.price, setup.stop, setup.target, snapshot.published_at)
    state = replace(ExecutionState.fresh(), position=position, last_candle=setup.timestamp)
    later = snapshot.published_at + pd.Timedelta(seconds=30)

    decision = decide(state, snapshot, quote_at(setup.stop - 0.01, later), later, 100_000, 90_000, DEFAULT_LIMITS)

    assert isinstance(decision, Exit) and decision.reason == "stop"


def test_decide_cancels_a_stale_entry_order():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    pending = PendingOrder("order-1", 100, setup.price, setup.stop, setup.target, snapshot.published_at)
    state = replace(ExecutionState.fresh(), pending_order=pending, last_candle=setup.timestamp)
    later = snapshot.published_at + pd.Timedelta(seconds=DEFAULT_LIMITS.entry_timeout_seconds + 1)

    assert isinstance(decide(state, snapshot, quote_at(setup.price, later), later, 100_000, 100_000, DEFAULT_LIMITS), CancelEntry)


def test_decide_does_nothing_while_halted():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    state = replace(ExecutionState.fresh(), halted="kill switch")

    assert isinstance(decide(state, snapshot, quote_at(setup.price, snapshot.published_at), snapshot.published_at, 100_000, 100_000, DEFAULT_LIMITS), Nothing)


def test_entry_block_reason_explains_a_zero_size_from_the_position_cap():
    from trader import entry_block_reason

    setup = buy_setup()

    reason = entry_block_reason(setup, 388.0, 380.0, 0, 0.0, DEFAULT_LIMITS)

    assert reason is not None
    assert "0 shares" in reason and "20%" in reason
    assert entry_block_reason(setup, 100_000, 100_000, 0, 0.0, DEFAULT_LIMITS) is None
    assert "6 trades" in entry_block_reason(setup, 100_000, 100_000, 6, 0.0, DEFAULT_LIMITS)


def test_step_records_why_an_entry_was_skipped():
    from executor import step

    class NoOrderBroker:
        def account(self):
            from executor import Account

            return Account(equity=388.0, cash=380.0)

        def position_quantity(self, symbol):
            return 0

        def buy_limit(self, *args):
            raise AssertionError("must not place an order for zero shares")

        def sell_market(self, *args):
            raise AssertionError("nothing to sell")

        def order(self, order_id):
            raise AssertionError("no orders exist")

        def cancel(self, order_id):
            raise AssertionError("no orders exist")

    setup = buy_setup()
    snapshot = snapshot_of(setup)
    store = SignalStore()
    store.publish(snapshot)
    store.publish_quote(quote_at(setup.price, snapshot.published_at))

    state = step(ExecutionState.fresh(), store, NoOrderBroker(), "SPCX", snapshot.published_at, DEFAULT_LIMITS)

    assert state.last_candle == setup.timestamp
    assert state.pending_order is None
    assert "0 shares" in state.last_skip


def test_limits_from_env_override_only_the_given_fields():
    from trader import limits_from_env

    limits = limits_from_env({"MAX_POSITION_FRACTION": "0.45", "MAX_TRADES_PER_DAY": "3"}, DEFAULT_LIMITS)

    assert limits.max_position_fraction == 0.45
    assert limits.max_trades_per_day == 3
    assert limits.risk_fraction == DEFAULT_LIMITS.risk_fraction
    assert limits.cutoff == DEFAULT_LIMITS.cutoff


def test_no_entries_in_the_last_minutes_before_the_cutoff():
    from trader import entry_block_reason

    late = replace(buy_setup(), timestamp=pd.Timestamp("2026-09-15 15:49", tz=NEW_YORK))
    reason = entry_block_reason(late, 100_000, 100_000, 0, 0.0, DEFAULT_LIMITS)

    assert reason is not None and "15:45" in reason
    ok = replace(buy_setup(), timestamp=pd.Timestamp("2026-09-15 15:44", tz=NEW_YORK))
    assert entry_block_reason(ok, 100_000, 100_000, 0, 0.0, DEFAULT_LIMITS) is None


def test_entry_is_blocked_when_expected_gain_does_not_cover_fees():
    from trader import entry_block_reason

    # 388 dollar account, 45% cap: one share; target 3.00 above entry vs 3x a 2.20 round trip.
    limits = replace(DEFAULT_LIMITS, max_position_fraction=0.45)
    reason = entry_block_reason(buy_setup(), 388.0, 380.0, 0, 0.0, limits)

    assert reason is not None and "fee" in reason
    assert entry_block_reason(buy_setup(), 100_000, 100_000, 0, 0.0, limits) is None


def test_sizing_uses_the_equity_cap_not_the_whole_account():
    from trader import DEFAULT_LIMITS as limits

    setup = buy_setup()
    plan = plan_entry(setup, "SPCX", 1_000_000, 1_000_000, 0, 0.0, limits)

    capped = position_size(limits.sizing_equity_cap, limits.sizing_equity_cap, setup.price, setup.stop, limits.risk_fraction, limits.max_position_fraction)
    assert plan is not None and plan.quantity == capped
    assert plan.quantity < position_size(1_000_000, 1_000_000, setup.price, setup.stop, limits.risk_fraction, limits.max_position_fraction)


def test_step_resets_daily_counters_on_a_new_session():
    import datetime

    from executor import Account, step

    class QuietBroker:
        def account(self):
            return Account(equity=5_000.0, cash=5_000.0)

        def position_quantity(self, symbol):
            return 0

    setup = replace(buy_setup(), signal="HOLD", stop=None, target=None)
    snapshot = snapshot_of(setup)
    store = SignalStore()
    store.publish(snapshot)
    store.publish_quote(quote_at(setup.price, snapshot.published_at))
    yesterday = replace(ExecutionState.fresh(), trades_today=6, daily_pnl=-90.0, day=datetime.date(2026, 9, 14))

    state = step(yesterday, store, QuietBroker(), "SPCX", snapshot.published_at, DEFAULT_LIMITS)

    assert state.day == datetime.date(2026, 9, 15)
    assert state.trades_today == 0 and state.daily_pnl == 0.0


def test_fills_journal_round_trips(tmp_path):
    from executor import Fill, append_fill, load_fills

    path = tmp_path / "fills.csv"
    when = pd.Timestamp("2026-09-15 10:00", tz=NEW_YORK)
    append_fill(path, Fill("buy", 10, 150.25, when, "entry"))
    append_fill(path, Fill("sell", 10, 151.0, when + pd.Timedelta(minutes=5), "target"))

    fills = load_fills(path)

    assert [f.side for f in fills] == ["buy", "sell"]
    assert fills[1].price == 151.0 and fills[1].at == when + pd.Timedelta(minutes=5)
