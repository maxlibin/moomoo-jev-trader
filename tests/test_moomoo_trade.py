"""Broker connector: constructor validation everywhere, order round trips only against a running OpenD gateway."""

import socket
import time

import pandas as pd
import pytest
from moomoo import RET_OK

from moomoo_trade import MoomooBroker


HOST, PORT = "127.0.0.1", 11111


def gateway_listening() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=1):
            return True
    except OSError:
        return False


needs_gateway = pytest.mark.skipif(not gateway_listening(), reason="OpenD is not running on 127.0.0.1:11111")


@pytest.fixture
def broker() -> MoomooBroker:
    broker = MoomooBroker(HOST, PORT, "SIMULATE", "FUTUSG")
    broker.connect()
    yield broker
    broker.close()


@needs_gateway
def test_account_and_position_read_from_the_simulated_account(broker: MoomooBroker):
    account = broker.account()

    assert account.equity > 0
    assert account.cash > 0
    assert broker.holding("SPCX").quantity >= 0


@needs_gateway
def test_limit_buy_far_below_market_is_accepted_then_cancelled(broker: MoomooBroker):
    order_id = broker.buy_limit("SPCX", 1, 1.00)

    time.sleep(1)
    before = broker.order(order_id)
    assert before.status in {"SUBMITTING", "SUBMITTED"}
    assert before.filled_quantity == 0

    broker.cancel(order_id)
    time.sleep(1)
    after = broker.order(order_id)
    assert after.status == "CANCELLED_ALL"


def test_real_environment_requires_explicit_opt_in():
    with pytest.raises(ValueError, match="REAL"):
        MoomooBroker(HOST, PORT, "LIVE", "FUTUSG")


def test_unknown_security_firm_is_rejected_with_the_valid_names():
    with pytest.raises(ValueError, match="MOOMOO_SECURITY_FIRM 'FUTUXX' is not one of .*FUTUSG"):
        MoomooBroker(HOST, PORT, "SIMULATE", "FUTUXX")


class StubTradeContext:
    """Stands in for OpenSecTradeContext with the frame position_list_query documents, including an unpriced lot."""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def position_list_query(self, code: str, trd_env: str):
        return RET_OK, pd.DataFrame(self.rows)


def test_holding_averages_only_the_lots_with_a_valid_cost_price():
    broker = MoomooBroker(HOST, PORT, "SIMULATE", "FUTUSG")

    broker._context = StubTradeContext([
        {"code": "US.SPCX", "qty": 10, "cost_price": 150.0, "cost_price_valid": True},
        {"code": "US.SPCX", "qty": 30, "cost_price": 154.0, "cost_price_valid": True},
        {"code": "US.QQQ", "qty": 5, "cost_price": 700.0, "cost_price_valid": True},
    ])
    priced = broker.holding("SPCX")
    assert priced.quantity == 40 and priced.cost_price == pytest.approx(153.0)

    broker._context = StubTradeContext([{"code": "US.SPCX", "qty": 25, "cost_price": "N/A", "cost_price_valid": False}])
    unpriced = broker.holding("SPCX")
    assert unpriced.quantity == 25 and unpriced.cost_price == 0.0

    broker._context = StubTradeContext([{"code": "US.QQQ", "qty": 5, "cost_price": 700.0, "cost_price_valid": True}])
    assert broker.holding("SPCX").quantity == 0


class MalformedTradeContext:
    """Answers RET_OK with frames OpenD should never send: empty, missing columns, or NaN quantities."""

    def accinfo_query(self, trd_env: str, currency: str):
        return RET_OK, pd.DataFrame(columns=["total_assets", "cash"])

    def place_order(self, price, qty, code, trd_side, order_type, trd_env):
        return RET_OK, pd.DataFrame([{"code": code, "order_status": "SUBMITTED"}])

    def order_list_query(self, trd_env: str, order_id: str = "", code: str = ""):
        row = {"code": "US.SPCX", "order_id": "77", "trd_side": "BUY", "qty": 10.0, "price": 150.0, "order_status": "SUBMITTED",
               "dealt_qty": float("nan"), "dealt_avg_price": 0.0, "create_time": "2026-09-15 15:00:01"}
        return RET_OK, pd.DataFrame([row])

    def position_list_query(self, code: str, trd_env: str):
        return RET_OK, pd.DataFrame([{"code": code, "qty": 10}])


def test_malformed_gateway_frames_raise_connection_errors_that_show_the_frame():
    broker = MoomooBroker(HOST, PORT, "SIMULATE", "FUTUSG")
    broker._context = MalformedTradeContext()

    with pytest.raises(ConnectionError, match="accinfo_query.*IndexError"):
        broker.account()
    with pytest.raises(ConnectionError, match="place_order.*KeyError.*'order_status': 'SUBMITTED'"):
        broker.buy_limit("SPCX", 10, 150.0)
    with pytest.raises(ConnectionError, match="order_list_query.*dealt_qty is nan"):
        broker.order("77")
    with pytest.raises(ConnectionError, match="order_list_query.*dealt_qty is nan.*'order_id': '77'"):
        broker.orders("SPCX")
    with pytest.raises(ConnectionError, match="position_list_query.*KeyError.*cost_price_valid"):
        broker.holding("SPCX")
