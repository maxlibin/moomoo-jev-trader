"""Moomoo OpenAPI connector for one-minute US candles through a local OpenD gateway.

OpenD must be installed, running, and logged in with your moomoo ID; every call
here is a TCP request to it. ``MoomooFeed`` wraps the SDK context and raises
``ConnectionError`` with the gateway's own message whenever a call fails.
"""

import socket
from typing import Optional

import pandas as pd
from moomoo import KL_FIELD, RET_OK, AuType, KLType, OpenQuoteContext, SubType

from state import Quote


NEW_YORK = "America/New_York"
GATEWAY_PROBE_SECONDS = 3.0
BAR_COLUMNS = ["open", "high", "low", "close", "volume"]


def moomoo_code(symbol: str) -> str:
    """Moomoo identifies US listings as ``US.<ticker>``."""
    return f"US.{symbol.upper()}"


def to_bars(kline: pd.DataFrame) -> pd.DataFrame:
    """Convert a Moomoo candlestick frame into New York indexed OHLCV bars.

    Moomoo stamps each one-minute candle with the minute it ends on (the first
    session bar is 09:31), so stamps are shifted back one minute to the candle's
    start, which is the convention the signal engine and the backtester use.
    """
    required = {"time_key", *BAR_COLUMNS}
    missing = required.difference(kline.columns)
    if missing:
        raise ValueError(f"Moomoo candlestick frame is missing columns {sorted(missing)}; got {list(kline.columns)}")
    end_stamps = pd.DatetimeIndex(pd.to_datetime(kline["time_key"]), name="datetime").tz_localize(NEW_YORK)
    index = end_stamps - pd.Timedelta(minutes=1)
    bars = pd.DataFrame(
        {
            "open": kline["open"].astype(float).to_numpy(),
            "high": kline["high"].astype(float).to_numpy(),
            "low": kline["low"].astype(float).to_numpy(),
            "close": kline["close"].astype(float).to_numpy(),
            "volume": kline["volume"].astype("int64").to_numpy(),
        },
        index=index,
    )
    return bars.sort_index()


def to_quote(quote: pd.DataFrame, symbol: str) -> Quote:
    """Convert the first row of a Moomoo stock quote frame into a ``Quote``."""
    required = {"last_price", "data_date", "data_time"}
    missing = required.difference(quote.columns)
    if missing or quote.empty:
        raise ValueError(f"Moomoo quote frame for {symbol} is missing {sorted(missing)} or is empty; got {list(quote.columns)}")
    row = quote.iloc[0]
    quoted_at = pd.Timestamp(f"{row['data_date']} {row['data_time']}", tz=NEW_YORK)
    return Quote(symbol=symbol, price=float(row["last_price"]), quoted_at=quoted_at)


class MoomooFeed:
    """Quote connection to OpenD for real-time and historical one-minute candles."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._context: Optional[OpenQuoteContext] = None

    def _require_gateway(self) -> None:
        """Fail fast if nothing listens on the OpenD port; the SDK itself retries forever."""
        try:
            with socket.create_connection((self._host, self._port), timeout=GATEWAY_PROBE_SECONDS):
                return
        except OSError as exc:
            raise ConnectionError(
                f"OpenD is not reachable at {self._host}:{self._port} ({exc}). Start OpenD, log in with your "
                "moomoo ID, and check MOOMOO_HOST and MOOMOO_PORT in .env"
            ) from exc

    def connect(self, symbols: list[str]) -> None:
        """Open the gateway connection and subscribe to one-minute candles for ``symbols``."""
        self._require_gateway()
        self._context = OpenQuoteContext(host=self._host, port=self._port)
        codes = [moomoo_code(symbol) for symbol in symbols]
        ret, message = self._context.subscribe(codes, [SubType.K_1M, SubType.QUOTE], subscribe_push=False)
        if ret != RET_OK:
            raise ConnectionError(
                f"OpenD at {self._host}:{self._port} refused the K_1M and QUOTE subscription for {codes}: {message}"
            )

    def _require_context(self) -> OpenQuoteContext:
        if self._context is None:
            raise ConnectionError("MoomooFeed.connect must be called before requesting bars")
        return self._context

    def minute_bars(self, symbol: str, count: int) -> pd.DataFrame:
        """Most recent ``count`` one-minute candles (max 1000), including the minute still forming."""
        code = moomoo_code(symbol)
        ret, data = self._require_context().get_cur_kline(code, count, KLType.K_1M, AuType.NONE)
        if ret != RET_OK:
            raise ConnectionError(f"OpenD get_cur_kline failed for {code} (num={count}): {data}")
        return to_bars(data)

    def last_quote(self, symbol: str) -> Quote:
        """Last traded price and its exchange timestamp."""
        code = moomoo_code(symbol)
        ret, data = self._require_context().get_stock_quote([code])
        if ret != RET_OK:
            raise ConnectionError(f"OpenD get_stock_quote failed for {code}: {data}")
        return to_quote(data, symbol)

    def history_minute_bars(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """All one-minute candles between ``start`` and ``end`` dates (YYYY-MM-DD), following the pages."""
        code = moomoo_code(symbol)
        context = self._require_context()
        pages = []
        page_key = None
        while True:
            ret, data, page_key = context.request_history_kline(
                code,
                start=start,
                end=end,
                ktype=KLType.K_1M,
                autype=AuType.NONE,
                fields=[KL_FIELD.ALL],
                max_count=1000,
                page_req_key=page_key,
            )
            if ret != RET_OK:
                raise ConnectionError(f"OpenD request_history_kline failed for {code} {start}..{end}: {data}")
            pages.append(data)
            if page_key is None:
                break
        return to_bars(pd.concat(pages, ignore_index=True))

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
