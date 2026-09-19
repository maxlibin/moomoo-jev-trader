"""Watch one-minute setups from Moomoo OpenD and serve the dashboard.

Run from the project's ``.venv`` while OpenD is running and logged in::

    python run_live.py

Settings come from ``.env``: MOOMOO_HOST and MOOMOO_PORT for the gateway,
WATCH_SYMBOL and WATCH_BENCHMARK for the instruments.
"""

import os
import threading
import webbrowser

from dotenv import load_dotenv

from ai_analysis import gate_decision, gate_mode_from_env, idle_review, run_jev_forever
from dashboard import create_app
from executor import run_executor_forever
from moomoo_feed import MoomooFeed
from moomoo_trade import MoomooBroker
from state import STORE
from trader import DEFAULT_LIMITS, limits_from_env
from watcher import run_forever, run_quotes_forever


DASHBOARD_URL = "http://127.0.0.1:8050"
HISTORY_BARS = 1000
FORMING_BARS = 2
TICK_OFFSET_SECONDS = 2
QUOTE_INTERVAL_SECONDS = 2.0
EXECUTOR_INTERVAL_SECONDS = 2.0
JEV_INTERVAL_SECONDS = 5.0


def dashboard_review(_snapshot) -> dict:
    return STORE.latest_jev() or idle_review("Waiting for the first five-second Jev review.")


def serve_dashboard(environment: str, gate_mode: str) -> None:
    app = create_app(STORE, dashboard_review, environment, gate_mode)
    threading.Timer(1.0, lambda: webbrowser.open(DASHBOARD_URL)).start()
    app.run(host="127.0.0.1", port=8050, debug=False, use_reloader=False)


def start_auto_trading(host: str, port: int, symbol: str, gate_mode: str) -> str:
    """Connect the broker and start the executor thread; returns the trading environment name."""
    environment = os.getenv("MOOMOO_TRADE_ENV", "SIMULATE").upper()
    broker = MoomooBroker(host, port, environment, os.getenv("MOOMOO_SECURITY_FIRM", "FUTUSG").upper())
    broker.connect()
    account = broker.account()
    limits = limits_from_env(os.environ, DEFAULT_LIMITS)
    entry_gate = (lambda snapshot, now: gate_decision(STORE.latest_jev(), snapshot, now)) if gate_mode == "enforce" else None
    print(f"Auto trading ON in {environment}: equity {account.equity:,.2f}, cash {account.cash:,.2f}, "
          f"limits risk {limits.risk_fraction:.0%}/trade, max position {limits.max_position_fraction:.0%}, "
          f"max {limits.max_trades_per_day} trades/day, daily loss stop {limits.max_daily_loss_fraction:.0%}, "
          f"Jev gate {gate_mode}")
    threading.Thread(
        target=run_executor_forever,
        args=(STORE, broker, symbol, limits, EXECUTOR_INTERVAL_SECONDS, entry_gate),
        name="executor",
        daemon=True,
    ).start()
    return environment


def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    symbol = os.getenv("WATCH_SYMBOL", "SPCX").upper()
    benchmark = os.getenv("WATCH_BENCHMARK", "QQQ").upper()
    host = os.getenv("MOOMOO_HOST", "127.0.0.1")
    port = int(os.getenv("MOOMOO_PORT", "11111"))
    feed = MoomooFeed(host, port)
    feed.connect([symbol, benchmark])
    auto_trade = os.getenv("AUTO_TRADE", "false").lower() in {"1", "true", "yes"}
    gate_mode = gate_mode_from_env(os.environ)
    environment = start_auto_trading(host, port, symbol, gate_mode) if auto_trade else "OFF"
    threading.Thread(target=run_jev_forever, args=(STORE, JEV_INTERVAL_SECONDS), name="jev", daemon=True).start()
    threading.Thread(target=serve_dashboard, args=(environment, gate_mode), name="dashboard", daemon=True).start()
    threading.Thread(
        target=run_quotes_forever,
        args=(feed.last_quote, lambda name: feed.minute_bars(name, FORMING_BARS), symbol, STORE, QUOTE_INTERVAL_SECONDS),
        name="quotes",
        daemon=True,
    ).start()
    print(
        f"Dashboard at {DASHBOARD_URL}; evaluating {symbol} every minute from OpenD, "
        "live quote every 2s, Jev every 5s. Press Ctrl-C to stop."
    )
    try:
        run_forever(lambda name: feed.minute_bars(name, HISTORY_BARS), symbol, benchmark, STORE, TICK_OFFSET_SECONDS)
    except KeyboardInterrupt:
        print("\nWatcher stopped.")
    finally:
        feed.close()


if __name__ == "__main__":
    main()
