"""Moomoo OpenAPI trading connector for US stocks through the local OpenD gateway.

Defaults to the simulated account. Real-money trading needs ``environment="REAL"``
and a trading unlock done in the OpenD window; this module never handles the
trade password. Every call raises ``ConnectionError`` with the gateway's own
message on failure.
"""

from typing import Optional

from moomoo import (
    RET_OK,
    Currency,
    ModifyOrderOp,
    OpenSecTradeContext,
    OrderType,
    SecurityFirm,
    TrdEnv,
    TrdMarket,
    TrdSide,
)

from executor import Account, BrokerOrder, Holding, OrderState
from moomoo_feed import moomoo_code


ENVIRONMENTS = {"SIMULATE": TrdEnv.SIMULATE, "REAL": TrdEnv.REAL}


class MoomooBroker:
    """Order and account access for one trading environment on the US market."""

    def __init__(self, host: str, port: int, environment: str, security_firm: str) -> None:
        if environment not in ENVIRONMENTS:
            raise ValueError(f"MOOMOO_TRADE_ENV must be SIMULATE or REAL, got {environment!r}")
        if not SecurityFirm.if_has_key(security_firm):
            raise ValueError(f"MOOMOO_SECURITY_FIRM {security_firm!r} is not one of {SecurityFirm.get_all_keys()}")
        self._host = host
        self._port = port
        self._environment = ENVIRONMENTS[environment]
        self._security_firm = security_firm
        self._context: Optional[OpenSecTradeContext] = None

    @property
    def environment(self) -> str:
        return str(self._environment)

    def connect(self) -> None:
        self._context = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US, host=self._host, port=self._port, security_firm=self._security_firm
        )
        ret, accounts = self._context.get_acc_list()
        if ret != RET_OK:
            raise ConnectionError(f"OpenD get_acc_list failed at {self._host}:{self._port}: {accounts}")
        if not (accounts["trd_env"] == self._environment).any():
            raise ConnectionError(f"No {self._environment} account is available through OpenD; accounts: {accounts['trd_env'].tolist()}")

    def _require_context(self) -> OpenSecTradeContext:
        if self._context is None:
            raise ConnectionError("MoomooBroker.connect must be called before trading")
        return self._context

    def account(self) -> Account:
        ret, data = self._require_context().accinfo_query(trd_env=self._environment, currency=Currency.USD)
        if ret != RET_OK:
            raise ConnectionError(f"OpenD accinfo_query failed ({self._environment}): {data}")
        row = data.iloc[0]
        return Account(equity=float(row["total_assets"]), cash=float(row["cash"]))

    def holding(self, symbol: str) -> Holding:
        """Shares held and their average cost; the cost is 0.0 when OpenD reports no valid cost for them."""
        code = moomoo_code(symbol)
        ret, data = self._require_context().position_list_query(code=code, trd_env=self._environment)
        if ret != RET_OK:
            raise ConnectionError(f"OpenD position_list_query failed for {code} ({self._environment}): {data}")
        rows = data.loc[data["code"] == code]
        quantity = int(rows["qty"].sum()) if not rows.empty else 0
        if quantity <= 0:
            return Holding(0, 0.0)
        priced = rows.loc[rows["cost_price_valid"].astype(bool)]
        priced_quantity = int(priced["qty"].sum()) if not priced.empty else 0
        if priced_quantity <= 0:
            return Holding(quantity, 0.0)
        return Holding(quantity, float((priced["cost_price"].astype(float) * priced["qty"]).sum() / priced_quantity))

    def orders(self, symbol: str) -> list[BrokerOrder]:
        """Today's orders for ``symbol`` as OpenD reports them, newest first."""
        code = moomoo_code(symbol)
        ret, data = self._require_context().order_list_query(code=code, trd_env=self._environment)
        if ret != RET_OK:
            raise ConnectionError(f"OpenD order_list_query failed for {code} ({self._environment}): {data}")
        rows = data.loc[data["code"] == code].sort_values("create_time", ascending=False)
        return [
            BrokerOrder(
                order_id=str(row["order_id"]), side=str(row["trd_side"]), quantity=int(row["qty"]), price=float(row["price"]),
                status=str(row["order_status"]), filled_quantity=int(row["dealt_qty"]), average_price=float(row["dealt_avg_price"]),
            )
            for _, row in rows.iterrows()
        ]

    def _place(self, symbol: str, quantity: int, side: TrdSide, order_type: OrderType, price: float) -> str:
        code = moomoo_code(symbol)
        ret, data = self._require_context().place_order(
            price=price, qty=quantity, code=code, trd_side=side, order_type=order_type, trd_env=self._environment
        )
        if ret != RET_OK:
            raise ConnectionError(f"OpenD place_order failed: {side} {quantity} {code} @ {price} ({self._environment}): {data}")
        return str(data.iloc[0]["order_id"])

    def buy_limit(self, symbol: str, quantity: int, price: float) -> str:
        return self._place(symbol, quantity, TrdSide.BUY, OrderType.NORMAL, price)

    def sell_market(self, symbol: str, quantity: int) -> str:
        return self._place(symbol, quantity, TrdSide.SELL, OrderType.MARKET, 0.0)

    def order(self, order_id: str) -> OrderState:
        ret, data = self._require_context().order_list_query(order_id=order_id, trd_env=self._environment)
        if ret != RET_OK:
            raise ConnectionError(f"OpenD order_list_query failed for order {order_id} ({self._environment}): {data}")
        if data.empty:
            raise ConnectionError(f"OpenD returned no order with id {order_id} ({self._environment})")
        row = data.iloc[0]
        return OrderState(status=str(row["order_status"]), filled_quantity=int(row["dealt_qty"]), average_price=float(row["dealt_avg_price"]))

    def cancel(self, order_id: str) -> None:
        ret, data = self._require_context().modify_order(ModifyOrderOp.CANCEL, order_id, 0, 0, trd_env=self._environment)
        if ret != RET_OK:
            raise ConnectionError(f"OpenD cancel failed for order {order_id} ({self._environment}): {data}")

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
