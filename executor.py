"""Execution state machine: pure ``decide`` plus a thin ``step`` that applies decisions through a broker.

``decide`` looks at the execution state, the latest closed-candle snapshot, the
live quote, and the account, and returns exactly one decision. ``step`` carries
it out against a ``Broker`` and returns the new state. Entry and exit orders are
tracked until the broker reports them filled or dead, so the state never claims
to be flat while shares are still held. ``run_executor_forever`` polls the store
every couple of seconds, records broker failures instead of dying on them, and
keeps flattening while the kill switch is pulled until nothing is left open.
"""

import csv
import datetime
import functools
import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional, Protocol, Union

import pandas as pd

from state import LiveQuote, SignalStore, Snapshot
from signals import BUY_SIGNALS
from trader import OpenPosition, RiskLimits, TradePlan, entry_block_reason, exit_reason, plan_entry


NEW_YORK = "America/New_York"
ORDER_POLL_SECONDS = 4.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    quantity: int
    limit_price: float
    stop: float
    target: float
    placed_at: pd.Timestamp
    cancel_requested: bool = False
    checked_at: Optional[pd.Timestamp] = None


@dataclass(frozen=True)
class PendingExit:
    order_id: str
    quantity: int
    reason: str
    placed_at: pd.Timestamp
    checked_at: Optional[pd.Timestamp] = None


@dataclass(frozen=True)
class Fill:
    side: str
    quantity: int
    price: float
    at: pd.Timestamp
    reason: str


@dataclass(frozen=True)
class ExecutionState:
    position: Optional[OpenPosition]
    pending_order: Optional[PendingOrder]
    trades_today: int
    daily_pnl: float
    last_candle: Optional[pd.Timestamp]
    halted: Optional[str]
    fills: tuple[Fill, ...] = field(default_factory=tuple)
    last_skip: Optional[str] = None
    day: Optional[datetime.date] = None
    pending_exit: Optional[PendingExit] = None
    broker_error: Optional[str] = None

    @staticmethod
    def fresh() -> "ExecutionState":
        return ExecutionState(position=None, pending_order=None, trades_today=0, daily_pnl=0.0, last_candle=None, halted=None)


def is_flat(state: ExecutionState) -> bool:
    """True when no shares are held and no entry or exit order is outstanding."""
    return state.position is None and state.pending_order is None and state.pending_exit is None


@dataclass(frozen=True)
class Enter:
    plan: TradePlan


@dataclass(frozen=True)
class Exit:
    reason: str


@dataclass(frozen=True)
class CancelEntry:
    order_id: str


@dataclass(frozen=True)
class Nothing:
    pass


Decision = Union[Enter, Exit, CancelEntry, Nothing]
EntryGate = Callable[[Snapshot, pd.Timestamp], tuple[Optional[bool], str]]


@dataclass(frozen=True)
class Account:
    equity: float
    cash: float


AccountFetch = Callable[[], Account]


@dataclass(frozen=True)
class OrderState:
    status: str
    filled_quantity: int
    average_price: float


class Broker(Protocol):
    def account(self) -> Account: ...
    def position_quantity(self, symbol: str) -> int: ...
    def buy_limit(self, symbol: str, quantity: int, price: float) -> str: ...
    def sell_market(self, symbol: str, quantity: int) -> str: ...
    def order(self, order_id: str) -> OrderState: ...
    def cancel(self, order_id: str) -> None: ...


def decide(
    state: ExecutionState,
    snapshot: Snapshot,
    quote: LiveQuote,
    now: pd.Timestamp,
    account: AccountFetch,
    limits: RiskLimits,
) -> Decision:
    """One decision for the current state; ``account`` is only called when a new entry is possible."""
    if state.halted is not None or state.pending_exit is not None:
        return Nothing()
    if state.pending_order is not None:
        pending = state.pending_order
        if not pending.cancel_requested and (now - pending.placed_at).total_seconds() > limits.entry_timeout_seconds:
            return CancelEntry(pending.order_id)
        return Nothing()
    if state.position is not None:
        if quote.price is None:
            return Nothing()
        reason = exit_reason(state.position, quote.price, now, limits.cutoff)
        return Exit(reason) if reason is not None else Nothing()
    if snapshot.setup is None or snapshot.setup.timestamp == state.last_candle:
        return Nothing()
    funds = account()
    plan = plan_entry(snapshot.setup, snapshot.symbol, funds.equity, funds.cash, state.trades_today, state.daily_pnl, limits)
    return Enter(plan) if plan is not None else Nothing()


FILLED = {"FILLED_ALL"}
DEAD = {"CANCELLED_ALL", "CANCELLED_PART", "FAILED", "SUBMIT_FAILED", "DISABLED", "DELETED"}


def _poll_due(checked_at: Optional[pd.Timestamp], now: pd.Timestamp) -> bool:
    """Order queries are rate limited by OpenD, so an open order is re-read every ``ORDER_POLL_SECONDS`` at most."""
    return checked_at is None or (now - checked_at).total_seconds() >= ORDER_POLL_SECONDS


def _settle_pending(state: ExecutionState, broker: Broker, symbol: str, now: pd.Timestamp) -> ExecutionState:
    """Turn a filled entry order into a position, or drop a dead one."""
    pending = state.pending_order
    if not _poll_due(pending.checked_at, now):
        return state
    order = broker.order(pending.order_id)
    if order.status in FILLED or (order.status in DEAD and order.filled_quantity > 0):
        position = OpenPosition(symbol, order.filled_quantity, order.average_price, pending.stop, pending.target, now)
        fill = Fill("buy", order.filled_quantity, order.average_price, now, "entry")
        logger.info("entry filled", extra={"symbol": symbol, "quantity": order.filled_quantity, "price": order.average_price})
        return replace(state, position=position, pending_order=None, trades_today=state.trades_today + 1, fills=state.fills + (fill,))
    if order.status in DEAD:
        logger.info("entry order ended without fill", extra={"order_id": pending.order_id, "status": order.status})
        return replace(state, pending_order=None)
    return replace(state, pending_order=replace(pending, checked_at=now))


def _settle_exit(state: ExecutionState, broker: Broker, now: pd.Timestamp) -> ExecutionState:
    """Book a filled exit order against the position; halt with the shares still held when it died unfilled."""
    exit_order = state.pending_exit
    position = state.position
    if not _poll_due(exit_order.checked_at, now):
        return state
    order = broker.order(exit_order.order_id)
    if order.filled_quantity > 0 and (order.status in FILLED or order.status in DEAD):
        pnl = (order.average_price - position.entry_price) * order.filled_quantity
        fill = Fill("sell", order.filled_quantity, order.average_price, now, exit_order.reason)
        remaining = position.quantity - order.filled_quantity
        logger.info(
            "exit filled",
            extra={"symbol": position.symbol, "quantity": order.filled_quantity, "price": order.average_price, "pnl": pnl, "remaining": remaining},
        )
        return replace(
            state,
            position=None if remaining <= 0 else replace(position, quantity=remaining),
            pending_exit=None,
            daily_pnl=state.daily_pnl + pnl,
            fills=state.fills + (fill,),
        )
    if order.status in DEAD:
        reason = (
            f"exit order {exit_order.order_id} ended {order.status} without a fill; "
            f"{position.quantity} shares of {position.symbol} are still held, use the kill switch or flatten in moomoo"
        )
        logger.error("exit order died", extra={"order_id": exit_order.order_id, "status": order.status, "quantity": position.quantity})
        return replace(state, pending_exit=None, halted=reason)
    return replace(state, pending_exit=replace(exit_order, checked_at=now))


def _place_exit(state: ExecutionState, broker: Broker, now: pd.Timestamp, reason: str) -> ExecutionState:
    position = state.position
    order_id = broker.sell_market(position.symbol, position.quantity)
    logger.info("exit placed", extra={"symbol": position.symbol, "quantity": position.quantity, "reason": reason, "order_id": order_id})
    return replace(state, pending_exit=PendingExit(order_id, position.quantity, reason, now))


def _request_cancel(state: ExecutionState, broker: Broker) -> ExecutionState:
    broker.cancel(state.pending_order.order_id)
    logger.info("entry cancel requested", extra={"order_id": state.pending_order.order_id})
    return replace(state, pending_order=replace(state.pending_order, cancel_requested=True))


FILLS_JOURNAL = Path(__file__).parent / "logs" / "fills.csv"
FILL_COLUMNS = ["at", "side", "quantity", "price", "reason"]


def append_fill(path: Path, fill: Fill) -> None:
    """Append one fill to the CSV journal, writing the header on first use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FILL_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow({"at": fill.at.isoformat(), "side": fill.side, "quantity": fill.quantity, "price": fill.price, "reason": fill.reason})


def load_fills(path: Path) -> list[Fill]:
    with path.open(newline="") as handle:
        return [
            Fill(row["side"], int(row["quantity"]), float(row["price"]), pd.Timestamp(row["at"]), row["reason"])
            for row in csv.DictReader(handle)
        ]


def restore_day(state: ExecutionState, fills: list[Fill], today: datetime.date) -> ExecutionState:
    """Seed today's trade count and realised P&L from journaled fills so a restart cannot reset the daily limits."""
    todays = tuple(fill for fill in fills if fill.at.date() == today)
    bought = sum(fill.price * fill.quantity for fill in todays if fill.side == "buy")
    sold = sum(fill.price * fill.quantity for fill in todays if fill.side == "sell")
    entries = sum(1 for fill in todays if fill.side == "buy")
    return replace(state, day=today, trades_today=entries, daily_pnl=sold - bought, fills=todays)


def _journaled(before: ExecutionState, after: ExecutionState) -> ExecutionState:
    for fill in after.fills[len(before.fills):]:
        append_fill(FILLS_JOURNAL, fill)
    return after


def _roll_day(state: ExecutionState, snapshot: Snapshot) -> ExecutionState:
    """Reset the daily counters when the candle belongs to a new session."""
    if snapshot.setup is None or snapshot.setup.timestamp.date() == state.day:
        return state
    return replace(state, day=snapshot.setup.timestamp.date(), trades_today=0, daily_pnl=0.0, last_skip=None)


def step(
    state: ExecutionState,
    store: SignalStore,
    broker: Broker,
    symbol: str,
    now: pd.Timestamp,
    limits: RiskLimits,
    entry_gate: Optional[EntryGate] = None,
) -> ExecutionState:
    """Apply one round of decisions, journal any fills, and return the new state."""
    return _journaled(state, _step(state, store, broker, symbol, now, limits, entry_gate))


def _step(
    state: ExecutionState,
    store: SignalStore,
    broker: Broker,
    symbol: str,
    now: pd.Timestamp,
    limits: RiskLimits,
    entry_gate: Optional[EntryGate] = None,
) -> ExecutionState:
    if state.pending_order is not None:
        state = _settle_pending(state, broker, symbol, now)
    if state.pending_exit is not None:
        state = _settle_exit(state, broker, now)

    snapshot = store.latest()
    quote = store.latest_quote()
    if snapshot is None or quote is None:
        return state
    state = _roll_day(state, snapshot)
    account = functools.cache(broker.account)
    decision = decide(state, snapshot, quote, now, account, limits)

    if isinstance(decision, Enter):
        plan = decision.plan
        if entry_gate is not None:
            approved, reason = entry_gate(snapshot, now)
            if approved is None:
                return replace(state, last_skip=f"{setup_time(snapshot)} waiting: {reason}")
            if not approved:
                logger.info("entry rejected by Jev", extra={"candle": plan.candle.isoformat(), "reason": reason})
                # Do not consume the candle: the five-second review may change as the live candle develops.
                return replace(state, last_skip=f"{setup_time(snapshot)} {reason}")
        order_id = broker.buy_limit(plan.symbol, plan.quantity, plan.limit_price)
        logger.info("entry placed", extra={"symbol": plan.symbol, "quantity": plan.quantity, "limit": plan.limit_price, "order_id": order_id})
        pending = PendingOrder(order_id, plan.quantity, plan.limit_price, plan.stop, plan.target, now)
        return replace(state, pending_order=pending, last_candle=plan.candle)
    if isinstance(decision, CancelEntry):
        return _request_cancel(state, broker)
    if isinstance(decision, Exit):
        return _place_exit(state, broker, now, decision.reason)
    if state.halted is None and state.position is None and snapshot.setup is not None and state.last_candle != snapshot.setup.timestamp:
        setup = snapshot.setup
        skip = state.last_skip
        if setup.signal in BUY_SIGNALS:
            if state.pending_order is not None:
                reason = "entry order pending"
            else:
                reason = entry_block_reason(setup, account().equity, account().cash, state.trades_today, state.daily_pnl, limits)
            skip = f"{setup.timestamp.strftime('%H:%M')} {setup.signal} skipped: {reason}"
            logger.info("entry skipped", extra={"candle": setup.timestamp.isoformat(), "reason": reason})
        return replace(state, last_candle=setup.timestamp, last_skip=skip)
    return state


def flatten(state: ExecutionState, broker: Broker, symbol: str, now: pd.Timestamp, reason: str) -> ExecutionState:
    """One pass toward flat: cancel the entry order, sell the position, settle whatever is still open, and halt.

    Cancels and fills land asynchronously at the broker, so the caller repeats
    this until ``is_flat`` holds; each order is cancelled or sold only once.
    """
    return _journaled(state, _flatten(state, broker, symbol, now, reason))


def _flatten(state: ExecutionState, broker: Broker, symbol: str, now: pd.Timestamp, reason: str) -> ExecutionState:
    state = replace(state, halted=reason)
    if state.pending_order is not None:
        if not state.pending_order.cancel_requested:
            return _request_cancel(state, broker)
        state = _settle_pending(state, broker, symbol, now)
    if state.pending_exit is not None:
        state = _settle_exit(state, broker, now)
    if state.position is not None and state.pending_exit is None:
        return _place_exit(state, broker, now, reason)
    return state


def setup_time(snapshot: Snapshot) -> str:
    return snapshot.setup.timestamp.strftime("%H:%M") if snapshot.setup is not None else "unknown candle"


def executor_pass(
    state: ExecutionState,
    store: SignalStore,
    broker: Broker,
    symbol: str,
    now: pd.Timestamp,
    limits: RiskLimits,
    entry_gate: Optional[EntryGate],
) -> ExecutionState:
    """One loop pass: keep flattening while the kill switch is pulled and anything is open, otherwise step.

    A broker failure is recorded in ``broker_error`` and the same state is
    retried on the next pass; risk management is never abandoned silently.
    """
    try:
        if store.kill_switch_pulled() and (state.halted is None or not is_flat(state)):
            after = flatten(state, broker, symbol, now, "kill switch")
        else:
            after = step(state, store, broker, symbol, now, limits, entry_gate)
    except ConnectionError as exc:
        logger.warning("broker call failed; retrying on the next pass", extra={"symbol": symbol, "error": str(exc)})
        return replace(state, broker_error=str(exc))
    return replace(after, broker_error=None)


def run_executor_forever(
    store: SignalStore,
    broker: Broker,
    symbol: str,
    limits: RiskLimits,
    interval_seconds: float,
    entry_gate: Optional[EntryGate] = None,
) -> None:
    """Poll the store and act; today's counters come from the fills journal and the kill switch flattens and halts."""
    state = ExecutionState.fresh()
    if FILLS_JOURNAL.exists():
        state = restore_day(state, load_fills(FILLS_JOURNAL), pd.Timestamp.now(tz=NEW_YORK).date())
    existing = broker.position_quantity(symbol)
    if existing > 0:
        state = replace(state, halted=f"{symbol} already has {existing} shares in the account; flatten it first")
    store.publish_execution(state)
    while True:
        try:
            state = executor_pass(state, store, broker, symbol, pd.Timestamp.now(tz=NEW_YORK), limits, entry_gate)
        except Exception as exc:
            store.publish_execution(replace(state, halted=f"executor crashed: {exc!r}"))
            raise
        store.publish_execution(state)
        time.sleep(interval_seconds)
