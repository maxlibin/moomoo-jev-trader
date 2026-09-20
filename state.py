"""Thread-safe handoff of the latest evaluated setup and live quote to the dashboard.

The minute loop publishes a ``Snapshot`` once per minute and the quote loop
publishes a ``LiveQuote`` every couple of seconds; the Flask thread reads both.
``STORE`` is the single shared instance for the process.
"""

import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from signals import Setup


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    benchmark: str
    published_at: pd.Timestamp
    setup: Optional[Setup]
    session: Optional[pd.DataFrame]
    error: Optional[str]


@dataclass(frozen=True)
class Quote:
    """Last traded price as reported by the feed."""

    symbol: str
    price: float
    quoted_at: pd.Timestamp


@dataclass(frozen=True)
class Candle:
    """One-minute candle, ``start`` is the minute it covers."""

    start: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class LiveQuote:
    symbol: str
    price: Optional[float]
    quoted_at: Optional[pd.Timestamp]
    forming: Optional[Candle]
    published_at: pd.Timestamp
    error: Optional[str]


FUTURE_STAMP_TOLERANCE_SECONDS = 2.0


def quote_age_seconds(quote: LiveQuote, now: pd.Timestamp) -> Optional[float]:
    """Seconds from the exchange stamp to ``now``, or None when the feed gave no stamp."""
    if quote.quoted_at is None:
        return None
    return (now - quote.quoted_at).total_seconds()


def stale_quote_reason(quote: LiveQuote, now: pd.Timestamp, max_age_seconds: float) -> Optional[str]:
    """Why the quote is not current: the feed failed, the stamp is older than the limit, or it is ahead of the clock.

    The exchange stamp, not the gateway's answer, decides: OpenD repeats its
    last quote during an upstream outage and serves old stamps on a delayed
    entitlement, and a stamp more than ``FUTURE_STAMP_TOLERANCE_SECONDS`` ahead
    of ``now`` means a clock or time zone is wrong. The executor and the Jev
    loop share this rule so they never disagree about the same quote.
    """
    age = quote_age_seconds(quote, now)
    if quote.price is None or age is None:
        return f"no live quote: {quote.error}" if quote.error is not None else "no live quote"
    if age < -FUTURE_STAMP_TOLERANCE_SECONDS:
        return f"quote is stamped {-age:.0f}s in the future; check the clock and time zone"
    if age > max_age_seconds:
        return f"quote is {age:.0f}s old, over the {max_age_seconds:.0f}s limit"
    return None


class SignalStore:
    """Holds the most recent snapshot, live quote, and execution state behind a lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: Optional[Snapshot] = None
        self._quote: Optional[LiveQuote] = None
        self._execution: object = None
        self._jev_review: Optional[dict] = None
        self._jev_reviews: deque[dict] = deque(maxlen=100)
        self._jev_completed = 0
        self._kill_switch = False

    def publish_execution(self, execution: object) -> None:
        with self._lock:
            self._execution = execution

    def publish_jev(self, review: dict) -> None:
        with self._lock:
            self._jev_review = review
            if review.get("status") == "complete":
                self._jev_reviews.append(review)
                self._jev_completed += 1

    def latest_jev(self) -> Optional[dict]:
        with self._lock:
            return self._jev_review

    def jev_history(self) -> list[dict]:
        """The most recent completed reviews, as many as the dashboard chart and feed display."""
        with self._lock:
            return list(self._jev_reviews)

    def jev_completed(self) -> int:
        """Completed reviews since start-up, beyond what ``jev_history`` retains."""
        with self._lock:
            return self._jev_completed

    def latest_execution(self) -> object:
        with self._lock:
            return self._execution

    def pull_kill_switch(self) -> None:
        with self._lock:
            self._kill_switch = True

    def kill_switch_pulled(self) -> bool:
        with self._lock:
            return self._kill_switch

    def publish(self, snapshot: Snapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def latest(self) -> Optional[Snapshot]:
        with self._lock:
            return self._snapshot

    def publish_quote(self, quote: LiveQuote) -> None:
        with self._lock:
            self._quote = quote

    def latest_quote(self) -> Optional[LiveQuote]:
        with self._lock:
            return self._quote


STORE = SignalStore()
