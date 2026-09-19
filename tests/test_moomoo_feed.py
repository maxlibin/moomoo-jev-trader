"""Conversion of Moomoo candlestick frames into the bar format the signal engine expects."""

import socket
import time

import pandas as pd
import pytest

from moomoo_feed import MoomooFeed, moomoo_code, to_bars


def moomoo_kline_frame() -> pd.DataFrame:
    """Shape and column names documented for OpenQuoteContext.get_cur_kline on a US stock."""
    return pd.DataFrame(
        {
            "code": ["US.SPCX", "US.SPCX"],
            "time_key": ["2026-09-16 10:15:00", "2026-09-16 10:16:00"],
            "open": [151.08, 151.02],
            "close": [151.01, 150.89],
            "high": [151.27, 151.04],
            "low": [150.82, 150.53],
            "volume": [449438, 281343],
            "turnover": [67_900_000.0, 42_400_000.0],
            "pe_ratio": [0.0, 0.0],
            "turnover_rate": [0.0, 0.0],
        }
    )


def test_to_bars_produces_new_york_indexed_ohlcv_stamped_by_candle_start():
    """Moomoo stamps a candle with its end minute; 10:15:00 covers 10:14 to 10:15."""
    bars = to_bars(moomoo_kline_frame())

    assert list(bars.columns) == ["open", "high", "low", "close", "volume"]
    assert bars.index[0] == pd.Timestamp("2026-09-16 10:14", tz="America/New_York")
    assert bars.index.is_monotonic_increasing
    assert bars["close"].iloc[-1] == 150.89
    assert bars["volume"].dtype.kind in "iu"


def test_to_bars_rejects_frames_without_candle_columns():
    with pytest.raises(ValueError, match="time_key"):
        to_bars(pd.DataFrame({"close": [1.0]}))


def test_moomoo_code_prefixes_us_market():
    assert moomoo_code("spcx") == "US.SPCX"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_connect_fails_fast_when_opend_is_not_listening():
    port = free_port()
    started = time.monotonic()
    with pytest.raises(ConnectionError, match=f"127.0.0.1:{port}"):
        MoomooFeed("127.0.0.1", port).connect(["SPCX"])
    assert time.monotonic() - started < 5


def test_to_quote_reads_last_price_and_new_york_time():
    from moomoo_feed import to_quote

    frame = pd.DataFrame(
        {
            "code": ["US.SPCX"],
            "name": ["Space Exploration Technologies"],
            "data_date": ["2026-09-16"],
            "data_time": ["12:01:54.276"],
            "last_price": [151.18],
            "open_price": [150.1],
            "high_price": [152.4],
            "low_price": [149.9],
            "prev_close_price": [150.0],
            "volume": [12345678],
        }
    )

    quote = to_quote(frame, "SPCX")

    assert quote.symbol == "SPCX"
    assert quote.price == 151.18
    assert quote.quoted_at == pd.Timestamp("2026-09-16 12:01:54.276", tz="America/New_York")
