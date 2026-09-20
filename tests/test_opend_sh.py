"""The OpenD launcher starts the binary named by OPEND_DIR without handing the rest of ``.env`` to it."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


LAUNCHER = Path(__file__).parent.parent / "opend.sh"
needs_zsh = pytest.mark.skipif(shutil.which("zsh") is None, reason="opend.sh is a zsh script")


def fake_gateway(folder: Path) -> Path:
    """An OpenD stand-in that records its environment and arguments next to itself."""
    binary = folder / "OpenD.app" / "Contents" / "MacOS" / "OpenD"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        '#!/bin/sh\nhere="$(dirname "$0")"\nenv > "$here/env.txt"\necho "$@" > "$here/args.txt"\ntouch "$here/done"\n'
    )
    binary.chmod(0o755)
    return binary


def launch(project: Path, *arguments: str) -> subprocess.CompletedProcess:
    launcher = project / "opend.sh"
    shutil.copy(LAUNCHER, launcher)
    minimal = {"PATH": os.environ["PATH"], "HOME": str(project)}
    return subprocess.run(["zsh", str(launcher), *arguments], cwd=project, env=minimal, capture_output=True, text=True, timeout=10)


@needs_zsh
def test_launcher_starts_the_binary_from_opend_dir_without_exporting_the_env_file(tmp_path):
    gateway = tmp_path / "gateway dir"
    binary = fake_gateway(gateway)
    (tmp_path / ".env").write_text(
        'TYPESAFE_API_KEY=ts-file-secret\nMOOMOO_PORT=11111\nWATCH_SYMBOL=SPCX\n'
        f'OPEND_DIR="{gateway}"\n'
    )

    result = launch(tmp_path)

    assert result.returncode == 0, result.stderr
    environment = (binary.parent / "env.txt").read_text()
    assert "ts-file-secret" not in environment
    assert "MOOMOO_PORT" not in environment and "WATCH_SYMBOL" not in environment
    assert (binary.parent / "args.txt").read_text().strip() == ""


@needs_zsh
def test_launcher_daemon_mode_passes_the_remembered_login_flags(tmp_path):
    gateway = tmp_path / "opend"
    binary = fake_gateway(gateway)
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=ts-file-secret\n")

    result = launch(tmp_path, "daemon")
    result.check_returncode()
    for _ in range(50):
        if (binary.parent / "done").exists():
            break
        time.sleep(0.1)

    assert "pid" in result.stdout
    assert (binary.parent / "args.txt").read_text().split() == ["-login_by_remember=1", "-console=0"]
    assert "ts-file-secret" not in (binary.parent / "env.txt").read_text()


@needs_zsh
def test_launcher_explains_a_missing_binary(tmp_path):
    (tmp_path / ".env").write_text(f"OPEND_DIR={tmp_path / 'nowhere'}\n")

    result = launch(tmp_path)

    assert result.returncode == 1
    assert "OpenD binary not found at" in result.stderr and "nowhere" in result.stderr
