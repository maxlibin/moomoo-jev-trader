"""Five-second TypeSafe Jev market review and optional entry gate.

Jev does not replace the deterministic signal or risk engines. It receives a
compact view of recent price action and answers three independent questions.
The answers are published for the dashboard and may, when explicitly enabled,
block a new long entry. Broker execution, sizing, stops, and exits remain in
normal Python code.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, RetryPolicy, TypeSafeClient

from signals import BUY_SIGNALS
from state import LiveQuote, SignalStore, Snapshot


load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MODEL = os.getenv("JEV_MODEL", "jev-latest")
MIN_UP_PROBABILITY = float(os.getenv("JEV_MIN_UP_PROBABILITY", "0.60"))
MIN_QUALITY_PROBABILITY = float(os.getenv("JEV_MIN_QUALITY_PROBABILITY", "0.55"))
MAX_STOP_FIRST_PROBABILITY = float(os.getenv("JEV_MAX_STOP_FIRST_PROBABILITY", "0.45"))
MAX_GATE_AGE_SECONDS = float(os.getenv("JEV_MAX_GATE_AGE_SECONDS", "15"))
MAX_QUOTE_AGE_SECONDS = float(os.getenv("JEV_MAX_QUOTE_AGE_SECONDS", "60"))
RECENT_BARS = int(os.getenv("JEV_RECENT_BARS", "30"))
REVIEWS_JOURNAL = Path(__file__).parent / "logs" / "jev_reviews.jsonl"

QUESTIONS = {
    "direction": Choice(
        instructions=(
            "Using only the supplied completed one-minute bars and current forming candle, "
            "what is the most likely direction of the symbol over the next five one-minute candles?"
        ),
        criteria={
            "up": "The price is more likely to finish meaningfully above the current live price.",
            "sideways": "The price is more likely to remain near the current live price without a clear directional move.",
            "down": "The price is more likely to finish meaningfully below the current live price.",
        },
    ),
    "entry_quality": Choice(
        instructions=(
            "How supportive is the supplied price action for the proposed long entry? Judge price structure, "
            "volume, trend consistency, breakout follow-through, and the benchmark check."
        ),
        criteria={
            "supportive": "The evidence consistently supports the proposed long entry.",
            "mixed": "The evidence is conflicting or too weak for a clear judgment.",
            "unsupportive": "The evidence contradicts the proposed long entry or suggests a failing move.",
        },
    ),
    "stop_first": Noul(
        instructions=(
            "For the proposed long trade, does the supplied price action indicate elevated risk that the stop "
            "will be reached before the target?"
        ),
        criteria={
            "true": "Recent structure indicates elevated downside, exhaustion, or failed-breakout risk.",
            "false": "Recent structure remains orderly enough that stop-first risk is not elevated.",
        },
    ),
}


def _number(value) -> Optional[float]:
    """Return a finite rounded float, otherwise None so state remains valid JSON."""
    if value is None or pd.isna(value):
        return None
    value = float(value)
    if value in (float("inf"), float("-inf")):
        return None
    return round(value, 6)


def _recent_bars(snapshot: Snapshot) -> list[dict]:
    if snapshot.session is None:
        return []
    columns = [name for name in ("open", "high", "low", "close", "volume") if name in snapshot.session.columns]
    rows = []
    for at, row in snapshot.session[columns].tail(RECENT_BARS).iterrows():
        rows.append({"time": at.isoformat(), **{name: _number(row[name]) for name in columns}})
    return rows


def build_state(snapshot: Snapshot, quote: LiveQuote) -> dict:
    """Build compact market evidence for one Jev evaluation."""
    setup = snapshot.setup
    forming = quote.forming
    return {
        "objective": {
            "strategy": "Long-only one-minute breakout setup; entry on the next live quote, exit at stop, target, or 15:50 New York time.",
            "forecast_horizon_minutes": 5,
            "symbol": snapshot.symbol,
            "benchmark": snapshot.benchmark,
        },
        "candidate": {
            "completed_candle": setup.timestamp.isoformat(),
            "rule_signal": setup.signal,
            "price": _number(setup.price),
            "stop": _number(setup.stop),
            "target": _number(setup.target),
            "buy_score": setup.buy_score,
            "total_checks": setup.total_checks,
            "buy_checks": setup.buy_checks,
            "ema_fast": _number(setup.ema_fast),
            "ema_slow": _number(setup.ema_slow),
            "vwap": _number(setup.vwap),
            "rsi14": _number(setup.rsi),
            "relative_volume": _number(setup.relative_volume),
        },
        "live": {
            "price": _number(quote.price),
            "quoted_at": quote.quoted_at.isoformat() if quote.quoted_at is not None else None,
            "forming_candle": None
            if forming is None
            else {
                "start": forming.start.isoformat(),
                "open": _number(forming.open),
                "high": _number(forming.high),
                "low": _number(forming.low),
                "close": _number(forming.close),
                "volume": forming.volume,
            },
        },
        "recent_completed_bars": _recent_bars(snapshot),
    }


def idle_review(reason: str, status: str = "idle") -> dict:
    return {
        "status": status,
        "verdict": "WAIT",
        "summary": reason,
        "risks": [],
        "model": MODEL,
        "approved": False,
    }


class JevReviewer:
    """Owns a reusable TypeSafe client and evaluates the latest market state."""

    def __init__(self, client=None):
        self.client = client
        if self.client is None and os.getenv("TYPESAFE_API_KEY"):
            timeout = float(os.getenv("JEV_TIMEOUT_SECONDS", "4"))
            self.client = TypeSafeClient(model=MODEL, retry=RetryPolicy(max_retries=0, timeout=timeout))

    def review(self, snapshot: Snapshot, quote: LiveQuote) -> dict:
        if snapshot.setup is None:
            return idle_review("No completed setup is available.")
        if quote.price is None:
            return idle_review("No live quote is available.")
        if self.client is None:
            return idle_review("Set TYPESAFE_API_KEY to enable five-second Jev reviews.", "disabled")

        started = time.perf_counter()
        try:
            response = self.client.system_one(state=build_state(snapshot, quote), questions=QUESTIONS)
            direction = response.answers["direction"]
            quality = response.answers["entry_quality"]
            stop_first = response.answers["stop_first"]
            direction_probabilities = dict(direction.probabilities)
            quality_probabilities = dict(quality.probabilities)
            up_probability = float(direction_probabilities.get("up", 0.0))
            supportive_probability = float(quality_probabilities.get("supportive", 0.0))
            stop_probability = float(stop_first.noul)
            is_candidate = snapshot.setup.signal in BUY_SIGNALS
            approved = bool(
                is_candidate
                and up_probability >= MIN_UP_PROBABILITY
                and supportive_probability >= MIN_QUALITY_PROBABILITY
                and stop_probability <= MAX_STOP_FIRST_PROBABILITY
            )
            risks = []
            if not is_candidate:
                risks.append("The deterministic rules do not currently have a long entry.")
            if up_probability < MIN_UP_PROBABILITY:
                risks.append(f"Up probability {up_probability:.0%} is below {MIN_UP_PROBABILITY:.0%}.")
            if supportive_probability < MIN_QUALITY_PROBABILITY:
                risks.append(f"Supportive-entry probability {supportive_probability:.0%} is below {MIN_QUALITY_PROBABILITY:.0%}.")
            if stop_probability > MAX_STOP_FIRST_PROBABILITY:
                risks.append(f"Stop-first risk {stop_probability:.0%} exceeds {MAX_STOP_FIRST_PROBABILITY:.0%}.")
            return {
                "status": "complete",
                "verdict": "PASS" if approved else "WAIT",
                "summary": (
                    f"5m direction: {direction.choice} (up {up_probability:.0%}); "
                    f"entry quality: {quality.choice} (supportive {supportive_probability:.0%}); "
                    f"stop-first risk {stop_probability:.0%}."
                ),
                "risks": risks[:3],
                "model": getattr(response, "model", MODEL),
                "approved": approved,
                "candle": snapshot.setup.timestamp.isoformat(),
                "signal": snapshot.setup.signal,
                "live_price": quote.price,
                "quoted_at": quote.quoted_at.isoformat() if quote.quoted_at is not None else None,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "direction": {"choice": direction.choice, "probabilities": direction_probabilities, "confidence": direction.confidence},
                "entry_quality": {"choice": quality.choice, "probabilities": quality_probabilities, "confidence": quality.confidence},
                "stop_first": stop_probability,
            }
        except Exception as exc:
            return {
                **idle_review(f"Jev request failed: {exc}", "error"),
                "candle": snapshot.setup.timestamp.isoformat(),
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "latency_ms": round((time.perf_counter() - started) * 1000),
            }


def gate_decision(review: Optional[dict], snapshot: Snapshot, now: pd.Timestamp) -> tuple[Optional[bool], str]:
    """Translate the latest review into ready/pass/reject for the executor.

    ``None`` means wait for a fresh Jev result without consuming the candle.
    """
    if snapshot.setup is None:
        return None, "waiting for a completed setup"
    if not review or review.get("status") != "complete":
        return None, "waiting for a successful Jev review"
    if review.get("candle") != snapshot.setup.timestamp.isoformat():
        return None, "waiting for Jev to evaluate the latest candle"
    try:
        evaluated_at = pd.Timestamp(review["evaluated_at"])
        age = (now.tz_convert("UTC") - evaluated_at.tz_convert("UTC")).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None, "Jev review has no valid timestamp"
    if age < -2 or age > MAX_GATE_AGE_SECONDS:
        return None, f"waiting for a fresh Jev review (latest is {max(0, age):.0f}s old)"
    if review.get("approved") is True:
        return True, "Jev entry gate passed"
    return False, f"Jev entry gate rejected: {review.get('summary', 'thresholds not met')}"


def append_review(review: dict, path: Path = REVIEWS_JOURNAL) -> None:
    """Append a completed shadow/enforced review for later outcome analysis."""
    if review.get("status") != "complete":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(review, separators=(",", ":")) + "\n")


def run_jev_forever(store: SignalStore, interval_seconds: float = 5.0, reviewer: Optional[JevReviewer] = None) -> None:
    """Evaluate the newest snapshot and live quote on a five-second schedule."""
    reviewer = reviewer or JevReviewer()
    next_run = time.monotonic()
    while True:
        snapshot, quote = store.latest(), store.latest_quote()
        if snapshot is not None and snapshot.error is None and quote is not None and quote.error is None:
            quote_age = None
            if quote.quoted_at is not None:
                quote_age = (pd.Timestamp.now(tz="UTC") - quote.quoted_at.tz_convert("UTC")).total_seconds()
            if quote_age is None or quote_age < -2 or quote_age > MAX_QUOTE_AGE_SECONDS:
                store.publish_jev(idle_review("Market data is stale; Jev is paused until a fresh exchange quote arrives."))
            else:
                review = reviewer.review(snapshot, quote)
                store.publish_jev(review)
                append_review(review)
        delay = max(0.0, next_run + interval_seconds - time.monotonic())
        time.sleep(delay)
        next_run = max(next_run + interval_seconds, time.monotonic())
