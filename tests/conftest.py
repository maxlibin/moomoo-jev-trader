"""Keep every test's fill journal in a temporary directory instead of the project's ``logs/``."""

from pathlib import Path

import pytest

import executor


@pytest.fixture(autouse=True)
def fills_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fills.csv"
    monkeypatch.setattr(executor, "FILLS_JOURNAL", path)
    return path
