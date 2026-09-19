"""Pure trade logic: sizing, entry plans, exit reasons, and the executor's decisions."""

from dataclasses import replace
from datetime import time
from pathlib import Path

import pandas as pd
import pytest

from bars_csv import load_bars_csv
from executor import (
    Account,
    CancelEntry,
    Enter,
    Exit,
    ExecutionState,
    Fill,
    Nothing,
    OrderState,
    PendingOrder,
    append_fill,
    decide,
    executor_pass,
    is_flat,
    load_fills,
    restore_day,
    step,
)
from signals import DEFAULT_CONFIG, evaluate
from state import LiveQuote, SignalStore, Snapshot
from trader import DEFAULT_LIMITS, OpenPosition, exit_reason, plan_entry, position_size


FIXTURES = Path(__file__).parent / "fixtures"
NEW_YORK = "America/New_York"
SECONDS = pd.Timedelta(seconds=1)


def funds() -> Account:
    return Account(100_000, 100_000)


class ScriptedBroker:
    """Answers order queries from a per-order script of states, so fills and cancels land later like at OpenD."""

    def __init__(self, order_states: dict[str, list[OrderState]]):
        self.order_states = {order_id: list(states) for order_id, states in order_states.items()}
        self.buys: list[tuple[str, int, float]] = []
        self.sells: list[tuple[str, int]] = []
        self.cancels: list[str] = []

    def account(self) -> Account:
        return funds()

    def position_quantity(self, symbol: str) -> int:
        return 0

    def buy_limit(self, symbol: str, quantity: int, price: float) -> str:
        self.buys.append((symbol, quantity, price))
        return f"buy-{len(self.buys)}"

    def sell_market(self, symbol: str, quantity: int) -> str:
        self.sells.append((symbol, quantity))
        return f"sell-{len(self.sells)}"

    def order(self, order_id: str) -> OrderState:
        states = self.order_states[order_id]
        return states.pop(0) if len(states) > 1 else states[0]

    def cancel(self, order_id: str) -> None:
        self.cancels.append(order_id)


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

    decision = decide(state, snapshot, quote_at(setup.price, snapshot.published_at), snapshot.published_at, funds, DEFAULT_LIMITS)
    assert isinstance(decision, Enter)

    acted = replace(state, last_candle=setup.timestamp)
    assert isinstance(decide(acted, snapshot, quote_at(setup.price, snapshot.published_at), snapshot.published_at, funds, DEFAULT_LIMITS), Nothing)


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

    decision = decide(state, snapshot, quote_at(setup.stop - 0.01, later), later, funds, DEFAULT_LIMITS)

    assert isinstance(decision, Exit) and decision.reason == "stop"


def test_decide_cancels_a_stale_entry_order():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    pending = PendingOrder("order-1", 100, setup.price, setup.stop, setup.target, snapshot.published_at)
    state = replace(ExecutionState.fresh(), pending_order=pending, last_candle=setup.timestamp)
    later = snapshot.published_at + pd.Timedelta(seconds=DEFAULT_LIMITS.entry_timeout_seconds + 1)

    assert isinstance(decide(state, snapshot, quote_at(setup.price, later), later, funds, DEFAULT_LIMITS), CancelEntry)
    requested = replace(state, pending_order=replace(pending, cancel_requested=True))
    assert isinstance(decide(requested, snapshot, quote_at(setup.price, later), later, funds, DEFAULT_LIMITS), Nothing)


def test_decide_does_nothing_while_halted():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    state = replace(ExecutionState.fresh(), halted="kill switch")

    assert isinstance(decide(state, snapshot, quote_at(setup.price, snapshot.published_at), snapshot.published_at, funds, DEFAULT_LIMITS), Nothing)


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


def test_decide_only_reads_the_account_when_a_new_entry_is_possible():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    reads = []

    def counted() -> Account:
        reads.append(1)
        return funds()

    position = OpenPosition("SPCX", 10, setup.price, setup.stop, setup.target, snapshot.published_at)
    for state in (
        replace(ExecutionState.fresh(), position=position),
        replace(ExecutionState.fresh(), last_candle=setup.timestamp),
        replace(ExecutionState.fresh(), halted="kill switch"),
    ):
        decide(state, snapshot, quote_at(setup.price, snapshot.published_at), snapshot.published_at, counted, DEFAULT_LIMITS)
    assert reads == []

    decide(ExecutionState.fresh(), snapshot, quote_at(setup.price, snapshot.published_at), snapshot.published_at, counted, DEFAULT_LIMITS)
    assert reads == [1]


def test_step_keeps_the_position_until_the_broker_confirms_the_exit_fill(fills_journal):
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = SignalStore()
    store.publish(snapshot)
    store.publish_quote(quote_at(setup.stop - 0.01, t0))
    position = OpenPosition("SPCX", 10, setup.price, setup.stop, setup.target, t0)
    state = replace(ExecutionState.fresh(), position=position, last_candle=setup.timestamp)
    fill_price = setup.stop - 0.02
    broker = ScriptedBroker({"sell-1": [OrderState("SUBMITTED", 0, 0.0), OrderState("FILLED_ALL", 10, fill_price)]})

    placed = step(state, store, broker, "SPCX", t0, DEFAULT_LIMITS)
    assert placed.position == position
    assert placed.pending_exit.reason == "stop" and placed.pending_exit.quantity == 10
    assert broker.sells == [("SPCX", 10)]

    still_open = step(placed, store, broker, "SPCX", t0 + 2 * SECONDS, DEFAULT_LIMITS)
    assert still_open.position == position and still_open.pending_exit is not None
    assert still_open.daily_pnl == 0.0 and still_open.fills == ()
    assert broker.sells == [("SPCX", 10)]

    filled = step(still_open, store, broker, "SPCX", t0 + 6 * SECONDS, DEFAULT_LIMITS)
    assert filled.position is None and filled.pending_exit is None and filled.halted is None
    assert filled.daily_pnl == pytest.approx((fill_price - setup.price) * 10)
    assert filled.fills[-1].side == "sell" and filled.fills[-1].price == fill_price
    assert [fill.reason for fill in load_fills(fills_journal)] == ["stop"]


def test_step_halts_with_the_shares_still_held_when_the_exit_order_dies():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = SignalStore()
    store.publish(snapshot)
    store.publish_quote(quote_at(setup.stop - 0.01, t0))
    position = OpenPosition("SPCX", 10, setup.price, setup.stop, setup.target, t0)
    state = replace(ExecutionState.fresh(), position=position, last_candle=setup.timestamp)
    broker = ScriptedBroker({"sell-1": [OrderState("FAILED", 0, 0.0)]})

    placed = step(state, store, broker, "SPCX", t0, DEFAULT_LIMITS)
    dead = step(placed, store, broker, "SPCX", t0 + 2 * SECONDS, DEFAULT_LIMITS)

    assert dead.position == position and dead.pending_exit is None
    assert "FAILED" in dead.halted and "10 shares" in dead.halted
    assert dead.daily_pnl == 0.0 and dead.fills == ()

    later = step(dead, store, broker, "SPCX", t0 + 8 * SECONDS, DEFAULT_LIMITS)
    assert later.position == position and broker.sells == [("SPCX", 10)]


def test_kill_switch_keeps_flattening_until_a_late_entry_fill_is_sold():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = SignalStore()
    store.publish(snapshot)
    store.publish_quote(quote_at(setup.price, t0))
    store.pull_kill_switch()
    pending = PendingOrder("buy-1", 10, setup.price, setup.stop, setup.target, t0)
    state = replace(ExecutionState.fresh(), pending_order=pending, last_candle=setup.timestamp)
    broker = ScriptedBroker({
        "buy-1": [OrderState("SUBMITTED", 0, 0.0), OrderState("FILLED_ALL", 10, setup.price)],
        "sell-1": [OrderState("FILLED_ALL", 10, setup.price + 0.5)],
    })

    first = executor_pass(state, store, broker, "SPCX", t0, DEFAULT_LIMITS, None)
    assert first.halted == "kill switch" and first.pending_order.cancel_requested
    assert broker.cancels == ["buy-1"]

    second = executor_pass(first, store, broker, "SPCX", t0 + 2 * SECONDS, DEFAULT_LIMITS, None)
    assert second.pending_order is not None and second.position is None
    assert broker.cancels == ["buy-1"]

    third = executor_pass(second, store, broker, "SPCX", t0 + 6 * SECONDS, DEFAULT_LIMITS, None)
    assert third.pending_order is None and third.position.quantity == 10
    assert third.pending_exit.reason == "kill switch" and broker.sells == [("SPCX", 10)]

    fourth = executor_pass(third, store, broker, "SPCX", t0 + 8 * SECONDS, DEFAULT_LIMITS, None)
    assert is_flat(fourth) and fourth.halted == "kill switch"
    assert fourth.daily_pnl == pytest.approx(5.0) and fourth.trades_today == 1

    fifth = executor_pass(fourth, store, broker, "SPCX", t0 + 10 * SECONDS, DEFAULT_LIMITS, None)
    assert is_flat(fifth) and broker.sells == [("SPCX", 10)] and broker.cancels == ["buy-1"]


def test_executor_pass_records_a_broker_failure_and_retries_with_the_same_state():
    class FlakyBroker(ScriptedBroker):
        def __init__(self):
            super().__init__({"sell-1": [OrderState("FILLED_ALL", 10, 0.0)]})
            self.gateway_down = True

        def sell_market(self, symbol: str, quantity: int) -> str:
            if self.gateway_down:
                raise ConnectionError("OpenD place_order failed: SELL 10 US.SPCX (SIMULATE): RET_ERROR disconnected")
            return super().sell_market(symbol, quantity)

    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = SignalStore()
    store.publish(snapshot)
    store.publish_quote(quote_at(setup.stop - 0.01, t0))
    position = OpenPosition("SPCX", 10, setup.price, setup.stop, setup.target, t0)
    state = replace(ExecutionState.fresh(), position=position, last_candle=setup.timestamp)
    broker = FlakyBroker()

    failed = executor_pass(state, store, broker, "SPCX", t0, DEFAULT_LIMITS, None)
    assert failed.position == position and failed.pending_exit is None and failed.halted is None
    assert "RET_ERROR disconnected" in failed.broker_error

    broker.gateway_down = False
    recovered = executor_pass(failed, store, broker, "SPCX", t0 + 2 * SECONDS, DEFAULT_LIMITS, None)
    assert recovered.broker_error is None and recovered.pending_exit.reason == "stop"
    assert broker.sells == [("SPCX", 10)]


def test_step_explains_a_new_buy_candle_skipped_while_an_entry_is_pending():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = SignalStore()
    store.publish(snapshot)
    store.publish_quote(quote_at(setup.price, t0))
    pending = PendingOrder("buy-1", 10, setup.price, setup.stop, setup.target, t0 - 10 * SECONDS)
    state = replace(ExecutionState.fresh(), pending_order=pending, last_candle=setup.timestamp - pd.Timedelta(minutes=1))
    broker = ScriptedBroker({"buy-1": [OrderState("SUBMITTED", 0, 0.0)]})

    state = step(state, store, broker, "SPCX", t0, DEFAULT_LIMITS)

    assert state.pending_order is not None
    assert state.last_candle == setup.timestamp
    assert state.last_skip == f"{setup.timestamp.strftime('%H:%M')} BUY SETUP skipped: entry order pending"


def test_restore_day_seeds_the_daily_limits_from_todays_journal(tmp_path):
    path = tmp_path / "journal.csv"
    yesterday = pd.Timestamp("2026-09-14 10:00", tz=NEW_YORK)
    today = pd.Timestamp("2026-09-15 10:00", tz=NEW_YORK)
    minute = pd.Timedelta(minutes=1)
    append_fill(path, Fill("buy", 10, 100.0, yesterday, "entry"))
    append_fill(path, Fill("sell", 10, 90.0, yesterday + 5 * minute, "stop"))
    append_fill(path, Fill("buy", 5, 150.0, today, "entry"))
    append_fill(path, Fill("sell", 5, 148.0, today + 5 * minute, "stop"))
    append_fill(path, Fill("buy", 5, 151.0, today + 10 * minute, "entry"))
    append_fill(path, Fill("sell", 5, 152.0, today + 15 * minute, "target"))

    state = restore_day(ExecutionState.fresh(), load_fills(path), today.date())

    assert state.day == today.date()
    assert state.trades_today == 2
    assert state.daily_pnl == pytest.approx(-10.0 + 5.0)
    assert len(state.fills) == 4 and all(fill.at.date() == today.date() for fill in state.fills)
