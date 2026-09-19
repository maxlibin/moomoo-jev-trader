"""Dashboard API behavior against a real evaluated snapshot."""

from pathlib import Path

import pandas as pd

from bars_csv import load_bars_csv
from dashboard import FLATTEN_HEADER, FLATTEN_HEADER_VALUE, create_app
from signals import DEFAULT_CONFIG, evaluate, indicator_frame
from state import LiveQuote, SignalStore, Snapshot


FIXTURES = Path(__file__).parent / "fixtures"
NEW_YORK = "America/New_York"


def bars_until(symbol: str, end: str) -> pd.DataFrame:
    frame = load_bars_csv(FIXTURES / f"{symbol}_1m.csv")
    return frame.loc[frame.index <= pd.Timestamp(end, tz=NEW_YORK)]


def evaluated_snapshot() -> Snapshot:
    bars = bars_until("SPCX", "2026-09-15 14:59")
    setup = evaluate(bars, bars_until("QQQ", "2026-09-15 14:59"), DEFAULT_CONFIG)
    return Snapshot(
        symbol="SPCX",
        benchmark="QQQ",
        published_at=pd.Timestamp("2026-09-15 15:00:01", tz=NEW_YORK),
        setup=setup,
        session=indicator_frame(bars, DEFAULT_CONFIG),
        error=None,
    )


def test_api_reports_waiting_before_the_first_snapshot():
    client = create_app(SignalStore(), review=lambda snapshot: {"verdict": "NOT TRIGGERED"}).test_client()

    response = client.get("/api/data")

    assert response.status_code == 503
    assert "No completed candle" in response.get_json()["error"]


def test_api_returns_the_published_setup_without_chart_bars():
    store = SignalStore()
    store.publish(evaluated_snapshot())
    client = create_app(store, review=lambda snapshot: {"verdict": "NOT TRIGGERED"}).test_client()

    payload = client.get("/api/data").get_json()

    summary = payload["summary"]
    assert summary["timestamp"] == "2026-09-15T14:59:00-04:00"
    assert summary["signal"] in {"STRONG BUY", "BUY SETUP", "STRONG SELL", "SELL SETUP", "HOLD"}
    assert len(summary["buy_checks"]) == 7
    assert "bars" not in payload
    assert payload["ai"]["verdict"] == "NOT TRIGGERED"


def test_api_surfaces_a_feed_error_instead_of_stale_numbers():
    store = SignalStore()
    store.publish(
        Snapshot(
            symbol="SPCX",
            benchmark="QQQ",
            published_at=pd.Timestamp("2026-09-15 15:00:01", tz=NEW_YORK),
            setup=None,
            session=None,
            error="No completed one-minute bars returned for SPCX",
        )
    )
    client = create_app(store, review=lambda snapshot: {"verdict": "NOT TRIGGERED"}).test_client()

    response = client.get("/api/data")

    assert response.status_code == 503
    assert response.get_json()["error"] == "No completed one-minute bars returned for SPCX"


def test_action_text_for_a_hold_lists_the_missing_buy_checks():
    from dashboard import action_for

    setup = evaluated_snapshot().setup
    action = action_for(setup)

    if setup.signal == "HOLD":
        assert action["headline"] == "No trade"
        for name, ok in setup.buy_checks.items():
            assert (name in action["detail"]) == (not ok)
    else:
        assert action["headline"].startswith(("BUY", "SELL"))


def test_action_text_for_a_buy_gives_entry_stop_and_target():
    from dataclasses import replace

    from dashboard import action_for

    setup = replace(evaluated_snapshot().setup, signal="BUY SETUP", stop=143.5, target=146.5)

    action = action_for(setup)

    assert action["headline"] == f"BUY near {setup.price:.2f}"
    assert "stop 143.50" in action["detail"]
    assert "target 146.50" in action["detail"]


def test_quote_api_reports_waiting_then_the_live_price():
    from state import LiveQuote, Candle

    store = SignalStore()
    app = create_app(store, review=lambda snapshot: {"verdict": "NOT TRIGGERED"})
    client = app.test_client()

    assert client.get("/api/quote").status_code == 503

    now = pd.Timestamp("2026-09-15 15:00:41", tz=NEW_YORK)
    store.publish_quote(
        LiveQuote(
            symbol="SPCX",
            price=144.21,
            quoted_at=now,
            forming=Candle(start=pd.Timestamp("2026-09-15 15:00", tz=NEW_YORK), open=144.0, high=144.3, low=143.9, close=144.21, volume=5060),
            published_at=now,
            error=None,
        )
    )

    payload = client.get("/api/quote").get_json()
    assert payload["price"] == 144.21
    assert payload["forming"]["time"] == "2026-09-15T15:00:00-04:00"
    assert payload["forming"]["volume"] == 5060


def test_live_api_uses_real_snapshot_ohlc_bars_for_the_chart():
    store = SignalStore()
    snapshot = evaluated_snapshot()
    store.publish(snapshot)
    client = create_app(store, review=lambda snapshot: {"verdict": "WAIT"}).test_client()

    payload = client.get("/api/live").get_json()

    assert len(payload["bars"]) == len(snapshot.session.tail(390))
    last = payload["bars"][-1]
    expected = snapshot.session.iloc[-1]
    assert last["at"] == snapshot.session.index[-1].isoformat()
    assert last["open"] == expected["open"]
    assert last["high"] == expected["high"]
    assert last["low"] == expected["low"]
    assert last["close"] == expected["close"]
    assert last["complete"] is True
    assert "quotes" not in payload


def test_execution_api_reports_state_and_kill_switch():
    from executor import ExecutionState

    store = SignalStore()
    app = create_app(store, review=lambda snapshot: {"verdict": "NOT TRIGGERED"})
    client = app.test_client()

    assert client.get("/api/execution").status_code == 503

    store.publish_execution(ExecutionState.fresh())
    payload = client.get("/api/execution").get_json()
    assert payload["position"] is None
    assert payload["trades_today"] == 0
    assert payload["halted"] is None

    assert client.post("/api/flatten", headers={FLATTEN_HEADER: FLATTEN_HEADER_VALUE}).status_code == 200
    assert store.kill_switch_pulled() is True


def test_flatten_rejects_requests_without_the_dashboard_header_or_from_a_foreign_host():
    store = SignalStore()
    client = create_app(store, review=lambda snapshot: {"verdict": "NOT TRIGGERED"}).test_client()

    assert client.post("/api/flatten").status_code == 403
    assert client.post("/api/flatten", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/flatten", headers={"Host": "evil.example:8050", FLATTEN_HEADER: FLATTEN_HEADER_VALUE}).status_code == 400
    assert store.kill_switch_pulled() is False

    assert client.post("/api/flatten", headers={"Host": "127.0.0.1:8050", FLATTEN_HEADER: FLATTEN_HEADER_VALUE}).status_code == 200
    assert store.kill_switch_pulled() is True


def test_live_api_surfaces_the_quote_feed_error_and_counts_completed_reviews():
    store = SignalStore()
    now = pd.Timestamp("2026-09-15 15:00:41", tz=NEW_YORK)
    store.publish_quote(LiveQuote("SPCX", None, None, None, now, "quote rights taken by the moomoo app"))
    for _ in range(3):
        store.publish_jev({"status": "complete", "approved": False, "evaluated_at": now.isoformat()})
    store.publish_jev({"status": "error", "summary": "Jev request failed"})
    client = create_app(store, review=lambda snapshot: {"verdict": "WAIT"}).test_client()

    payload = client.get("/api/live").get_json()

    assert payload["latest_quote"] is None
    assert payload["latest_quote_error"] == "quote rights taken by the moomoo app"
    assert payload["review_count"] == 3 and len(payload["reviews"]) == 3
    assert payload["latest_review"]["status"] == "error"


def test_execution_api_reports_a_pending_exit_and_broker_errors():
    from dataclasses import replace

    from executor import ExecutionState, PendingExit
    from trader import OpenPosition

    store = SignalStore()
    now = pd.Timestamp("2026-09-15 15:00:41", tz=NEW_YORK)
    position = OpenPosition("SPCX", 10, 150.0, 149.0, 153.0, now)
    state = replace(
        ExecutionState.fresh(),
        position=position,
        pending_exit=PendingExit("sell-1", 10, "stop", now),
        broker_error="OpenD order_list_query failed",
    )
    store.publish_execution(state)
    client = create_app(store, review=lambda snapshot: {"verdict": "WAIT"}).test_client()

    payload = client.get("/api/execution").get_json()

    assert payload["position"]["quantity"] == 10
    assert payload["pending_exit"] == {"order_id": "sell-1", "quantity": 10, "reason": "stop", "placed_at": now.isoformat()}
    assert payload["broker_error"] == "OpenD order_list_query failed"


def test_flatten_says_so_when_there_is_nothing_left_to_flatten():
    from dataclasses import replace

    from executor import ExecutionState

    store = SignalStore()
    client = create_app(store, review=lambda snapshot: {"verdict": "WAIT"}).test_client()
    headers = {FLATTEN_HEADER: FLATTEN_HEADER_VALUE}

    assert "not running" in client.post("/api/flatten", headers=headers).get_json()["status"]

    store.publish_execution(replace(ExecutionState.fresh(), halted="kill switch"))
    assert "nothing to flatten" in client.post("/api/flatten", headers=headers).get_json()["status"]

    store.publish_execution(ExecutionState.fresh())
    assert "keeps going until nothing is open" in client.post("/api/flatten", headers=headers).get_json()["status"]
    assert store.kill_switch_pulled() is True


def test_execution_api_serialises_an_adopted_holding_without_levels():
    import math
    from dataclasses import replace

    from executor import ExecutionState
    from trader import OpenPosition

    store = SignalStore()
    now = pd.Timestamp("2026-09-15 09:31:00", tz=NEW_YORK)
    store.publish_execution(replace(ExecutionState.fresh(), position=OpenPosition("SPCX", 25, 140.0, 0.0, math.inf, now), halted="already held"))
    client = create_app(store, review=lambda snapshot: {"verdict": "WAIT"}).test_client()

    payload = client.get("/api/live").get_json()

    assert payload["execution"]["position"]["quantity"] == 25
    assert payload["execution"]["position"]["target"] is None
