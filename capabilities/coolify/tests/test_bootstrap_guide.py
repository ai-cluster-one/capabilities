#!/usr/bin/env python3
"""Tests for the `bootstrap` guide: it ships, it is on the menu, and it keeps
the steps and the secret rules a setup session relies on.

Run with: uv run --with httpx --with pytest pytest capabilities/coolify/tests/test_bootstrap_guide.py
"""

import re
import sys
import types
from pathlib import Path

_capability = Path(__file__).resolve().parents[1]
_coolify_path = next((path for path in (
    _capability / "bin" / "coolify", _capability / "coolify")
    if path.is_file()), _capability / "bin" / "coolify")
coolify_module = types.ModuleType("coolify_bootstrap_guide")
coolify_module.__file__ = str(_coolify_path)
exec(_coolify_path.read_text(), coolify_module.__dict__)
sys.modules["coolify_bootstrap_guide"] = coolify_module

GUIDE = _capability / "guides" / "bootstrap.md"


def _steps() -> dict[int, str]:
    text = GUIDE.read_text()
    parts = re.split(r"^## (\d)\. ", text, flags=re.MULTILINE)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts), 2)}


def test_the_guide_is_on_the_menu_with_a_preview():
    entry = next(e for e in coolify_module._guide_menu() if e["topic"] == "bootstrap")
    assert entry["command"] == "coolify guide bootstrap"
    assert entry["preview"].startswith("This guide takes a setup session")


def test_the_guide_names_the_version_it_was_proven_against():
    text = GUIDE.read_text()
    assert "Proven against Coolify 4.3.23" in text
    assert "recheck each step marked *internal*" in text


def test_the_guide_has_the_eight_steps_in_order_each_with_a_check():
    steps = _steps()
    assert sorted(steps) == list(range(1, 9))
    for number, body in steps.items():
        assert "Check" in body, f"step {number} has no check"


def test_each_pitfall_sits_at_its_step():
    steps = _steps()
    assert "seeder fails silently" in steps[1]
    assert "/data/coolify/source/.env" in steps[1]
    assert "undocumented" in steps[2] and "POST only" in steps[2]
    assert "--token-stdin --global --default" in steps[2]
    assert "capabilities set coolify grant <name> '{\"enabled\": true}'" in steps[2]
    assert "--project" not in steps[2]
    assert "resets the instance setting every time Coolify starts" in steps[3]
    assert "ufw" in steps[4] and "8000" in steps[4] and "6001" in steps[4]
    assert "git-shell" in steps[5] and "uploadpack.allowReachableSHA1InWant" in steps[5]
    assert "without verifying the host key" in steps[5]
    assert "hostnossl all all all reject" in steps[6]
    assert "127.0.0.1" in steps[7]
    assert "never by sourcing" in GUIDE.read_text() and "`|`" in steps[8]


def test_step_six_ends_with_the_store_pointer():
    paragraphs = [p for p in _steps()[6].strip().split("\n\n") if p.strip()]
    assert "`capabilities store set`" in paragraphs[-1]


def test_no_secret_is_put_on_a_command_line_by_the_guide():
    text = GUIDE.read_text()
    assert "--password " not in text
    assert "--token " not in text
    assert not re.search(r"^\s*(source|\.) \S*credentials", text, flags=re.MULTILINE)
