"""Broker connector against the Moomoo simulated account through a running OpenD gateway."""

import socket
import time

import pytest

from moomoo_trade import MoomooBroker


HOST, PORT = "127.0.0.1", 11111


def gateway_listening() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not gateway_listening(), reason="OpenD is not running on 127.0.0.1:11111")


@pytest.fixture
def broker() -> MoomooBroker:
    broker = MoomooBroker(HOST, PORT, "SIMULATE", "FUTUSG")
    broker.connect()
    yield broker
    broker.close()


def test_account_and_position_read_from_the_simulated_account(broker: MoomooBroker):
    account = broker.account()

    assert account.equity > 0
    assert account.cash > 0
    assert broker.position_quantity("SPCX") >= 0


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
