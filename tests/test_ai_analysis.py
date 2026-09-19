"""TypeSafe Jev state construction, thresholds, and freshness gating."""

from types import SimpleNamespace

import pandas as pd
import pytest

from pathlib import Path

from ai_analysis import JevReviewer, append_review, build_state, gate_decision, gate_mode_from_env
from bars_csv import load_bars_csv
from signals import DEFAULT_CONFIG, evaluate, indicator_frame
from state import Candle, LiveQuote, Snapshot


NEW_YORK = "America/New_York"
FIXTURES = Path(__file__).parent / "fixtures"


def evaluated_snapshot() -> Snapshot:
    end = pd.Timestamp("2026-09-15 14:59", tz=NEW_YORK)
    bars = load_bars_csv(FIXTURES / "SPCX_1m.csv").loc[:end]
    benchmark = load_bars_csv(FIXTURES / "QQQ_1m.csv").loc[:end]
    setup = evaluate(bars, benchmark, DEFAULT_CONFIG)
    return Snapshot(
        "SPCX",
        "QQQ",
        pd.Timestamp("2026-09-15 15:00:01", tz=NEW_YORK),
        setup,
        indicator_frame(bars, DEFAULT_CONFIG),
        None,
    )


class FakeClient:
    def __init__(self, up=0.72, supportive=0.68, stop_first=0.25):
        self.up = up
        self.supportive = supportive
        self.stop_first = stop_first
        self.calls = []

    def system_one(self, state, questions):
        self.calls.append((state, questions))
        return SimpleNamespace(
            model="jev-test",
            answers={
                "direction": SimpleNamespace(
                    choice="up",
                    probabilities={"up": self.up, "sideways": 0.18, "down": 1 - self.up - 0.18},
                    confidence=0.7,
                ),
                "entry_quality": SimpleNamespace(
                    choice="supportive",
                    probabilities={"supportive": self.supportive, "mixed": 0.2, "unsupportive": 0.8 - self.supportive},
                    confidence=0.6,
                ),
                "stop_first": SimpleNamespace(noul=self.stop_first),
            },
        )


def live_quote(snapshot):
    at = snapshot.published_at
    return LiveQuote(
        symbol=snapshot.symbol,
        price=snapshot.setup.price + 0.05,
        quoted_at=at,
        forming=Candle(at.floor("min"), snapshot.setup.price, snapshot.setup.price + 0.1, snapshot.setup.price - 0.05, snapshot.setup.price + 0.05, 1200),
        published_at=at,
        error=None,
    )


def test_build_state_includes_recent_bars_and_live_evidence():
    snapshot = evaluated_snapshot()
    state = build_state(snapshot, live_quote(snapshot))

    assert state["candidate"]["completed_candle"] == snapshot.setup.timestamp.isoformat()
    assert state["live"]["forming_candle"]["volume"] == 1200
    assert 1 <= len(state["recent_completed_bars"]) <= 30
    assert state["recent_completed_bars"][-1]["close"] == pytest.approx(snapshot.setup.price, abs=1e-6)


def test_review_uses_typed_probabilities_to_approve_candidate(monkeypatch):
    from dataclasses import replace

    snapshot = evaluated_snapshot()
    snapshot = replace(snapshot, setup=replace(snapshot.setup, signal="BUY SETUP"))
    client = FakeClient()

    review = JevReviewer(client).review(snapshot, live_quote(snapshot))

    assert len(client.calls) == 1
    assert review["status"] == "complete"
    assert review["approved"] is True
    assert review["direction"]["probabilities"]["up"] == 0.72
    assert review["stop_first"] == 0.25


def test_review_rejects_when_stop_first_risk_is_high():
    from dataclasses import replace

    snapshot = evaluated_snapshot()
    snapshot = replace(snapshot, setup=replace(snapshot.setup, signal="BUY SETUP"))

    review = JevReviewer(FakeClient(stop_first=0.8)).review(snapshot, live_quote(snapshot))

    assert review["approved"] is False
    assert any("Stop-first" in risk for risk in review["risks"])


def test_completed_reviews_are_journaled_for_shadow_analysis(tmp_path):
    path = tmp_path / "reviews.jsonl"
    append_review({"status": "disabled"}, path)
    append_review({"status": "complete", "approved": True, "live_price": 144.2}, path)

    assert not (tmp_path / "missing.jsonl").exists()
    lines = path.read_text().splitlines()
    assert len(lines) == 1 and '"live_price":144.2' in lines[0]


def test_gate_requires_a_fresh_review_for_the_same_candle():
    snapshot = evaluated_snapshot()
    now = pd.Timestamp("2026-09-15 15:00:05", tz=NEW_YORK)
    review = {
        "status": "complete",
        "approved": True,
        "summary": "ok",
        "candle": snapshot.setup.timestamp.isoformat(),
        "evaluated_at": now.tz_convert("UTC").isoformat(),
    }

    assert gate_decision(review, snapshot, now) == (True, "Jev entry gate passed")
    old = {**review, "evaluated_at": (now - pd.Timedelta(seconds=60)).tz_convert("UTC").isoformat()}
    assert gate_decision(old, snapshot, now)[0] is None
    wrong = {**review, "candle": "2026-09-15T14:58:00-04:00"}
    assert gate_decision(wrong, snapshot, now)[0] is None


def test_gate_mode_accepts_only_shadow_or_enforce():
    assert gate_mode_from_env({}) == "shadow"
    assert gate_mode_from_env({"JEV_ENTRY_GATE_MODE": " Enforce "}) == "enforce"
    with pytest.raises(ValueError, match="JEV_ENTRY_GATE_MODE.*'enforced'"):
        gate_mode_from_env({"JEV_ENTRY_GATE_MODE": "enforced"})
