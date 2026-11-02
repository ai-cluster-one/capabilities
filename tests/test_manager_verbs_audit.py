"""The node and service verbs are the manager's: `capabilities audit` refuses a
capability whose help names one and that answers it as its own, and accepts
one that answers it with exit 6 `moved`.

The capability these tests drive is the manager's own scaffold given one
domain verb, `node add`, either carried out or refused as moved.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
NAME = "nvfix"


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_verbs_audit", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()

ANSWERS = {
    "carried out": '''    if cleaned[:2] == ["node", "add"]:
        _emit({"node": cleaned[2:]})
        return
''',
    "moved": '''    if cleaned[:2] == ["node", "add"]:
        _die(6, "moved", "the standard agent node is the manager's",
             "capabilities node add <label> --server <uuid>")
''',
}


def _fixture(tmp_path: Path, answer: str) -> Path:
    text = M._capability_skeleton(NAME, False)
    text = text.replace(
        f"  {NAME} inventory [--network]\n",
        f"  {NAME} inventory [--network]\n  {NAME} node add <label> --server <uuid>\n", 1)
    text = text.replace('    if cleaned == ["doctor"]:',
                        ANSWERS[answer] + '    if cleaned == ["doctor"]:', 1)
    assert ANSWERS[answer] in text
    script = tmp_path / "src" / NAME
    script.parent.mkdir(parents=True)
    script.write_text(text)
    script.chmod(0o755)
    return script


def _audit(tmp_path: Path, script: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CAPABILITIES_", "CLAUDE_"))}
    home = tmp_path / "home"
    home.mkdir()
    env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "XDG_DATA_HOME": str(tmp_path / "data"),
                "XDG_CACHE_HOME": str(tmp_path / "cache"),
                "CAPABILITIES_HOME": str(tmp_path / "registry"),
                "CAPABILITIES_BIN": str(tmp_path / "bin")})
    return subprocess.run([str(MANAGER), "audit", NAME, "--from", str(script)],
                          cwd=home, env=env, text=True, capture_output=True,
                          timeout=600)


def _manager_verbs(result) -> list[str]:
    return [f for f in json.loads(result.stdout)["failures"]
            if f.startswith("manager-verbs:")]


def test_a_capability_that_answers_a_manager_node_verb_as_its_own_is_refused(tmp_path):
    result = _audit(tmp_path, _fixture(tmp_path, "carried out"))
    assert result.returncode == 7, result.stderr
    failures = _manager_verbs(result)
    assert len(failures) == 1, failures
    assert f"`{NAME} node add`" in failures[0]
    assert "capabilities node add" in failures[0]


def test_a_capability_that_answers_it_as_moved_passes(tmp_path):
    result = _audit(tmp_path, _fixture(tmp_path, "moved"))
    assert _manager_verbs(result) == [], result.stdout
