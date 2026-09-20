"""Loops that turn the bar and quote feeds into published state for the dashboard.

``tick`` runs once per minute and scores the last completed candle; ``quote_tick``
runs every few seconds and publishes the live price plus the candle still forming.
Both take plain callables, which keeps them testable on saved bars and lets
``run_live`` plug in the Moomoo gateway.
"""

import logging
import math
import time
from typing import Callable, Optional

import pandas as pd

from signals import DEFAULT_CONFIG, completed_minute_bars, evaluate, indicator_frame
from state import Candle, LiveQuote, Quote, SignalStore, Snapshot


NEW_YORK = "America/New_York"
BarFetch = Callable[[str], pd.DataFrame]
QuoteFetch = Callable[[str], Quote]
logger = logging.getLogger(__name__)


def seconds_until_next_tick(now: pd.Timestamp, offset_seconds: int) -> int:
    """Whole seconds until ``offset_seconds`` past the next minute boundary."""
    next_tick = now.floor("min") + pd.Timedelta(minutes=1, seconds=offset_seconds)
    return math.ceil((next_tick - now).total_seconds())


def forming_candle(bars: pd.DataFrame, now: pd.Timestamp) -> Optional[Candle]:
    """The bar covering the current minute, or None when the feed has not started it yet."""
    start = now.floor("min")
    if start not in bars.index:
        return None
    row = bars.loc[start]
    return Candle(
        start=start,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=int(row["volume"]),
    )


def _feed_error(symbol: str, benchmark: str, now: pd.Timestamp, message: str) -> Snapshot:
    return Snapshot(symbol=symbol, benchmark=benchmark, published_at=now, setup=None, session=None, error=message)


def tick(fetch: BarFetch, symbol: str, benchmark: str, now: pd.Timestamp, store: SignalStore) -> None:
    """Evaluate the latest completed candle and publish the result or the feed error."""
    try:
        bars = completed_minute_bars(fetch(symbol), now)
        benchmark_bars = completed_minute_bars(fetch(benchmark), now)
    except (ConnectionError, ValueError) as exc:
        logger.warning("feed error", extra={"symbol": symbol, "error": str(exc)})
        store.publish(_feed_error(symbol, benchmark, now, str(exc)))
        return

    if bars.empty or benchmark_bars.empty:
        missing = symbol if bars.empty else benchmark
        store.publish(_feed_error(symbol, benchmark, now, f"Feed returned no completed one-minute bars for {missing}"))
        return

    setup = evaluate(bars, benchmark_bars, DEFAULT_CONFIG)
    store.publish(
        Snapshot(
            symbol=symbol,
            benchmark=benchmark,
            published_at=now,
            setup=setup,
            session=indicator_frame(bars, DEFAULT_CONFIG),
            error=None,
        )
    )
    logger.info(
        "setup",
        extra={
            "candle": setup.timestamp.isoformat(),
            "symbol": symbol,
            "price": setup.price,
            "signal": setup.signal,
            "buy_score": setup.buy_score,
            "sell_score": setup.sell_score,
        },
    )


def quote_tick(fetch_quote: QuoteFetch, fetch_bars: BarFetch, symbol: str, now: pd.Timestamp, store: SignalStore) -> None:
    """Publish the last price and the forming candle, or the feed error.

    ``ConnectionError`` is the gateway failing; ``ValueError`` is the gateway
    answering with a frame the converters reject. Both are published rather
    than raised so the quote thread outlives them.
    """
    try:
        quote = fetch_quote(symbol)
        forming = forming_candle(fetch_bars(symbol), now)
    except (ConnectionError, ValueError) as exc:
        logger.warning("quote error", extra={"symbol": symbol, "error": str(exc)})
        store.publish_quote(LiveQuote(symbol=symbol, price=None, quoted_at=None, forming=None, published_at=now, error=str(exc)))
        return
    store.publish_quote(
        LiveQuote(symbol=symbol, price=quote.price, quoted_at=quote.quoted_at, forming=forming, published_at=now, error=None)
    )


def run_forever(fetch: BarFetch, symbol: str, benchmark: str, store: SignalStore, offset_seconds: int) -> None:
    """Tick once now, then once every minute shortly after the boundary."""
    tick(fetch, symbol, benchmark, pd.Timestamp.now(tz=NEW_YORK), store)
    while True:
        time.sleep(seconds_until_next_tick(pd.Timestamp.now(tz=NEW_YORK), offset_seconds))
        tick(fetch, symbol, benchmark, pd.Timestamp.now(tz=NEW_YORK), store)


def run_quotes_forever(
    fetch_quote: QuoteFetch, fetch_bars: BarFetch, symbol: str, store: SignalStore, interval_seconds: float
) -> None:
    """Publish the live quote every ``interval_seconds``."""
    while True:
        quote_tick(fetch_quote, fetch_bars, symbol, pd.Timestamp.now(tz=NEW_YORK), store)
        time.sleep(interval_seconds)
