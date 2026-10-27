#!/usr/bin/env python3
"""An update of a capability that stops is recorded for its doctor, until one lands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
BUNDLE = REPO / "capabilities" / "resend"


def _env(tmp: Path) -> dict[str, str]:
    home = tmp / "home"
    cap_home = home / ".capabilities"
    bin_dir = tmp / "bin"
    for path in (home, cap_home, bin_dir):
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "CAPABILITIES_HOME": str(cap_home),
        "CAPABILITIES_BIN": str(bin_dir),
        "CAPABILITIES_SOURCE": "http://127.0.0.1:9",
        "XDG_CONFIG_HOME": str(tmp / "config"),
        "XDG_STATE_HOME": str(tmp / "state"),
        "XDG_DATA_HOME": str(tmp / "data"),
        "XDG_CACHE_HOME": str(tmp / "cache"),
        "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
    })
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env


def _run(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([str(MANAGER), *args], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=300)


def test_a_failed_update_is_recorded_and_the_next_landed_update_removes_it(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    record = Path(env["CAPABILITIES_HOME"]) / ".update-failed" / "resend.json"
    source = tmp_path / "src" / "resend"
    shutil.copytree(BUNDLE, source)
    installed = _run(["install", "resend", "--from", str(source)], env, tmp_path)
    assert installed.returncode == 0, installed.stderr
    assert not record.exists()

    shutil.move(source, tmp_path / "moved")
    failed = _run(["update", "resend"], env, tmp_path)
    assert failed.returncode == 3, failed.stderr
    error = json.loads(failed.stderr.splitlines()[-1])["error"]
    written = json.loads(record.read_text())
    assert written["name"] == "resend"
    assert written["error"] == error
    assert written["at"]

    listed = _run(["list"], env, tmp_path)
    assert listed.returncode == 0, listed.stderr
    assert ".update-failed" not in listed.stdout

    shutil.move(tmp_path / "moved", source)
    landed = _run(["update", "resend"], env, tmp_path)
    assert landed.returncode == 0, landed.stderr
    assert not record.exists()
