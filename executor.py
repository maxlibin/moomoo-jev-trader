"""Execution state machine: pure ``decide`` plus a thin ``step`` that applies decisions through a broker.

``decide`` looks at the execution state, the latest closed-candle snapshot, the
live quote, and the account, and returns exactly one decision. A quote older
than ``limits.max_quote_age_seconds`` never drives an entry, a stop, or a
target; only the session cutoff acts without a fresh price. ``step`` carries
the decision out against a ``Broker`` and returns the new state. Entry and exit
orders are tracked until the broker reports them filled or dead, so the state
never claims to be flat while shares are still held. An order placement that
OpenD does not confirm is reconciled against the broker's order list before
anything is sent again. ``run_executor_forever`` polls the store every couple
of seconds, records failures instead of dying on them, and keeps flattening
while the kill switch is pulled until nothing is left open.
"""

import csv
import datetime
import functools
import logging
import math
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional, Protocol, Union

import pandas as pd

from state import LiveQuote, SignalStore, Snapshot, quote_age_seconds
from signals import BUY_SIGNALS
from trader import OpenPosition, RiskLimits, TradePlan, entry_block_reason, exit_reason, plan_entry


NEW_YORK = "America/New_York"
ORDER_POLL_SECONDS = 4.0
MAX_EXIT_ATTEMPTS = 3
EXIT_RETRY_SECONDS = 30.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingOrder:
    """An entry order in flight; ``order_id`` is None until OpenD has confirmed the placement.

    ``status`` is the last status the broker reported for it, so an order whose
    result OpenD does not know (``TIMEOUT``) is visible while it is re-read.
    """

    order_id: Optional[str]
    quantity: int
    limit_price: float
    stop: float
    target: float
    placed_at: pd.Timestamp
    cancel_requested: bool = False
    checked_at: Optional[pd.Timestamp] = None
    status: Optional[str] = None


@dataclass(frozen=True)
class PendingExit:
    """A sell order in flight; ``order_id`` is None until OpenD has confirmed the placement."""

    order_id: Optional[str]
    quantity: int
    reason: str
    placed_at: pd.Timestamp
    checked_at: Optional[pd.Timestamp] = None
    status: Optional[str] = None


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
    known_orders: frozenset[str] = frozenset()
    exit_failures: int = 0
    last_exit_failure: Optional[pd.Timestamp] = None

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


@dataclass(frozen=True)
class BrokerOrder:
    """One of today's orders for a symbol as the broker reports it."""

    order_id: str
    side: str
    quantity: int
    price: float
    status: str
    filled_quantity: int
    average_price: float


@dataclass(frozen=True)
class Holding:
    quantity: int
    cost_price: float


class Broker(Protocol):
    def account(self) -> Account: ...
    def holding(self, symbol: str) -> Holding: ...
    def orders(self, symbol: str) -> list[BrokerOrder]: ...
    def buy_limit(self, symbol: str, quantity: int, price: float) -> str: ...
    def sell_market(self, symbol: str, quantity: int) -> str: ...
    def order(self, order_id: str) -> OrderState: ...
    def cancel(self, order_id: str) -> None: ...


def entry_window_open(state: ExecutionState, snapshot: Snapshot) -> bool:
    """A new BUY candle the executor is free to act on: not halted, flat, nothing in flight, not yet consumed."""
    setup = snapshot.setup
    return (
        state.halted is None
        and is_flat(state)
        and setup is not None
        and setup.timestamp != state.last_candle
        and setup.signal in BUY_SIGNALS
    )


def quote_block_reason(quote: LiveQuote, now: pd.Timestamp, max_age_seconds: float) -> Optional[str]:
    """Why the live price must not drive an entry, stop, or target: the feed failed, or the exchange stamp is too old.

    OpenD answers with its last known quote during an upstream outage and with
    delayed data on a delayed entitlement, so the exchange stamp, not the
    gateway's answer, decides whether the price is current.
    """
    age = quote_age_seconds(quote, now)
    if quote.price is None or age is None:
        return f"no live quote: {quote.error}" if quote.error is not None else "no live quote"
    if age > max_age_seconds:
        return f"quote is {age:.0f}s old, over the {max_age_seconds:.0f}s limit"
    return None


def usable_price(quote: LiveQuote, now: pd.Timestamp, max_age_seconds: float) -> Optional[float]:
    """The live price when it is fresh enough to act on, otherwise None."""
    return None if quote_block_reason(quote, now, max_age_seconds) is not None else quote.price


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
        timed_out = (now - pending.placed_at).total_seconds() > limits.entry_timeout_seconds
        if pending.order_id is not None and not pending.cancel_requested and timed_out:
            return CancelEntry(pending.order_id)
        return Nothing()
    price = usable_price(quote, now, limits.max_quote_age_seconds)
    if state.position is not None:
        reason = exit_reason(state.position, price, now, limits.cutoff)
        return Exit(reason) if reason is not None else Nothing()
    if price is None or not entry_window_open(state, snapshot):
        return Nothing()
    funds = account()
    plan = plan_entry(snapshot.setup, snapshot.symbol, funds.equity, funds.cash, state.trades_today, state.daily_pnl, limits)
    return Enter(plan) if plan is not None else Nothing()


FILLED = {"FILLED_ALL"}
DEAD = {"CANCELLED_ALL", "CANCELLED_PART", "FAILED", "SUBMIT_FAILED", "DISABLED", "DELETED", "FILL_CANCELLED"}
UNKNOWN = {"TIMEOUT"}


def _poll_due(checked_at: Optional[pd.Timestamp], now: pd.Timestamp) -> bool:
    """Order queries are rate limited by OpenD, so an open order is re-read every ``ORDER_POLL_SECONDS`` at most."""
    return checked_at is None or (now - checked_at).total_seconds() >= ORDER_POLL_SECONDS


def _filled_quantity(order: OrderState) -> int:
    """Shares that actually changed hands; a FILL_CANCELLED order's fill was rolled back by the exchange."""
    return 0 if order.status == "FILL_CANCELLED" else order.filled_quantity


def _recover_order(
    orders: list[BrokerOrder], side: str, quantity: int, price: Optional[float], known: frozenset[str]
) -> Optional[BrokerOrder]:
    """The newest broker order matching an unconfirmed placement, if OpenD accepted it after all.

    ``known`` holds every order id seen before the placement was sent (orders
    from earlier processes, manual orders, and the executor's own), so only an
    order that appeared afterwards can be adopted.
    """
    for order in orders:
        if order.order_id in known or order.side != side or order.quantity != quantity:
            continue
        if price is not None and abs(order.price - price) >= 0.005:
            continue
        return order
    return None


def _recover_entry(state: ExecutionState, broker: Broker, symbol: str) -> ExecutionState:
    """Adopt an unconfirmed entry order the broker did accept; otherwise skip the candle, never re-place it."""
    pending = state.pending_order
    match = _recover_order(broker.orders(symbol), "BUY", pending.quantity, pending.limit_price, state.known_orders)
    if match is None:
        logger.warning("entry placement was not accepted; candle skipped", extra={"symbol": symbol, "quantity": pending.quantity})
        return replace(state, pending_order=None, last_skip=f"{state.last_candle.strftime('%H:%M')} entry was not accepted by OpenD; not retried")
    logger.info("entry order recovered from the broker", extra={"symbol": symbol, "order_id": match.order_id})
    return replace(state, pending_order=replace(pending, order_id=match.order_id), known_orders=state.known_orders | {match.order_id})


def _settle_pending(state: ExecutionState, broker: Broker, symbol: str, now: pd.Timestamp) -> ExecutionState:
    """Turn a filled entry order into a position, or drop a dead one."""
    pending = state.pending_order
    if not _poll_due(pending.checked_at, now):
        return state
    if pending.order_id is None:
        return _recover_entry(state, broker, symbol)
    order = broker.order(pending.order_id)
    filled = _filled_quantity(order)
    if order.status in FILLED or (order.status in DEAD and filled > 0):
        position = OpenPosition(symbol, filled, order.average_price, pending.stop, pending.target, now)
        fill = Fill("buy", filled, order.average_price, now, "entry")
        logger.info("entry filled", extra={"symbol": symbol, "quantity": filled, "price": order.average_price})
        return replace(state, position=position, pending_order=None, trades_today=state.trades_today + 1, fills=state.fills + (fill,))
    if order.status in DEAD:
        logger.info("entry order ended without fill", extra={"order_id": pending.order_id, "status": order.status})
        return replace(state, pending_order=None)
    if order.status in UNKNOWN:
        logger.warning("entry order result unknown at OpenD; re-reading until it settles", extra={"order_id": pending.order_id, "status": order.status})
    return replace(state, pending_order=replace(pending, checked_at=now, status=order.status))


def _exit_retry_block(state: ExecutionState, now: pd.Timestamp) -> Optional[str]:
    """Why another sell must not be sent now: too many rejected attempts, or the pause after the last one is still running."""
    position = state.position
    if state.exit_failures >= MAX_EXIT_ATTEMPTS:
        return (
            f"{state.exit_failures} sell attempts for {position.quantity} shares of {position.symbol} were rejected; "
            f"no more will be sent, flatten in moomoo and restart"
        )
    if state.exit_failures >= 2:
        wait = EXIT_RETRY_SECONDS - (now - state.last_exit_failure).total_seconds()
        if wait > 0:
            return f"sell for {position.quantity} shares of {position.symbol} was rejected {state.exit_failures} times; next attempt in {wait:.0f}s"
    return None


def _exit_failed(state: ExecutionState, now: pd.Timestamp) -> ExecutionState:
    return replace(state, pending_exit=None, exit_failures=state.exit_failures + 1, last_exit_failure=now)


def _recover_exit(state: ExecutionState, broker: Broker, now: pd.Timestamp) -> ExecutionState:
    """Adopt an unconfirmed sell the broker did accept; re-place it only once the broker shows every tracked share still held."""
    position = state.position
    exit_order = state.pending_exit
    match = _recover_order(broker.orders(position.symbol), "SELL", exit_order.quantity, None, state.known_orders)
    if match is not None:
        logger.info("exit order recovered from the broker", extra={"symbol": position.symbol, "order_id": match.order_id})
        return replace(state, pending_exit=replace(exit_order, order_id=match.order_id), known_orders=state.known_orders | {match.order_id})
    held = broker.holding(position.symbol).quantity
    if held < position.quantity:
        reason = (
            f"{position.quantity - held} of {position.quantity} tracked shares of {position.symbol} left the account outside "
            f"the executor while a sell was unconfirmed; their P&L is unknown, reconcile in moomoo"
        )
        logger.error("position changed outside the executor", extra={"symbol": position.symbol, "tracked": position.quantity, "held": held})
        remaining = None if held <= 0 else replace(position, quantity=held)
        return replace(state, position=remaining, pending_exit=None, halted=reason)
    failed = _exit_failed(state, now)
    block = _exit_retry_block(failed, now)
    if block is not None:
        logger.error("exit placement was not accepted; not sending another", extra={"symbol": position.symbol, "reason": block})
        return replace(failed, halted=block)
    logger.info("exit placement was not accepted; placing again", extra={"symbol": position.symbol, "quantity": position.quantity})
    return _place_exit(failed, broker, now, exit_order.reason)


def _settle_exit(state: ExecutionState, broker: Broker, now: pd.Timestamp) -> ExecutionState:
    """Book a filled exit order against the position; halt with the shares still held when it died unfilled."""
    exit_order = state.pending_exit
    position = state.position
    if not _poll_due(exit_order.checked_at, now):
        return state
    if exit_order.order_id is None:
        return _recover_exit(state, broker, now)
    order = broker.order(exit_order.order_id)
    filled = _filled_quantity(order)
    if filled > 0 and (order.status in FILLED or order.status in DEAD):
        pnl = (order.average_price - position.entry_price) * filled
        fill = Fill("sell", filled, order.average_price, now, exit_order.reason)
        remaining = position.quantity - filled
        logger.info(
            "exit filled",
            extra={"symbol": position.symbol, "quantity": filled, "price": order.average_price, "pnl": pnl, "remaining": remaining},
        )
        return replace(
            state,
            position=None if remaining <= 0 else replace(position, quantity=remaining),
            pending_exit=None,
            daily_pnl=state.daily_pnl + pnl,
            fills=state.fills + (fill,),
            exit_failures=0,
            last_exit_failure=None,
        )
    if order.status in DEAD:
        reason = (
            f"exit order {exit_order.order_id} ended {order.status} without a fill; "
            f"{position.quantity} shares of {position.symbol} are still held, use the kill switch or flatten in moomoo"
        )
        logger.error("exit order died", extra={"order_id": exit_order.order_id, "status": order.status, "quantity": position.quantity})
        return replace(_exit_failed(state, now), halted=reason)
    if order.status in UNKNOWN:
        logger.warning("exit order result unknown at OpenD; re-reading until it settles", extra={"order_id": exit_order.order_id, "status": order.status})
    return replace(state, pending_exit=replace(exit_order, checked_at=now, status=order.status))


def _orders_before(state: ExecutionState, broker: Broker, symbol: str) -> frozenset[str]:
    """Every order id the broker already reports, so a placement sent next can be told apart from all of them."""
    return state.known_orders | {order.order_id for order in broker.orders(symbol)}


def _place_exit(state: ExecutionState, broker: Broker, now: pd.Timestamp, reason: str) -> ExecutionState:
    """Send the market sell; when OpenD does not confirm it, remember the attempt so it is reconciled, never blindly repeated."""
    position = state.position
    known = _orders_before(state, broker, position.symbol)
    try:
        order_id = broker.sell_market(position.symbol, position.quantity)
    except ConnectionError as exc:
        logger.warning("exit placement unconfirmed; reconciling with the broker next pass", extra={"symbol": position.symbol, "error": str(exc)})
        unconfirmed = PendingExit(None, position.quantity, reason, now, checked_at=now)
        return replace(state, pending_exit=unconfirmed, known_orders=known, broker_error=str(exc))
    logger.info("exit placed", extra={"symbol": position.symbol, "quantity": position.quantity, "reason": reason, "order_id": order_id})
    return replace(state, pending_exit=PendingExit(order_id, position.quantity, reason, now), known_orders=known | {order_id})


def _place_entry(state: ExecutionState, broker: Broker, plan: TradePlan, now: pd.Timestamp) -> ExecutionState:
    """Send the limit buy; when OpenD does not confirm it, remember the attempt so it is reconciled, never blindly repeated."""
    known = _orders_before(state, broker, plan.symbol)
    try:
        order_id = broker.buy_limit(plan.symbol, plan.quantity, plan.limit_price)
    except ConnectionError as exc:
        logger.warning("entry placement unconfirmed; reconciling with the broker next pass", extra={"symbol": plan.symbol, "error": str(exc)})
        unconfirmed = PendingOrder(None, plan.quantity, plan.limit_price, plan.stop, plan.target, now, checked_at=now)
        return replace(state, pending_order=unconfirmed, last_candle=plan.candle, known_orders=known, broker_error=str(exc))
    logger.info("entry placed", extra={"symbol": plan.symbol, "quantity": plan.quantity, "limit": plan.limit_price, "order_id": order_id})
    pending = PendingOrder(order_id, plan.quantity, plan.limit_price, plan.stop, plan.target, now)
    return replace(state, pending_order=pending, last_candle=plan.candle, known_orders=known | {order_id})


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


def matched_pnl(fills: tuple[Fill, ...]) -> tuple[float, int]:
    """Realised P&L of sells matched first-in-first-out against buys, and how many bought shares have no journaled sell."""
    lots: list[tuple[int, float]] = []
    pnl = 0.0
    for fill in fills:
        if fill.side == "buy":
            lots.append((fill.quantity, fill.price))
            continue
        remaining = fill.quantity
        while remaining > 0 and lots:
            lot_quantity, lot_price = lots[0]
            taken = min(remaining, lot_quantity)
            pnl += (fill.price - lot_price) * taken
            remaining -= taken
            lots = lots[1:] if taken == lot_quantity else [(lot_quantity - taken, lot_price)] + lots[1:]
    return pnl, sum(quantity for quantity, _ in lots)


def restore_day(state: ExecutionState, fills: list[Fill], today: datetime.date) -> ExecutionState:
    """Seed today's trade count and matched realised P&L from journaled fills so a restart cannot reset the daily limits.

    A buy without a journaled sell has unknown P&L, so the daily loss limit
    cannot be trusted and the day is blocked until the journal is corrected.
    """
    todays = tuple(fill for fill in fills if fill.at.date() == today)
    pnl, unmatched = matched_pnl(todays)
    entries = sum(1 for fill in todays if fill.side == "buy")
    restored = replace(state, day=today, trades_today=entries, daily_pnl=pnl, fills=todays)
    if unmatched == 0:
        return restored
    reason = (
        f"{FILLS_JOURNAL.name} shows {unmatched} shares bought today without a journaled exit, so today's P&L is unknown; "
        f"correct or move {FILLS_JOURNAL} and restart to trade again"
    )
    return replace(restored, halted=reason)


def initial_state(broker: Broker, symbol: str, now: pd.Timestamp) -> ExecutionState:
    """Start-up state: today's counters from the fills journal, and shares already held adopted as a halted position.

    Every order the broker already reports is marked as known so that a later
    reconciliation can never adopt one from a previous process or the moomoo
    app. The adopted position has no stop or target; it exists so the kill
    switch can sell exactly what the broker reports, priced at the broker's cost.
    """
    state = ExecutionState.fresh()
    if FILLS_JOURNAL.exists():
        state = restore_day(state, load_fills(FILLS_JOURNAL), now.date())
    state = replace(state, known_orders=_orders_before(state, broker, symbol))
    holding = broker.holding(symbol)
    if holding.quantity > 0:
        position = OpenPosition(symbol, holding.quantity, holding.cost_price, 0.0, math.inf, now)
        reason = f"{symbol} already has {holding.quantity} shares in the account; the kill switch sells them, or flatten in moomoo and restart"
        state = replace(state, position=position, halted=reason)
    return state


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
    state = replace(state, broker_error=None)
    if state.pending_order is not None:
        state = _settle_pending(state, broker, symbol, now)
    if state.pending_exit is not None:
        state = _settle_exit(state, broker, now)

    snapshot = store.latest()
    quote = store.latest_quote()
    if snapshot is None or quote is None:
        return state
    state = _roll_day(state, snapshot)
    quote_block = quote_block_reason(quote, now, limits.max_quote_age_seconds)
    if quote_block is not None and entry_window_open(state, snapshot):
        return replace(state, last_skip=f"{setup_time(snapshot)} waiting: {quote_block}")
    if entry_gate is not None and entry_window_open(state, snapshot):
        approved, reason = entry_gate(snapshot, now)
        if approved is None:
            return replace(state, last_skip=f"{setup_time(snapshot)} waiting: {reason}")
        if not approved:
            logger.info("entry rejected by Jev", extra={"candle": snapshot.setup.timestamp.isoformat(), "reason": reason})
            # Do not consume the candle: the five-second review may change as the live candle develops.
            return replace(state, last_skip=f"{setup_time(snapshot)} {reason}")
    account = functools.cache(broker.account)
    decision = decide(state, snapshot, quote, now, account, limits)

    if isinstance(decision, Enter):
        return _place_entry(state, broker, decision.plan, now)
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
    """One pass toward flat: settle what is open, cancel the entry order once, sell the position once, and halt.

    Cancels and fills land asynchronously at the broker, so the caller repeats
    this until ``is_flat`` holds. A settle that halts for its own reason (a
    rejected sell, shares that left the account) ends the pass without sending
    anything new; a rejected sell is retried after a pause and at most
    ``MAX_EXIT_ATTEMPTS`` times before the halt asks for manual reconciliation.
    """
    return _journaled(state, _flatten(state, broker, symbol, now, reason))


def _flatten(state: ExecutionState, broker: Broker, symbol: str, now: pd.Timestamp, reason: str) -> ExecutionState:
    state = replace(state, halted=reason, broker_error=None)
    if state.pending_order is not None:
        state = _settle_pending(state, broker, symbol, now)
    pending = state.pending_order
    if pending is not None and pending.order_id is not None and not pending.cancel_requested:
        return _request_cancel(state, broker)
    if state.pending_exit is not None:
        state = _settle_exit(state, broker, now)
    if state.halted != reason or state.pending_exit is not None or state.position is None:
        return state
    block = _exit_retry_block(state, now)
    if block is not None:
        return replace(state, halted=f"{reason}: {block}")
    return _place_exit(state, broker, now, reason)


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

    A failed broker query is recorded in ``broker_error`` and the same state is
    retried on the next pass; a failed placement is recorded by the step itself
    as an unconfirmed order so that it is reconciled rather than repeated.
    """
    try:
        if store.kill_switch_pulled() and (state.halted is None or not is_flat(state)):
            return flatten(state, broker, symbol, now, "kill switch")
        return step(state, store, broker, symbol, now, limits, entry_gate)
    except ConnectionError as exc:
        logger.warning("broker call failed; retrying on the next pass", extra={"symbol": symbol, "error": str(exc)})
        return replace(state, broker_error=str(exc))


def run_executor_forever(
    store: SignalStore,
    broker: Broker,
    symbol: str,
    limits: RiskLimits,
    interval_seconds: float,
    entry_gate: Optional[EntryGate] = None,
) -> None:
    """Poll the store and act; today's counters come from the fills journal and the kill switch flattens and halts.

    Start-up queries that fail are retried every pass with the error on the
    dashboard. Any other failure is logged with its traceback and shown on the
    dashboard, and the same state is retried on the next pass: the thread never
    ends while the process runs, so the cutoff exit and the kill switch stay in
    service for whatever is held.
    """
    state: Optional[ExecutionState] = None
    while True:
        now = pd.Timestamp.now(tz=NEW_YORK)
        try:
            started = initial_state(broker, symbol, now) if state is None else executor_pass(state, store, broker, symbol, now, limits, entry_gate)
        except ConnectionError as exc:
            logger.warning("start-up query failed; retrying on the next pass", extra={"symbol": symbol, "error": str(exc)})
            store.publish_execution(replace(ExecutionState.fresh(), halted=f"starting up, OpenD query failed: {exc}"))
        except Exception as exc:
            logger.exception("executor pass failed; retrying on the next pass with the same state", extra={"symbol": symbol})
            if state is None:
                store.publish_execution(replace(ExecutionState.fresh(), halted=f"starting up, unexpected {exc!r}; retrying"))
            else:
                store.publish_execution(replace(state, broker_error=f"unexpected {exc!r}; the pass is retried"))
        else:
            state = started
            store.publish_execution(state)
        time.sleep(interval_seconds)
