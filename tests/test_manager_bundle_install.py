#!/usr/bin/env python3
"""Regression tests for manager bundle installation.

Run with: python3 tests/test_manager_bundle_install.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
TELEGRAM_SCRIPT = REPO / "capabilities" / "telegram" / "bin" / "telegram"
TELEGRAM_BUNDLE = TELEGRAM_SCRIPT.parent.parent


def _env(tmp: Path) -> tuple[dict[str, str], Path, Path]:
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
        "XDG_CONFIG_HOME": str(tmp / "config"),
        "XDG_STATE_HOME": str(tmp / "state"),
        "XDG_DATA_HOME": str(tmp / "data"),
        "XDG_CACHE_HOME": str(tmp / "cache"),
        "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
    })
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env, cap_home, bin_dir


def _run(argv: list[str], env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        argv, cwd=str(cwd or REPO), env=env, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(
            f"{' '.join(argv)} exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc


def _run_manager(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return _run([str(MANAGER), *args], env)


def _run_service_init(bin_dir: Path, env: dict[str, str], project: Path) -> None:
    (project / ".capabilities").mkdir(parents=True, exist_ok=True)
    proc = _run(
        [str(bin_dir / "telegram"), "service", "init", "--connection", "marvin"],
        {**env, "CLAUDE_PROJECT_DIR": str(project)},
        cwd=project,
    )
    if "FileNotFoundError" in proc.stderr:
        raise AssertionError(proc.stderr)
    assert (project / ".capabilities" / "telegram" / "service" / "settings.json").is_file()


def test_install_from_source_script_installs_bundle() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env, cap_home, bin_dir = _env(tmp)

        _run_manager(["install", "telegram", "--from", str(TELEGRAM_SCRIPT)], env)

        assert (cap_home / "telegram" / "service" / "templates" / "settings.json").is_file()
        meta = json.loads((cap_home / "telegram" / "meta.json").read_text())
        assert meta["source_type"] == "directory"
        assert meta["source"] == str(TELEGRAM_BUNDLE)
        _run_service_init(bin_dir, env, tmp / "project-install")


def test_update_migrates_script_source_to_bundle() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env, cap_home, bin_dir = _env(tmp)
        reg = cap_home / "telegram"
        reg.mkdir(parents=True)
        installed = reg / "telegram"
        shutil.copy2(TELEGRAM_SCRIPT, installed)
        installed.chmod(installed.stat().st_mode | 0o755)
        (bin_dir / "telegram").symlink_to(installed)
        (reg / "meta.json").write_text(json.dumps({
            "name": "telegram",
            "source": str(TELEGRAM_SCRIPT),
            "source_type": "script",
        }) + "\n")

        _run_manager(["update", "telegram"], env)

        assert (cap_home / "telegram" / "service" / "templates" / "settings.json").is_file()
        meta = json.loads((cap_home / "telegram" / "meta.json").read_text())
        assert meta["source_type"] == "directory"
        assert meta["source"] == str(TELEGRAM_BUNDLE)
        _run_service_init(bin_dir, env, tmp / "project-update")


if __name__ == "__main__":
    tests = [
        ("install from source script installs bundle", test_install_from_source_script_installs_bundle),
        ("update migrates script source to bundle", test_update_migrates_script_source_to_bundle),
    ]
    failed = 0
    for name, test in tests:
        try:
            test()
            print(f"ok - {name}")
        except Exception:
            failed += 1
            print(f"not ok - {name}")
            import traceback
            traceback.print_exc()
    sys.exit(1 if failed else 0)
