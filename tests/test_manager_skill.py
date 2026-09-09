#!/usr/bin/env python3
"""Regression tests for the global awareness skill.

The skill is the only surface that tells an agent this system exists before any
project is open, and the manager reaches into agent host homes to place it —
so every test here runs against an isolated HOME and never the real machine.

Run with: python3 tests/test_manager_skill.py
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
CARRIED = REPO / "skill" / "SKILL.md"


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
    env.pop("CODEX_HOME", None)
    return env, home, cap_home


def _manager(args: list[str], env: dict[str, str], expect: int = 0) -> dict:
    proc = subprocess.run([str(MANAGER), *args], cwd=str(tmp_workdir(env)), env=env,
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != expect:
        raise AssertionError(
            f"{' '.join(args)} exited {proc.returncode}, wanted {expect}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return json.loads(proc.stdout or "{}")


def tmp_workdir(env: dict[str, str]) -> Path:
    """Run outside any real project so project findings stay out of the way."""
    work = Path(env["HOME"]) / "work"
    work.mkdir(parents=True, exist_ok=True)
    return work


@contextlib.contextmanager
def _sandbox():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        env, home, cap_home = _env(tmp)
        yield env, home, cap_home / ".manager" / "skill"


def _skill_findings(env: dict[str, str]) -> list[str]:
    proc = subprocess.run([str(MANAGER), "doctor"], cwd=str(tmp_workdir(env)), env=env,
                          capture_output=True, text=True, timeout=180)
    payload = json.loads(proc.stdout or "{}")
    return [f for f in payload.get("findings", []) if f.startswith("skill:")]


def test_the_skill_is_carried_whole_so_the_bootstrap_can_place_it() -> None:
    """The skill is an asset, not a composed string: the bootstrap must fetch it."""
    manager = MANAGER.read_text()
    assert 'SKILL_ASSET = "skill/SKILL.md"' in manager, "skill asset is not declared"
    assert "SKILL_ASSET,)" in manager, "skill asset is not a manager release asset"
    installer = (REPO / "install.sh").read_text()
    assert installer.count("skill/SKILL.md") == 2, \
        "install.sh must both fetch and stage the skill"
    assert '"$BIN_DIR/capabilities" skill' in installer, \
        "the bootstrap must make the machine aware, not wait for a later verb"


def test_the_skill_introduces_the_manager_and_names_no_machine_state() -> None:
    """Whatever is installed here belongs to `capabilities list`, not to this file."""
    text = CARRIED.read_text()
    assert text.startswith("---\nname: capabilities\n"), "frontmatter missing"
    description = next(l for l in text.splitlines() if l.startswith("description:"))
    assert "capabilit" in description.lower(), "the word that reaches this skill is absent"
    installed = {p.name for p in (REPO / "capabilities").iterdir() if p.is_dir()}
    named = {n for n in installed if n in text}
    assert not named, f"the global skill enumerates capabilities: {sorted(named)}"


def test_skill_writes_one_artifact_and_links_no_absent_host() -> None:
    with _sandbox() as (env, home, canonical):
        result = _manager(["skill"], env)
        assert (canonical / "SKILL.md").is_file(), "canonical artifact missing"
        assert result["hosts"] == [], f"linked an absent host: {result['hosts']}"
        assert (canonical / "SKILL.md").read_text() == CARRIED.read_text(), \
            "the placed artifact is not the carried skill, byte for byte"


def test_present_hosts_are_symlinked_to_the_one_artifact() -> None:
    with _sandbox() as (env, home, canonical):
        (home / ".claude").mkdir()
        (home / ".codex").mkdir()
        _manager(["skill"], env)
        for host in (home / ".claude" / "skills" / "capabilities",
                     home / ".agents" / "skills" / "capabilities"):
            assert host.is_symlink(), f"{host} is a copy, not a link"
            assert host.resolve() == canonical.resolve(), f"{host} points elsewhere"


def test_codex_is_detected_by_its_own_home_not_by_the_skill_root() -> None:
    """A fresh Codex machine has ~/.codex and no ~/.agents; it still gets the skill."""
    with _sandbox() as (env, home, canonical):
        (home / ".codex").mkdir()
        assert not (home / ".agents").exists()
        _manager(["skill"], env)
        host = home / ".agents" / "skills" / "capabilities"
        assert host.is_symlink() and host.resolve() == canonical.resolve()


def test_a_previously_written_copy_is_migrated_to_a_link() -> None:
    with _sandbox() as (env, home, canonical):
        host = home / ".claude" / "skills" / "capabilities"
        host.mkdir(parents=True)
        (host / "SKILL.md").write_text("stale content from an older manager\n")
        _manager(["skill"], env)
        assert host.is_symlink() and host.resolve() == canonical.resolve()


def test_foreign_content_in_a_host_directory_is_never_destroyed() -> None:
    with _sandbox() as (env, home, canonical):
        host = home / ".claude" / "skills" / "capabilities"
        host.mkdir(parents=True)
        (host / "NOTES.md").write_text("hand written\n")
        _manager(["skill"], env)
        assert not host.is_symlink(), "a directory holding other files was replaced"
        assert (host / "NOTES.md").read_text() == "hand written\n"
        assert (host / "SKILL.md").read_text() == (canonical / "SKILL.md").read_text()


def test_doctor_reports_every_way_the_awareness_surface_drifts() -> None:
    with _sandbox() as (env, home, canonical):
        (home / ".claude").mkdir()
        _manager(["skill"], env)
        assert _skill_findings(env) == [], "a freshly written skill reports drift"

        (canonical / "SKILL.md").unlink()
        assert any("missing" in f for f in _skill_findings(env)), "missing artifact unreported"

        _manager(["skill"], env)
        (canonical / "SKILL.md").write_text("---\nname: capabilities\ndescription: x\n---\n")
        assert any("differs" in f for f in _skill_findings(env)), "edited artifact unreported"

        _manager(["skill"], env)
        host = home / ".claude" / "skills" / "capabilities"
        host.unlink()
        assert any(str(host) in f for f in _skill_findings(env)), "missing host link unreported"

        _manager(["skill"], env)
        assert _skill_findings(env) == [], "`capabilities skill` did not repair the drift"


if __name__ == "__main__":
    tests = [
        ("the skill is carried whole as a release asset", test_the_skill_is_carried_whole_so_the_bootstrap_can_place_it),
        ("the skill names no machine state", test_the_skill_introduces_the_manager_and_names_no_machine_state),
        ("skill writes one artifact and links no absent host", test_skill_writes_one_artifact_and_links_no_absent_host),
        ("present hosts are symlinked to the one artifact", test_present_hosts_are_symlinked_to_the_one_artifact),
        ("codex is detected by its own home", test_codex_is_detected_by_its_own_home_not_by_the_skill_root),
        ("a previously written copy is migrated to a link", test_a_previously_written_copy_is_migrated_to_a_link),
        ("foreign content in a host directory is never destroyed", test_foreign_content_in_a_host_directory_is_never_destroyed),
        ("doctor reports every way the awareness surface drifts", test_doctor_reports_every_way_the_awareness_surface_drifts),
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
