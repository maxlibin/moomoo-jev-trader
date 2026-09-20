"""Pure trade logic: sizing, entry plans, exit reasons, and the executor's decisions."""

from dataclasses import replace
from datetime import time
from pathlib import Path

import pandas as pd
import pytest

from bars_csv import load_bars_csv
from executor import (
    Account,
    BrokerOrder,
    CancelEntry,
    Enter,
    Exit,
    ExecutionState,
    Fill,
    Holding,
    Nothing,
    OrderState,
    PendingExit,
    PendingOrder,
    append_fill,
    decide,
    executor_pass,
    initial_state,
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
    """Answers order queries from a per-order script of states, so fills and cancels land later like at OpenD.

    ``accepted`` is what the broker's order list reports, newest last; a
    placement that raises before being added there was never accepted.
    """

    def __init__(self, order_states: dict[str, list[OrderState]], held: int = 0, cost_price: float = 0.0):
        self.order_states = {order_id: list(states) for order_id, states in order_states.items()}
        self.buys: list[tuple[str, int, float]] = []
        self.sells: list[tuple[str, int]] = []
        self.cancels: list[str] = []
        self.accepted: list[tuple[str, str, int, float]] = []
        self.held = held
        self.cost_price = cost_price

    def account(self) -> Account:
        return funds()

    def holding(self, symbol: str) -> Holding:
        return Holding(self.held, self.cost_price)

    def orders(self, symbol: str) -> list[BrokerOrder]:
        return [
            BrokerOrder(order_id, side, quantity, price, current.status, current.filled_quantity, current.average_price)
            for order_id, side, quantity, price in reversed(self.accepted)
            for current in [self.order_states[order_id][0]]
        ]

    def buy_limit(self, symbol: str, quantity: int, price: float) -> str:
        self.buys.append((symbol, quantity, price))
        order_id = f"buy-{len(self.buys)}"
        self.accepted.append((order_id, "BUY", quantity, price))
        return order_id

    def sell_market(self, symbol: str, quantity: int) -> str:
        self.sells.append((symbol, quantity))
        order_id = f"sell-{len(self.sells)}"
        self.accepted.append((order_id, "SELL", quantity, 0.0))
        return order_id

    def order(self, order_id: str) -> OrderState:
        states = self.order_states[order_id]
        return states.pop(0) if len(states) > 1 else states[0]

    def cancel(self, order_id: str) -> None:
        self.cancels.append(order_id)


def held_position(setup, opened: pd.Timestamp, quantity: int = 10) -> OpenPosition:
    return OpenPosition("SPCX", quantity, setup.price, setup.stop, setup.target, opened)


def stopped_out_store(setup, snapshot) -> SignalStore:
    store = SignalStore()
    store.publish(snapshot)
    store.publish_quote(quote_at(setup.stop - 0.01, snapshot.published_at))
    return store


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
    assert exit_reason(position, None, opened + pd.Timedelta(minutes=1), time(15, 50)) is None
    assert exit_reason(position, None, pd.Timestamp("2026-09-15 15:50", tz=NEW_YORK), time(15, 50)) == "cutoff"


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

        def holding(self, symbol):
            return Holding(0, 0.0)

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

    limits = limits_from_env({"MAX_POSITION_FRACTION": "0.45", "MAX_TRADES_PER_DAY": "3", "MAX_QUOTE_AGE_SECONDS": "20"}, DEFAULT_LIMITS)

    assert limits.max_position_fraction == 0.45
    assert limits.max_trades_per_day == 3
    assert limits.max_quote_age_seconds == 20.0
    assert limits.risk_fraction == DEFAULT_LIMITS.risk_fraction
    assert limits.cutoff == DEFAULT_LIMITS.cutoff


def test_limits_from_env_refuses_values_outside_their_safe_range_by_name():
    from trader import limits_from_env

    for variable, value in (
        ("RISK_FRACTION", "1.5"),
        ("RISK_FRACTION", "0"),
        ("MAX_POSITION_FRACTION", "20"),
        ("MAX_DAILY_LOSS_FRACTION", "-0.02"),
        ("MAX_TRADES_PER_DAY", "0"),
        ("ENTRY_TIMEOUT_SECONDS", "-5"),
        ("FEE_PER_ORDER", "-1"),
        ("MIN_GAIN_TO_FEE_RATIO", "nan"),
        ("SIZING_EQUITY_CAP", "0"),
        ("MAX_QUOTE_AGE_SECONDS", "0"),
        ("RISK_FRACTION", "one percent"),
        ("MAX_TRADES_PER_DAY", ""),
    ):
        with pytest.raises(ValueError, match=f"{variable}={value!r}"):
            limits_from_env({variable: value}, DEFAULT_LIMITS)

    assert limits_from_env({"RISK_FRACTION": "1", "FEE_PER_ORDER": "0"}, DEFAULT_LIMITS).risk_fraction == 1.0


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

        def holding(self, symbol):
            return Holding(0, 0.0)

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


def test_executor_pass_records_a_failed_query_and_retries_with_the_same_state():
    class FlakyBroker(ScriptedBroker):
        def __init__(self):
            super().__init__({"sell-1": [OrderState("FILLED_ALL", 10, 150.0)]})
            self.gateway_down = True

        def order(self, order_id: str) -> OrderState:
            if self.gateway_down:
                raise ConnectionError("OpenD order_list_query failed for order sell-1 (SIMULATE): RET_ERROR disconnected")
            return super().order(order_id)

    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = stopped_out_store(setup, snapshot)
    position = held_position(setup, t0)
    state = replace(ExecutionState.fresh(), position=position, pending_exit=PendingExit("sell-1", 10, "stop", t0), last_candle=setup.timestamp)
    broker = FlakyBroker()

    failed = executor_pass(state, store, broker, "SPCX", t0 + 2 * SECONDS, DEFAULT_LIMITS, None)
    assert failed.position == position and failed.pending_exit == state.pending_exit and failed.halted is None
    assert "RET_ERROR disconnected" in failed.broker_error

    broker.gateway_down = False
    recovered = executor_pass(failed, store, broker, "SPCX", t0 + 4 * SECONDS, DEFAULT_LIMITS, None)
    assert recovered.broker_error is None and is_flat(recovered)
    assert recovered.fills[-1].price == 150.0 and broker.sells == []


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


def test_decide_never_enters_or_exits_on_a_stale_quote_but_still_exits_at_the_cutoff():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    limit = pd.Timedelta(seconds=DEFAULT_LIMITS.max_quote_age_seconds)
    reads = []

    def counted() -> Account:
        reads.append(1)
        return funds()

    stale_stop = quote_at(setup.stop - 0.01, t0)
    position = held_position(setup, t0)
    holding = replace(ExecutionState.fresh(), position=position, last_candle=setup.timestamp)
    assert isinstance(decide(holding, snapshot, stale_stop, t0 + limit, counted, DEFAULT_LIMITS), Exit)
    assert isinstance(decide(holding, snapshot, stale_stop, t0 + limit + SECONDS, counted, DEFAULT_LIMITS), Nothing)
    stale_target = quote_at(setup.target + 0.01, t0)
    assert isinstance(decide(holding, snapshot, stale_target, t0 + limit + SECONDS, counted, DEFAULT_LIMITS), Nothing)

    at_cutoff = pd.Timestamp("2026-09-15 15:50:00", tz=NEW_YORK)
    decision = decide(holding, snapshot, stale_stop, at_cutoff, counted, DEFAULT_LIMITS)
    assert isinstance(decision, Exit) and decision.reason == "cutoff"

    flat = ExecutionState.fresh()
    assert isinstance(decide(flat, snapshot, quote_at(setup.price, t0), t0 + limit + SECONDS, counted, DEFAULT_LIMITS), Nothing)
    assert reads == []
    assert isinstance(decide(flat, snapshot, quote_at(setup.price, t0 + limit), t0 + limit + SECONDS, counted, DEFAULT_LIMITS), Enter)
    assert reads == [1]


def test_step_waits_for_a_fresh_quote_before_entering_without_consuming_the_candle():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = SignalStore()
    store.publish(snapshot)
    store.publish_quote(quote_at(setup.price, t0 - 5 * pd.Timedelta(minutes=1)))
    broker = ScriptedBroker({"buy-1": [OrderState("SUBMITTED", 0, 0.0)]})

    waiting = step(ExecutionState.fresh(), store, broker, "SPCX", t0, DEFAULT_LIMITS)
    assert waiting.pending_order is None and waiting.last_candle is None and broker.buys == []
    assert waiting.last_skip == f"{setup.timestamp.strftime('%H:%M')} waiting: quote is 300s old, over the 60s limit"

    store.publish_quote(LiveQuote("SPCX", None, None, None, t0, "quote rights taken by the moomoo app"))
    down = step(waiting, store, broker, "SPCX", t0 + 2 * SECONDS, DEFAULT_LIMITS)
    assert down.pending_order is None and broker.buys == []
    assert "waiting: no live quote: quote rights taken by the moomoo app" in down.last_skip

    store.publish_quote(quote_at(setup.price, t0 + 4 * SECONDS))
    entered = step(down, store, broker, "SPCX", t0 + 4 * SECONDS, DEFAULT_LIMITS)
    assert entered.pending_order.order_id == "buy-1" and entered.last_candle == setup.timestamp
    assert len(broker.buys) == 1


def test_decide_exits_at_the_cutoff_even_when_the_quote_feed_is_down():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    position = held_position(setup, snapshot.published_at)
    state = replace(ExecutionState.fresh(), position=position, last_candle=setup.timestamp)
    no_quote = LiveQuote("SPCX", None, None, None, snapshot.published_at, "quote rights taken by the moomoo app")

    before = pd.Timestamp("2026-09-15 15:49:58", tz=NEW_YORK)
    assert isinstance(decide(state, snapshot, no_quote, before, funds, DEFAULT_LIMITS), Nothing)

    at_cutoff = pd.Timestamp("2026-09-15 15:50:00", tz=NEW_YORK)
    decision = decide(state, snapshot, no_quote, at_cutoff, funds, DEFAULT_LIMITS)
    assert isinstance(decision, Exit) and decision.reason == "cutoff"


def test_unconfirmed_exit_is_recovered_from_the_broker_instead_of_being_re_sent():
    class AcceptedButTimedOut(ScriptedBroker):
        def sell_market(self, symbol: str, quantity: int) -> str:
            super().sell_market(symbol, quantity)
            raise ConnectionError("OpenD place_order failed: SELL 10 US.SPCX @ 0.0 (SIMULATE): Timeout")

    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = stopped_out_store(setup, snapshot)
    position = held_position(setup, t0)
    state = replace(ExecutionState.fresh(), position=position, last_candle=setup.timestamp)
    broker = AcceptedButTimedOut({"sell-1": [OrderState("FILLED_ALL", 10, setup.stop - 0.02)]}, held=10)

    attempted = executor_pass(state, store, broker, "SPCX", t0, DEFAULT_LIMITS, None)
    assert attempted.position == position and attempted.pending_exit.order_id is None
    assert "Timeout" in attempted.broker_error and attempted.halted is None

    recovered = executor_pass(attempted, store, broker, "SPCX", t0 + 4 * SECONDS, DEFAULT_LIMITS, None)
    assert recovered.pending_exit.order_id == "sell-1" and recovered.broker_error is None
    assert broker.sells == [("SPCX", 10)]

    settled = executor_pass(recovered, store, broker, "SPCX", t0 + 8 * SECONDS, DEFAULT_LIMITS, None)
    assert is_flat(settled) and settled.halted is None
    assert settled.daily_pnl == pytest.approx((setup.stop - 0.02 - setup.price) * 10)
    assert broker.sells == [("SPCX", 10)]


def test_unconfirmed_exit_is_re_sent_only_when_the_broker_still_shows_every_share():
    class RejectedOnce(ScriptedBroker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.reject = True

        def sell_market(self, symbol: str, quantity: int) -> str:
            if self.reject:
                self.reject = False
                raise ConnectionError("OpenD place_order failed: SELL 10 US.SPCX @ 0.0 (SIMULATE): disconnected")
            return super().sell_market(symbol, quantity)

    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = stopped_out_store(setup, snapshot)
    state = replace(ExecutionState.fresh(), position=held_position(setup, t0), last_candle=setup.timestamp)
    broker = RejectedOnce({"sell-1": [OrderState("FILLED_ALL", 10, setup.stop)]}, held=10)

    attempted = executor_pass(state, store, broker, "SPCX", t0, DEFAULT_LIMITS, None)
    assert attempted.pending_exit.order_id is None and broker.sells == []

    re_sent = executor_pass(attempted, store, broker, "SPCX", t0 + 4 * SECONDS, DEFAULT_LIMITS, None)
    assert re_sent.pending_exit.order_id == "sell-1" and broker.sells == [("SPCX", 10)]

    settled = executor_pass(re_sent, store, broker, "SPCX", t0 + 8 * SECONDS, DEFAULT_LIMITS, None)
    assert is_flat(settled) and settled.halted is None and settled.exit_failures == 0


def test_unconfirmed_exit_with_shares_gone_adopts_the_broker_quantity_and_halts():
    class NeverAccepts(ScriptedBroker):
        def sell_market(self, symbol: str, quantity: int) -> str:
            raise ConnectionError("OpenD place_order failed: SELL 10 US.SPCX @ 0.0 (SIMULATE): disconnected")

    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = stopped_out_store(setup, snapshot)
    state = replace(ExecutionState.fresh(), position=held_position(setup, t0), last_candle=setup.timestamp)

    partly_gone = NeverAccepts({}, held=4)
    attempted = executor_pass(state, store, partly_gone, "SPCX", t0, DEFAULT_LIMITS, None)
    reconciled = executor_pass(attempted, store, partly_gone, "SPCX", t0 + 4 * SECONDS, DEFAULT_LIMITS, None)
    assert reconciled.position.quantity == 4 and reconciled.pending_exit is None
    assert "6 of 10 tracked shares" in reconciled.halted

    all_gone = NeverAccepts({}, held=0)
    attempted = executor_pass(state, store, all_gone, "SPCX", t0, DEFAULT_LIMITS, None)
    reconciled = executor_pass(attempted, store, all_gone, "SPCX", t0 + 4 * SECONDS, DEFAULT_LIMITS, None)
    assert reconciled.position is None and reconciled.pending_exit is None
    assert "10 of 10 tracked shares" in reconciled.halted and reconciled.fills == ()


def test_unconfirmed_entry_is_adopted_when_accepted_and_skipped_otherwise_never_re_sent():
    class TimedOutEntry(ScriptedBroker):
        def __init__(self, *args, accepted_anyway: bool, **kwargs):
            super().__init__(*args, **kwargs)
            self.accepted_anyway = accepted_anyway

        def buy_limit(self, symbol: str, quantity: int, price: float) -> str:
            if self.accepted_anyway:
                super().buy_limit(symbol, quantity, price)
            else:
                self.buys.append((symbol, quantity, price))
            raise ConnectionError("OpenD place_order failed: BUY US.SPCX (SIMULATE): Timeout")

    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = SignalStore()
    store.publish(snapshot)
    store.publish_quote(quote_at(setup.price, t0))

    adopted = TimedOutEntry({"buy-1": [OrderState("SUBMITTED", 0, 0.0)]}, accepted_anyway=True)
    attempted = executor_pass(ExecutionState.fresh(), store, adopted, "SPCX", t0, DEFAULT_LIMITS, None)
    assert attempted.pending_order.order_id is None and attempted.last_candle == setup.timestamp
    recovered = executor_pass(attempted, store, adopted, "SPCX", t0 + 4 * SECONDS, DEFAULT_LIMITS, None)
    assert recovered.pending_order.order_id == "buy-1" and len(adopted.buys) == 1

    dropped = TimedOutEntry({}, accepted_anyway=False)
    attempted = executor_pass(ExecutionState.fresh(), store, dropped, "SPCX", t0, DEFAULT_LIMITS, None)
    skipped = executor_pass(attempted, store, dropped, "SPCX", t0 + 4 * SECONDS, DEFAULT_LIMITS, None)
    assert skipped.pending_order is None and skipped.last_candle == setup.timestamp
    assert "not accepted" in skipped.last_skip and len(dropped.buys) == 1
    again = executor_pass(skipped, store, dropped, "SPCX", t0 + 8 * SECONDS, DEFAULT_LIMITS, None)
    assert again.pending_order is None and len(dropped.buys) == 1


def test_timeout_exit_status_is_shown_until_the_broker_settles_it_and_nothing_else_is_sent():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = stopped_out_store(setup, snapshot)
    state = replace(ExecutionState.fresh(), position=held_position(setup, t0), last_candle=setup.timestamp)
    broker = ScriptedBroker({"sell-1": [OrderState("TIMEOUT", 0, 0.0), OrderState("TIMEOUT", 0, 0.0), OrderState("FILLED_ALL", 10, setup.stop)]})

    placed = step(state, store, broker, "SPCX", t0, DEFAULT_LIMITS)
    unknown = step(placed, store, broker, "SPCX", t0 + 2 * SECONDS, DEFAULT_LIMITS)
    assert unknown.position is not None and unknown.pending_exit.order_id == "sell-1"
    assert unknown.pending_exit.status == "TIMEOUT" and unknown.halted is None

    store.pull_kill_switch()
    still_unknown = executor_pass(unknown, store, broker, "SPCX", t0 + 6 * SECONDS, DEFAULT_LIMITS, None)
    assert still_unknown.pending_exit.status == "TIMEOUT" and broker.sells == [("SPCX", 10)]

    resolved = executor_pass(still_unknown, store, broker, "SPCX", t0 + 10 * SECONDS, DEFAULT_LIMITS, None)
    assert is_flat(resolved) and resolved.fills[-1].price == setup.stop
    assert resolved.halted == "kill switch" and broker.sells == [("SPCX", 10)]


def test_entry_that_times_out_then_fills_is_managed_like_any_other_position():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = stopped_out_store(setup, snapshot)
    pending = PendingOrder("buy-1", 10, setup.price, setup.stop, setup.target, t0)
    state = replace(ExecutionState.fresh(), pending_order=pending, last_candle=setup.timestamp, known_orders=frozenset({"buy-1"}))
    broker = ScriptedBroker({
        "buy-1": [OrderState("TIMEOUT", 0, 0.0), OrderState("FILLED_ALL", 10, setup.price)],
        "sell-1": [OrderState("FILLED_ALL", 10, setup.stop)],
    })

    unknown = step(state, store, broker, "SPCX", t0 + 2 * SECONDS, DEFAULT_LIMITS)
    assert unknown.pending_order.status == "TIMEOUT" and unknown.halted is None and broker.sells == []

    filled = step(unknown, store, broker, "SPCX", t0 + 6 * SECONDS, DEFAULT_LIMITS)
    assert filled.position.quantity == 10 and filled.halted is None
    assert filled.pending_exit.reason == "stop" and broker.sells == [("SPCX", 10)]

    closed = step(filled, store, broker, "SPCX", t0 + 10 * SECONDS, DEFAULT_LIMITS)
    assert is_flat(closed) and closed.halted is None and closed.trades_today == 1


def test_fill_cancelled_exit_counts_as_no_fill():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = stopped_out_store(setup, snapshot)
    position = held_position(setup, t0)
    state = replace(ExecutionState.fresh(), position=position, last_candle=setup.timestamp)
    broker = ScriptedBroker({"sell-1": [OrderState("FILL_CANCELLED", 10, setup.stop)]})

    placed = step(state, store, broker, "SPCX", t0, DEFAULT_LIMITS)
    dead = step(placed, store, broker, "SPCX", t0 + 2 * SECONDS, DEFAULT_LIMITS)

    assert dead.position == position and dead.pending_exit is None and dead.fills == ()
    assert "FILL_CANCELLED" in dead.halted and dead.daily_pnl == 0.0


def test_enforce_gate_is_asked_before_the_account_is_read():
    class CountingBroker(ScriptedBroker):
        def __init__(self):
            super().__init__({"buy-1": [OrderState("SUBMITTED", 0, 0.0)]})
            self.account_reads = 0

        def account(self) -> Account:
            self.account_reads += 1
            return funds()

    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = SignalStore()
    store.publish(snapshot)
    store.publish_quote(quote_at(setup.price, t0))
    broker = CountingBroker()

    state = ExecutionState.fresh()
    for offset in range(3):
        state = step(state, store, broker, "SPCX", t0 + 2 * offset * SECONDS, DEFAULT_LIMITS, entry_gate=lambda s, n: (None, "review pending"))
    state = step(state, store, broker, "SPCX", t0 + 6 * SECONDS, DEFAULT_LIMITS, entry_gate=lambda s, n: (False, "Jev rejected"))
    assert broker.account_reads == 0 and broker.buys == [] and state.last_candle is None

    approved = step(state, store, broker, "SPCX", t0 + 8 * SECONDS, DEFAULT_LIMITS, entry_gate=lambda s, n: (True, "Jev entry gate passed"))
    assert broker.account_reads == 1 and len(broker.buys) == 1 and approved.pending_order.order_id == "buy-1"


def test_kill_switch_sells_shares_that_were_already_in_the_account_at_start_up():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = SignalStore()
    store.publish(snapshot)
    store.publish_quote(quote_at(setup.price, t0))
    broker = ScriptedBroker({"sell-1": [OrderState("FILLED_ALL", 25, 141.0)]}, held=25, cost_price=140.0)

    state = initial_state(broker, "SPCX", t0)
    assert "already has 25 shares" in state.halted
    assert state.position.quantity == 25 and state.position.entry_price == 140.0

    idle = executor_pass(state, store, broker, "SPCX", t0 + 2 * SECONDS, DEFAULT_LIMITS, None)
    assert idle.position.quantity == 25 and broker.sells == []

    store.pull_kill_switch()
    selling = executor_pass(idle, store, broker, "SPCX", t0 + 4 * SECONDS, DEFAULT_LIMITS, None)
    assert selling.pending_exit.quantity == 25 and broker.sells == [("SPCX", 25)]

    flat = executor_pass(selling, store, broker, "SPCX", t0 + 6 * SECONDS, DEFAULT_LIMITS, None)
    assert is_flat(flat) and flat.halted == "kill switch"
    assert flat.daily_pnl == pytest.approx(25.0) and flat.trades_today == 0


def test_restore_day_matches_sells_to_buys_and_blocks_a_buy_without_a_journaled_exit(tmp_path):
    today = pd.Timestamp("2026-09-15 10:00", tz=NEW_YORK)
    minute = pd.Timedelta(minutes=1)

    unmatched = tmp_path / "unmatched.csv"
    append_fill(unmatched, Fill("buy", 5, 150.0, today, "entry"))
    append_fill(unmatched, Fill("sell", 5, 148.0, today + 5 * minute, "stop"))
    append_fill(unmatched, Fill("buy", 5, 151.0, today + 10 * minute, "entry"))
    state = restore_day(ExecutionState.fresh(), load_fills(unmatched), today.date())
    assert state.daily_pnl == pytest.approx(-10.0) and state.trades_today == 2
    assert "5 shares bought today without a journaled exit" in state.halted

    partial = tmp_path / "partial.csv"
    append_fill(partial, Fill("buy", 10, 150.0, today, "entry"))
    append_fill(partial, Fill("sell", 4, 152.0, today + 5 * minute, "target"))
    append_fill(partial, Fill("sell", 6, 151.0, today + 6 * minute, "target"))
    state = restore_day(ExecutionState.fresh(), load_fills(partial), today.date())
    assert state.daily_pnl == pytest.approx(4 * 2.0 + 6 * 1.0) and state.halted is None

    holding_sold = tmp_path / "holding.csv"
    append_fill(holding_sold, Fill("sell", 25, 141.0, today, "kill switch"))
    state = restore_day(ExecutionState.fresh(), load_fills(holding_sold), today.date())
    assert state.daily_pnl == 0.0 and state.trades_today == 0 and state.halted is None


def test_restart_recovery_never_adopts_an_order_from_before_start_up():
    class NeverAccepts(ScriptedBroker):
        def sell_market(self, symbol: str, quantity: int) -> str:
            if len(self.sells) == 0:
                self.sells.append((symbol, quantity))
                raise ConnectionError("OpenD place_order failed: SELL 10 US.SPCX @ 0.0 (SIMULATE): Timeout")
            return super().sell_market(symbol, quantity)

    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = stopped_out_store(setup, snapshot)
    broker = NeverAccepts({
        "buy-0": [OrderState("FILLED_ALL", 10, setup.price - 2.0)],
        "sell-0": [OrderState("FILLED_ALL", 10, setup.price - 3.0)],
        "sell-2": [OrderState("FILLED_ALL", 10, setup.stop)],
    }, held=10)
    broker.accepted = [("buy-0", "BUY", 10, setup.price - 2.0), ("sell-0", "SELL", 10, 0.0)]

    booted = initial_state(broker, "SPCX", t0)
    assert booted.known_orders == frozenset({"buy-0", "sell-0"})
    state = replace(booted, position=held_position(setup, t0), halted=None, last_candle=setup.timestamp)

    attempted = executor_pass(state, store, broker, "SPCX", t0, DEFAULT_LIMITS, None)
    assert attempted.pending_exit.order_id is None

    reconciled = executor_pass(attempted, store, broker, "SPCX", t0 + 4 * SECONDS, DEFAULT_LIMITS, None)
    assert reconciled.pending_exit.order_id == "sell-2" and reconciled.position is not None
    assert reconciled.fills == () and reconciled.daily_pnl == 0.0
    assert broker.sells == [("SPCX", 10), ("SPCX", 10)]


def test_reconciliation_ignores_an_order_placed_by_hand_before_the_executor_sent_its_own():
    class TimesOutOnce(ScriptedBroker):
        def sell_market(self, symbol: str, quantity: int) -> str:
            if len(self.sells) == 0:
                self.sells.append((symbol, quantity))
                raise ConnectionError("OpenD place_order failed: SELL 10 US.SPCX @ 0.0 (SIMULATE): Timeout")
            return super().sell_market(symbol, quantity)

    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = stopped_out_store(setup, snapshot)
    state = replace(ExecutionState.fresh(), position=held_position(setup, t0), last_candle=setup.timestamp)
    broker = TimesOutOnce({"manual-1": [OrderState("FILLED_ALL", 10, setup.price)], "sell-2": [OrderState("SUBMITTED", 0, 0.0)]}, held=10)
    broker.accepted.append(("manual-1", "SELL", 10, 0.0))

    attempted = executor_pass(state, store, broker, "SPCX", t0, DEFAULT_LIMITS, None)
    assert "manual-1" in attempted.known_orders and attempted.pending_exit.order_id is None

    reconciled = executor_pass(attempted, store, broker, "SPCX", t0 + 4 * SECONDS, DEFAULT_LIMITS, None)
    assert reconciled.pending_exit.order_id == "sell-2" and reconciled.fills == ()


def test_unconfirmed_exit_reconciliation_is_paced_like_other_open_orders():
    class CountingRejects(ScriptedBroker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.order_list_reads = 0

        def orders(self, symbol: str) -> list[BrokerOrder]:
            self.order_list_reads += 1
            return super().orders(symbol)

        def sell_market(self, symbol: str, quantity: int) -> str:
            raise ConnectionError("OpenD place_order failed: SELL 10 US.SPCX @ 0.0 (SIMULATE): trade not unlocked")

    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = stopped_out_store(setup, snapshot)
    state = replace(ExecutionState.fresh(), position=held_position(setup, t0), last_candle=setup.timestamp)
    broker = CountingRejects({}, held=10)

    attempted = executor_pass(state, store, broker, "SPCX", t0, DEFAULT_LIMITS, None)
    assert broker.order_list_reads == 1
    waiting = executor_pass(attempted, store, broker, "SPCX", t0 + 2 * SECONDS, DEFAULT_LIMITS, None)
    assert broker.order_list_reads == 1 and waiting.pending_exit.order_id is None
    executor_pass(waiting, store, broker, "SPCX", t0 + 4 * SECONDS, DEFAULT_LIMITS, None)
    assert broker.order_list_reads == 3


def test_kill_switch_stops_re_sending_a_sell_after_three_rejections():
    setup = buy_setup()
    snapshot = snapshot_of(setup)
    t0 = snapshot.published_at
    store = stopped_out_store(setup, snapshot)
    store.pull_kill_switch()
    state = replace(ExecutionState.fresh(), position=held_position(setup, t0), last_candle=setup.timestamp)
    broker = ScriptedBroker({f"sell-{n}": [OrderState("FAILED", 0, 0.0)] for n in range(1, 10)}, held=10)

    sent_at = {}
    for offset in range(0, 90, 2):
        state = executor_pass(state, store, broker, "SPCX", t0 + offset * SECONDS, DEFAULT_LIMITS, None)
        if len(broker.sells) not in sent_at:
            sent_at[len(broker.sells)] = offset

    assert len(broker.sells) == 3
    assert sent_at[1] == 0 and sent_at[2] == 4
    assert sent_at[3] - sent_at[2] >= 30
    assert state.position.quantity == 10 and state.pending_exit is None
    assert state.halted.startswith("kill switch") and "3 sell attempts" in state.halted and "flatten in moomoo" in state.halted


def test_run_executor_forever_retries_start_up_failures_and_outlives_an_unexpected_error(monkeypatch):
    import executor

    class BootBroker(ScriptedBroker):
        def __init__(self, boot_failures, account_failures):
            super().__init__({"buy-1": [OrderState("SUBMITTED", 0, 0.0)]}, held=0)
            self.boot_failures = list(boot_failures)
            self.account_failures = list(account_failures)

        def holding(self, symbol: str) -> Holding:
            if self.boot_failures:
                raise self.boot_failures.pop(0)
            return super().holding(symbol)

        def account(self) -> Account:
            if self.account_failures:
                raise self.account_failures.pop(0)
            return super().account()

    def sleep_then_stop(_seconds: float) -> None:
        sleep_then_stop.calls += 1
        if sleep_then_stop.calls >= 5:
            raise KeyboardInterrupt

    sleep_then_stop.calls = 0
    monkeypatch.setattr(executor.time, "sleep", sleep_then_stop)
    setup = buy_setup()
    store = SignalStore()
    store.publish(snapshot_of(setup))
    store.publish_quote(quote_at(setup.price, pd.Timestamp.now(tz=NEW_YORK)))
    broker = BootBroker(
        [
            ConnectionError("OpenD position_list_query failed for US.SPCX (SIMULATE): disconnected"),
            TypeError("unsupported operand type(s) for /: 'str' and 'int'"),
        ],
        [TypeError("'NoneType' object is not subscriptable")],
    )
    published = []
    monkeypatch.setattr(store, "publish_execution", published.append)

    with pytest.raises(KeyboardInterrupt):
        executor.run_executor_forever(store, broker, "SPCX", DEFAULT_LIMITS, 0.0)

    assert "starting up" in published[0].halted and "disconnected" in published[0].halted
    assert "starting up" in published[1].halted and "TypeError" in published[1].halted
    assert published[2].halted is None and is_flat(published[2])
    assert is_flat(published[3]) and "TypeError" in published[3].broker_error and "retried" in published[3].broker_error
    assert published[4].pending_order.order_id == "buy-1" and published[4].broker_error is None
    assert broker.buys == [("SPCX", published[4].pending_order.quantity, published[4].pending_order.limit_price)]
