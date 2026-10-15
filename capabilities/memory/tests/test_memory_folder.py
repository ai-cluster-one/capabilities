"""Where `memory adopt` reads a project's memory folder: `memory` in the body
root ContextKit names for a bound project, else <root>/memory."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from _cli import load


@pytest.fixture
def cli():
    return load()


def bound(root: Path, tmp_path: Path, monkeypatch, *, answer: str, exit_code: int = 0) -> Path:
    """Bind `root` to ContextKit and put a stand-in `contextkit` on PATH that
    records its arguments and answers `path --json` with `answer`."""
    (root / ".contextkit").mkdir(parents=True)
    (root / ".contextkit" / "config.toml").write_text("version = 1\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    fake = bin_dir / "contextkit"
    fake.write_text("#!/bin/sh\n"
                    f'echo "$@" >> "{calls}"\n'
                    f"cat <<'EOF'\n{answer}\nEOF\n"
                    f"exit {exit_code}\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    return calls


@pytest.mark.parametrize("body_root,expected", [(".", "memory"), ("agent", "agent/memory")])
def test_a_bound_project_reads_memory_in_the_body_root_contextkit_names(
        cli, tmp_path, monkeypatch, body_root, expected):
    root = tmp_path / "project"
    calls = bound(root, tmp_path, monkeypatch,
                  answer=json.dumps({"project": str(root), "body_root": body_root, "layers": {}}))
    folder, source = cli._memory_folder(root)
    assert folder == root / expected
    assert "contextkit path --json" in source
    assert calls.read_text().split() == ["path", "--json"]


def test_a_project_without_contextkit_reads_its_root_memory(cli, tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    assert cli._memory_folder(root) == (root / "memory", "<root>/memory")


@pytest.mark.parametrize("answer,exit_code", [
    ("unknown command", 2),
    ("not json", 0),
    (json.dumps({"layers": {}}), 0),
    (json.dumps({"body_root": "../elsewhere"}), 0),
])
def test_a_body_root_contextkit_does_not_name_falls_back_to_the_root(
        cli, tmp_path, monkeypatch, answer, exit_code):
    root = tmp_path / "project"
    bound(root, tmp_path, monkeypatch, answer=answer, exit_code=exit_code)
    folder, source = cli._memory_folder(root)
    assert folder == root / "memory"
    assert "named no body root" in source
