#!/usr/bin/env python3
"""One reader prints the change log of a capability, the manager, and the contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
BUNDLE = REPO / "capabilities" / "resend"
TITLE = "# resend — change log\n"
OLDER = "## 2026-01-01 — Older change\n\nWhat the installed copy already has.\n"
NEWER = "## 2026-02-01 — Newer change\n\nWhat arrived after the install.\n"


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
        "CAPABILITIES_SOURCE": "http://127.0.0.1:9",
        "XDG_CONFIG_HOME": str(tmp / "config"),
        "XDG_STATE_HOME": str(tmp / "state"),
        "XDG_DATA_HOME": str(tmp / "data"),
        "XDG_CACHE_HOME": str(tmp / "cache"),
        "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
    })
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env, cap_home, bin_dir


def _run(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([str(MANAGER), *args], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=300)


def _ok(args: list[str], env: dict[str, str], cwd: Path) -> dict:
    proc = _run(args, env, cwd)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _error(proc: subprocess.CompletedProcess) -> dict:
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    return json.loads(lines[-1])["error"]


def _headings(result: dict) -> list[str]:
    return [entry["heading"] for entry in result["entries"]]


def test_a_capability_log_rides_the_install_and_marks_the_installed_version(
    tmp_path: Path,
) -> None:
    env, cap_home, _bin = _env(tmp_path)
    source = tmp_path / "src" / "resend"
    shutil.copytree(BUNDLE, source)
    (source / "CHANGELOG.md").write_text(TITLE + "\n" + OLDER)
    _ok(["install", "resend", "--from", str(source)], env, tmp_path)
    installed_log = cap_home / "resend" / "CHANGELOG.md"
    assert installed_log.read_text() == TITLE + "\n" + OLDER

    (source / "CHANGELOG.md").write_text(TITLE + "\n" + NEWER + "\n" + OLDER)
    since = _ok(["changelog", "resend"], env, tmp_path)
    assert since["view"] == "since-installed"
    assert since["installed"]["version"] == "2026-01-01 — Older change"
    assert since["log"] == str(source / "CHANGELOG.md")
    assert _headings(since) == ["2026-02-01 — Newer change"]
    assert since["entries"][0]["body"] == "What arrived after the install."

    whole = _ok(["changelog", "resend", "--all"], env, tmp_path)
    assert whole["view"] == "all"
    assert _headings(whole) == ["2026-02-01 — Newer change", "2026-01-01 — Older change"]

    installed_log.unlink()
    predates = _ok(["changelog", "resend"], env, tmp_path)
    assert predates["installed"]["log"] is None
    assert predates["installed"]["version"] is None
    assert _headings(predates) == _headings(whole)
    assert "predates" in predates["note"]


def test_a_capability_not_installed_prints_its_whole_log(tmp_path: Path) -> None:
    env, _cap_home, _bin = _env(tmp_path)
    source = tmp_path / "ghost"
    source.mkdir()
    (source / "CHANGELOG.md").write_text("# ghost — change log\n\n" + NEWER + "\n" + OLDER)
    result = _ok(["changelog", "ghost", "--from", str(source)], env, tmp_path)
    assert result["installed"] is None
    assert result["view"] == "all"
    assert len(result["entries"]) == 2

    (source / "CHANGELOG.md").unlink()
    missing = _run(["changelog", "ghost", "--from", str(source)], env, tmp_path)
    assert missing.returncode == 3
    assert _error(missing)["code"] == "changelog_not_found"

    unreachable = _run(["changelog", "ghost"], env, tmp_path)
    assert unreachable.returncode == 5, unreachable.stderr
    assert _error(unreachable)["code"] == "network_error"


def test_the_manager_and_the_contract_read_the_release_on_path(tmp_path: Path) -> None:
    env, _cap_home, bin_dir = _env(tmp_path)
    release = tmp_path / "release"
    (release / "bin").mkdir(parents=True)
    shutil.copy2(MANAGER, release / "bin" / "capabilities")
    for rel in ("contract/preamble.py", "contract/CHANGELOG.md"):
        (release / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, release / rel)
    (release / "manager").mkdir()
    (release / "manager" / "CHANGELOG.md").write_text("# capabilities — change log\n")
    (bin_dir / "capabilities").symlink_to(release / "bin" / "capabilities")

    published = [entry.split("\n", 1)[0] for entry in
                 (REPO / "manager" / "CHANGELOG.md").read_text().split("\n## ")[1:]]
    manager = _ok(["changelog", "--manager", "--from", str(REPO)], env, tmp_path)
    assert manager["target"] == "capabilities"
    assert manager["installed"] == {"release": str(release.resolve()),
                                    "log": str(release.resolve() / "manager" / "CHANGELOG.md"),
                                    "version": None}
    assert _headings(manager) == published

    shutil.copy2(REPO / "manager" / "CHANGELOG.md", release / "manager" / "CHANGELOG.md")
    current = _ok(["changelog", "--manager", "--from", str(REPO)], env, tmp_path)
    assert current["entries"] == []
    assert current["installed"]["version"] == (published[0] if published else None)

    contract = _ok(["changelog", "--contract", "--from", str(REPO)], env, tmp_path)
    assert contract["target"] == "contract"
    assert contract["entries"] == []

    (bin_dir / "capabilities").unlink()
    absent = _ok(["changelog", "--contract", "--from", str(REPO)], env, tmp_path)
    assert absent["installed"] is None and absent["view"] == "all"


def test_the_logs_are_manager_release_assets_and_targets_are_exclusive(
    tmp_path: Path,
) -> None:
    manifest = json.loads((REPO / ".capability-source" / "manager-release.json").read_text())
    assert {"manager/CHANGELOG.md", "contract/CHANGELOG.md"} <= set(manifest["assets"])
    env, _cap_home, _bin = _env(tmp_path)
    for args in (["changelog"], ["changelog", "resend", "--manager"],
                 ["changelog", "--manager", "--contract"],
                 ["changelog", "--manager", "--source", "official"],
                 ["changelog", "resend", "--from", str(REPO), "--source", "official"]):
        proc = _run(args, env, tmp_path)
        assert proc.returncode == 6, (args, proc.stderr)
