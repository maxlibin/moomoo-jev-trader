"""The published ``.env.example`` starts watch-only through the real parsers, and Git never tracks a populated ``.env``."""

import subprocess
from pathlib import Path

from dotenv import dotenv_values

from ai_analysis import DEFAULT_SETTINGS, gate_mode_from_env, reviewer_from_env, settings_from_env
from moomoo_trade import MoomooBroker
from trader import DEFAULT_LIMITS, limits_from_env


PROJECT = Path(__file__).parent.parent


def test_env_example_starts_watch_only_in_the_simulated_account_with_jev_disabled():
    env = {name: value for name, value in dotenv_values(PROJECT / ".env.example").items() if value is not None}

    assert env["AUTO_TRADE"] == "false"
    assert gate_mode_from_env(env) == "shadow"
    assert reviewer_from_env(env).client is None
    assert settings_from_env(env) == DEFAULT_SETTINGS
    assert limits_from_env(env, DEFAULT_LIMITS) == DEFAULT_LIMITS
    broker = MoomooBroker(env["MOOMOO_HOST"], int(env["MOOMOO_PORT"]), env["MOOMOO_TRADE_ENV"], env["MOOMOO_SECURITY_FIRM"])
    assert broker.environment == "SIMULATE"
    assert "OPEND_DIR" not in env


def test_git_ignores_a_populated_env_and_trading_logs_but_tracks_the_example():
    def ignored(path: str) -> bool:
        result = subprocess.run(["git", "check-ignore", "-q", path], cwd=PROJECT, capture_output=True, text=True)
        assert result.returncode in (0, 1), result.stderr
        return result.returncode == 0

    assert ignored(".env") and ignored(".env.local") and ignored("logs/fills.csv") and ignored("logs/jev_reviews.jsonl")
    assert not ignored(".env.example")
    tracked = subprocess.run(["git", "ls-files"], cwd=PROJECT, capture_output=True, text=True, check=True).stdout.split()
    assert ".env.example" in tracked and "LICENSE" in tracked and ".github/workflows/tests.yml" in tracked
    assert not any(path == ".env" or path.startswith("logs/") for path in tracked)
