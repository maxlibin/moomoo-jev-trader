"""Five-second TypeSafe Jev market review and optional entry gate.

Jev does not replace the deterministic signal or risk engines. It receives a
compact view of recent price action and answers three independent questions.
The answers are published for the dashboard and may, when explicitly enabled,
block a new long entry. Broker execution, sizing, stops, and exits remain in
normal Python code. The model, thresholds, and freshness limits are a
``JevSettings`` resolved once by the entry point, never read at import time.
"""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

import pandas as pd
from typesafe_sdk import Choice, Noul, RetryPolicy, TypeSafeClient

from settings import count, name, overrides_from_env, positive, probability
from signals import BUY_SIGNALS
from state import LiveQuote, SignalStore, Snapshot, quote_age_seconds


REVIEWS_JOURNAL = Path(__file__).parent / "logs" / "jev_reviews.jsonl"
GATE_MODES = ("shadow", "enforce")


@dataclass(frozen=True)
class JevSettings:
    """Model, request timeout, approval thresholds, freshness limits, and evidence size for the review."""

    model: str
    timeout_seconds: float
    min_up_probability: float
    min_quality_probability: float
    max_stop_first_probability: float
    max_gate_age_seconds: float
    max_quote_age_seconds: float
    recent_bars: int


DEFAULT_SETTINGS = JevSettings(
    model="jev-latest",
    timeout_seconds=4.0,
    min_up_probability=0.60,
    min_quality_probability=0.55,
    max_stop_first_probability=0.45,
    max_gate_age_seconds=15.0,
    max_quote_age_seconds=60.0,
    recent_bars=30,
)


ENV_SETTING_FIELDS = {
    "JEV_MODEL": ("model", name),
    "JEV_TIMEOUT_SECONDS": ("timeout_seconds", positive),
    "JEV_MIN_UP_PROBABILITY": ("min_up_probability", probability),
    "JEV_MIN_QUALITY_PROBABILITY": ("min_quality_probability", probability),
    "JEV_MAX_STOP_FIRST_PROBABILITY": ("max_stop_first_probability", probability),
    "JEV_MAX_GATE_AGE_SECONDS": ("max_gate_age_seconds", positive),
    "JEV_MAX_QUOTE_AGE_SECONDS": ("max_quote_age_seconds", positive),
    "JEV_RECENT_BARS": ("recent_bars", count),
}


def settings_from_env(env: Mapping[str, str]) -> JevSettings:
    """``DEFAULT_SETTINGS`` with any of the ENV_SETTING_FIELDS variables present in ``env`` applied; a bad value refuses to start."""
    return overrides_from_env(env, ENV_SETTING_FIELDS, DEFAULT_SETTINGS)


def gate_mode_from_env(env: Mapping[str, str]) -> str:
    """``JEV_ENTRY_GATE_MODE`` normalised to ``shadow`` or ``enforce``; anything else refuses to start."""
    mode = env.get("JEV_ENTRY_GATE_MODE", "shadow").strip().lower()
    if mode not in GATE_MODES:
        raise ValueError(f"JEV_ENTRY_GATE_MODE must be one of {list(GATE_MODES)}, got {env['JEV_ENTRY_GATE_MODE']!r}")
    return mode


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


def _recent_bars(snapshot: Snapshot, recent_bars: int) -> list[dict]:
    if snapshot.session is None:
        return []
    columns = [column for column in ("open", "high", "low", "close", "volume") if column in snapshot.session.columns]
    rows = []
    for at, row in snapshot.session[columns].tail(recent_bars).iterrows():
        rows.append({"time": at.isoformat(), **{column: _number(row[column]) for column in columns}})
    return rows


def build_state(snapshot: Snapshot, quote: LiveQuote, recent_bars: int) -> dict:
    """Build compact market evidence for one Jev evaluation from the last ``recent_bars`` completed candles."""
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
        "recent_completed_bars": _recent_bars(snapshot, recent_bars),
    }


def idle_review(reason: str, model: str, status: str = "idle") -> dict:
    return {
        "status": status,
        "verdict": "WAIT",
        "summary": reason,
        "risks": [],
        "model": model,
        "approved": False,
    }


class JevReviewer:
    """Owns a reusable TypeSafe client and evaluates the latest market state under ``settings``."""

    def __init__(self, client: Optional[TypeSafeClient], settings: JevSettings):
        self.client = client
        self.settings = settings

    def review(self, snapshot: Snapshot, quote: LiveQuote) -> dict:
        settings = self.settings
        if snapshot.setup is None:
            return idle_review("No completed setup is available.", settings.model)
        if quote.price is None:
            return idle_review("No live quote is available.", settings.model)
        if self.client is None:
            return idle_review("Set TYPESAFE_API_KEY to enable five-second Jev reviews.", settings.model, "disabled")

        started = time.perf_counter()
        try:
            response = self.client.system_one(state=build_state(snapshot, quote, settings.recent_bars), questions=QUESTIONS)
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
                and up_probability >= settings.min_up_probability
                and supportive_probability >= settings.min_quality_probability
                and stop_probability <= settings.max_stop_first_probability
            )
            risks = []
            if not is_candidate:
                risks.append("The deterministic rules do not currently have a long entry.")
            if up_probability < settings.min_up_probability:
                risks.append(f"Up probability {up_probability:.0%} is below {settings.min_up_probability:.0%}.")
            if supportive_probability < settings.min_quality_probability:
                risks.append(f"Supportive-entry probability {supportive_probability:.0%} is below {settings.min_quality_probability:.0%}.")
            if stop_probability > settings.max_stop_first_probability:
                risks.append(f"Stop-first risk {stop_probability:.0%} exceeds {settings.max_stop_first_probability:.0%}.")
            return {
                "status": "complete",
                "verdict": "PASS" if approved else "WAIT",
                "summary": (
                    f"5m direction: {direction.choice} (up {up_probability:.0%}); "
                    f"entry quality: {quality.choice} (supportive {supportive_probability:.0%}); "
                    f"stop-first risk {stop_probability:.0%}."
                ),
                "risks": risks[:3],
                "model": getattr(response, "model", settings.model),
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
                **idle_review(f"Jev request failed: {exc}", settings.model, "error"),
                "candle": snapshot.setup.timestamp.isoformat(),
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "latency_ms": round((time.perf_counter() - started) * 1000),
            }


def reviewer_from_env(env: Mapping[str, str]) -> JevReviewer:
    """A reviewer for the ``JEV_*`` settings in ``env``; without ``TYPESAFE_API_KEY`` every review reports ``disabled``."""
    settings = settings_from_env(env)
    api_key = env.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        return JevReviewer(None, settings)
    client = TypeSafeClient(api_key=api_key, model=settings.model, timeout=settings.timeout_seconds, retry=RetryPolicy(max_retries=0))
    return JevReviewer(client, settings)


def gate_decision(
    review: Optional[dict], snapshot: Snapshot, now: pd.Timestamp, max_gate_age_seconds: float
) -> tuple[Optional[bool], str]:
    """Translate the latest review into ready/pass/reject for the executor.

    ``None`` means wait for a fresh Jev result without consuming the candle; a
    review older than ``max_gate_age_seconds`` is not fresh.
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
    if age < -2 or age > max_gate_age_seconds:
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


def run_jev_forever(store: SignalStore, interval_seconds: float, reviewer: JevReviewer) -> None:
    """Evaluate the newest snapshot and live quote on a five-second schedule."""
    settings = reviewer.settings
    next_run = time.monotonic()
    while True:
        snapshot, quote = store.latest(), store.latest_quote()
        if snapshot is not None and snapshot.error is None and quote is not None and quote.error is None:
            quote_age = quote_age_seconds(quote, pd.Timestamp.now(tz="UTC"))
            if quote_age is None or quote_age < -2 or quote_age > settings.max_quote_age_seconds:
                store.publish_jev(idle_review("Market data is stale; Jev is paused until a fresh exchange quote arrives.", settings.model))
            else:
                review = reviewer.review(snapshot, quote)
                store.publish_jev(review)
                append_review(review)
        delay = max(0.0, next_run + interval_seconds - time.monotonic())
        time.sleep(delay)
        next_run = max(next_run + interval_seconds, time.monotonic())
