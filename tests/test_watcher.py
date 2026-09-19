"""The minute tick that turns a bar feed into a published snapshot."""

from pathlib import Path

import pandas as pd

from bars_csv import load_bars_csv
from state import SignalStore
from watcher import seconds_until_next_tick, tick


FIXTURES = Path(__file__).parent / "fixtures"
NEW_YORK = "America/New_York"


def fixture_feed(end: str):
    def fetch(symbol: str) -> pd.DataFrame:
        frame = load_bars_csv(FIXTURES / f"{symbol}_1m.csv")
        return frame.loc[frame.index <= pd.Timestamp(end, tz=NEW_YORK)]

    return fetch


def test_tick_publishes_the_last_completed_candle():
    store = SignalStore()
    now = pd.Timestamp("2026-09-15 15:00:02", tz=NEW_YORK)

    tick(fixture_feed("2026-09-15 15:00"), "SPCX", "QQQ", now, store)

    snapshot = store.latest()
    assert snapshot.error is None
    assert snapshot.setup.timestamp == pd.Timestamp("2026-09-15 14:59", tz=NEW_YORK)
    assert snapshot.published_at == now
    assert {"ema_fast", "ema_slow", "rsi", "vwap"} <= set(snapshot.session.columns)


def test_tick_publishes_a_feed_error_when_the_gateway_fails():
    store = SignalStore()
    now = pd.Timestamp("2026-09-15 15:00:02", tz=NEW_YORK)

    def failing_fetch(symbol: str) -> pd.DataFrame:
        raise ConnectionError(f"OpenD returned RET_ERROR for {symbol}: not subscribed")

    tick(failing_fetch, "SPCX", "QQQ", now, store)

    snapshot = store.latest()
    assert snapshot.setup is None
    assert "not subscribed" in snapshot.error


def test_seconds_until_next_tick_lands_just_after_the_minute_boundary():
    now = pd.Timestamp("2026-09-15 15:00:41", tz=NEW_YORK)

    assert seconds_until_next_tick(now, 2) == 21


def test_forming_candle_is_the_bar_for_the_current_minute():
    from watcher import forming_candle

    fetch = fixture_feed("2026-09-15 15:00")
    bars = fetch("SPCX")

    candle = forming_candle(bars, pd.Timestamp("2026-09-15 15:00:41", tz=NEW_YORK))
    assert candle is not None
    assert candle.start == pd.Timestamp("2026-09-15 15:00", tz=NEW_YORK)
    assert candle.close == bars["close"].iloc[-1]

    assert forming_candle(bars, pd.Timestamp("2026-09-15 15:03:00", tz=NEW_YORK)) is None


def test_quote_tick_publishes_price_and_forming_candle():
    from state import Quote
    from watcher import quote_tick

    store = SignalStore()
    now = pd.Timestamp("2026-09-15 15:00:41", tz=NEW_YORK)
    fetch = fixture_feed("2026-09-15 15:00")

    def fetch_quote(symbol: str) -> Quote:
        return Quote(symbol=symbol, price=144.21, quoted_at=now)

    quote_tick(fetch_quote, fetch, "SPCX", now, store)

    live = store.latest_quote()
    assert live.error is None
    assert live.price == 144.21
    assert live.forming.start == pd.Timestamp("2026-09-15 15:00", tz=NEW_YORK)
    assert live.published_at == now


def test_quote_tick_publishes_a_feed_error_when_the_gateway_fails():
    from watcher import quote_tick

    store = SignalStore()
    now = pd.Timestamp("2026-09-15 15:00:41", tz=NEW_YORK)

    def failing_quote(symbol: str):
        raise ConnectionError("quote rights taken by the moomoo app")

    quote_tick(failing_quote, fixture_feed("2026-09-15 15:00"), "SPCX", now, store)

    assert "quote rights" in store.latest_quote().error
