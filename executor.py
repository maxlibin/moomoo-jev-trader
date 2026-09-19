"""Execution state machine: pure ``decide`` plus a thin ``step`` that applies decisions through a broker.

``decide`` looks at the execution state, the latest closed-candle snapshot, the
live quote, and the account, and returns exactly one decision. ``step`` carries
it out against a ``Broker`` and returns the new state. ``run_executor_forever``
polls the store every couple of seconds.
"""

import csv
import datetime
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
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    quantity: int
    limit_price: float
    stop: float
    target: float
    placed_at: pd.Timestamp


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

    @staticmethod
    def fresh() -> "ExecutionState":
        return ExecutionState(position=None, pending_order=None, trades_today=0, daily_pnl=0.0, last_candle=None, halted=None)


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
    equity: float,
    cash: float,
    limits: RiskLimits,
) -> Decision:
    if state.halted is not None:
        return Nothing()
    if state.pending_order is not None:
        if (now - state.pending_order.placed_at).total_seconds() > limits.entry_timeout_seconds:
            return CancelEntry(state.pending_order.order_id)
        return Nothing()
    if state.position is not None:
        if quote.price is None:
            return Nothing()
        reason = exit_reason(state.position, quote.price, now, limits.cutoff)
        return Exit(reason) if reason is not None else Nothing()
    if snapshot.setup is None or snapshot.setup.timestamp == state.last_candle:
        return Nothing()
    plan = plan_entry(snapshot.setup, snapshot.symbol, equity, cash, state.trades_today, state.daily_pnl, limits)
    return Enter(plan) if plan is not None else Nothing()


FILLED = {"FILLED_ALL"}
DEAD = {"CANCELLED_ALL", "CANCELLED_PART", "FAILED", "SUBMIT_FAILED"}


def _settle_pending(state: ExecutionState, broker: Broker, symbol: str, now: pd.Timestamp) -> ExecutionState:
    """Turn a filled entry order into a position, or drop a dead one."""
    pending = state.pending_order
    order = broker.order(pending.order_id)
    if order.status in FILLED or (order.status in DEAD and order.filled_quantity > 0):
        position = OpenPosition(symbol, order.filled_quantity, order.average_price, pending.stop, pending.target, now)
        fill = Fill("buy", order.filled_quantity, order.average_price, now, "entry")
        logger.info("entry filled", extra={"symbol": symbol, "quantity": order.filled_quantity, "price": order.average_price})
        return replace(state, position=position, pending_order=None, trades_today=state.trades_today + 1, fills=state.fills + (fill,))
    if order.status in DEAD:
        logger.info("entry order ended without fill", extra={"order_id": pending.order_id, "status": order.status})
        return replace(state, pending_order=None)
    return state


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


def _journal_new_fills(before: ExecutionState, after: ExecutionState) -> None:
    for fill in after.fills[len(before.fills):]:
        append_fill(FILLS_JOURNAL, fill)


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
    after = _step(state, store, broker, symbol, now, limits, entry_gate)
    _journal_new_fills(state, after)
    return after


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

    snapshot = store.latest()
    quote = store.latest_quote()
    if snapshot is None or quote is None:
        return state
    state = _roll_day(state, snapshot)
    account = broker.account()
    decision = decide(state, snapshot, quote, now, account.equity, account.cash, limits)

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
        broker.cancel(decision.order_id)
        return _settle_pending(replace(state, pending_order=state.pending_order), broker, symbol, now)
    if isinstance(decision, Exit):
        position = state.position
        order_id = broker.sell_market(position.symbol, position.quantity)
        order = broker.order(order_id)
        price = order.average_price if order.filled_quantity > 0 else quote.price
        pnl = (price - position.entry_price) * position.quantity
        fill = Fill("sell", position.quantity, price, now, decision.reason)
        logger.info("exit placed", extra={"symbol": symbol, "reason": decision.reason, "price": price, "pnl": pnl})
        return replace(state, position=None, daily_pnl=state.daily_pnl + pnl, fills=state.fills + (fill,))
    if snapshot.setup is not None and state.last_candle != snapshot.setup.timestamp and state.position is None:
        setup = snapshot.setup
        skip = state.last_skip
        if setup.signal in BUY_SIGNALS:
            reason = entry_block_reason(setup, account.equity, account.cash, state.trades_today, state.daily_pnl, limits)
            skip = f"{setup.timestamp.strftime('%H:%M')} {setup.signal} skipped: {reason}"
            logger.info("entry skipped", extra={"candle": setup.timestamp.isoformat(), "reason": reason})
        return replace(state, last_candle=setup.timestamp, last_skip=skip)
    return state


def flatten(state: ExecutionState, broker: Broker, symbol: str, now: pd.Timestamp, reason: str) -> ExecutionState:
    """Cancel any entry order, sell any position, and halt until restart."""
    if state.pending_order is not None:
        broker.cancel(state.pending_order.order_id)
        state = _settle_pending(state, broker, symbol, now)
    if state.position is not None:
        order_id = broker.sell_market(symbol, state.position.quantity)
        order = broker.order(order_id)
        price = order.average_price if order.filled_quantity > 0 else state.position.entry_price
        pnl = (price - state.position.entry_price) * state.position.quantity
        fill = Fill("sell", state.position.quantity, price, now, reason)
        state = replace(state, position=None, daily_pnl=state.daily_pnl + pnl, fills=state.fills + (fill,))
    return replace(state, halted=reason)


def setup_time(snapshot: Snapshot) -> str:
    return snapshot.setup.timestamp.strftime("%H:%M") if snapshot.setup is not None else "unknown candle"


def run_executor_forever(
    store: SignalStore,
    broker: Broker,
    symbol: str,
    limits: RiskLimits,
    interval_seconds: float,
    entry_gate: Optional[EntryGate] = None,
) -> None:
    """Poll the store and act; the store's kill switch flattens and halts."""
    state = ExecutionState.fresh()
    existing = broker.position_quantity(symbol)
    if existing > 0:
        state = replace(state, halted=f"{symbol} already has {existing} shares in the account; flatten it first")
    store.publish_execution(state)
    while True:
        now = pd.Timestamp.now(tz=NEW_YORK)
        if store.kill_switch_pulled() and state.halted is None:
            state = flatten(state, broker, symbol, now, "kill switch")
        else:
            state = step(state, store, broker, symbol, now, limits, entry_gate)
        store.publish_execution(state)
        time.sleep(interval_seconds)
