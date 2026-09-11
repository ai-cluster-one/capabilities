"""The caller's own project root is refused before any peer is spawned.

A peer started in the project the caller already stands in re-reads files the
caller can read itself and pays a fresh prompt cache to do it, and the waste is
silent because the answer comes back correct. These tests pin the refusal to
path identity of the project root: every spelling of the same root is refused,
and a subdirectory, a git worktree at another path, and an unrelated project
still dispatch.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "askproject", CAPABILITY / "askproject")
    if path.is_file()), CAPABILITY / "bin" / "askproject")


# Records that it ran, so a refusal is proven by the absence of the marker
# rather than by the exit code alone.
CLAUDE_FAKE = r'''#!/usr/bin/env python3
import json
import os
import sys

open(os.environ["PEER_MARKER"], "w").close()
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "PEER ANSWER", "session_id": "claude-session",
    "duration_ms": 1, "num_turns": 1, "total_cost_usd": 0.01, "usage": {},
}), flush=True)
'''


def _project(path: Path) -> Path:
    """A directory the root detector recognises as a project root."""
    envelope = path / "capabilities"
    envelope.mkdir(parents=True, exist_ok=True)
    (envelope / "settings.json").write_text(json.dumps({
        "capabilities": {"askproject": {"enabled": True}},
    }) + "\n")
    return path


def _run(tmp_path: Path, cwd: Path, target: str, *extra: str):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake = fake_bin / "claude"
    fake.write_text(CLAUDE_FAKE)
    fake.chmod(0o755)

    marker = tmp_path / "peer-ran"
    if marker.exists():
        marker.unlink()

    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["PEER_MARKER"] = str(marker)
    env.pop("CLAUDE_PROJECT_DIR", None)

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), target, "what is here?", "--quiet", *extra],
        cwd=cwd, env=env, text=True, capture_output=True, timeout=30)
    return proc, marker.exists()


def _caller(tmp_path: Path) -> Path:
    return _project(tmp_path / "caller")


@pytest.mark.parametrize("spelling", [
    "{root}",            # the plain absolute path
    ".",                 # relative to the caller's cwd
    "{root}/",           # a trailing slash
    "{root}/sub/..",     # an uncollapsed ..
    "{link}",            # a symlink to the root
])
def test_every_spelling_of_the_caller_root_is_refused(tmp_path, spelling):
    caller = _caller(tmp_path)
    (caller / "sub").mkdir(exist_ok=True)
    link = tmp_path / "caller-link"
    if not link.exists():
        link.symlink_to(caller, target_is_directory=True)

    proc, peer_ran = _run(
        tmp_path, caller, spelling.format(root=caller, link=link))

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert peer_ran is False
    error = json.loads(proc.stdout)["error"]
    assert "this project itself" in error
    assert "read the files directly" in error


def test_the_caller_root_reached_from_a_subdirectory_is_refused(tmp_path):
    caller = _caller(tmp_path)
    sub = caller / "sub"
    sub.mkdir(exist_ok=True)

    proc, peer_ran = _run(tmp_path, sub, "..")

    assert proc.returncode == 1
    assert peer_ran is False
    assert "this project itself" in json.loads(proc.stdout)["error"]


def test_the_refusal_is_legible_on_the_text_output_path(tmp_path):
    caller = _caller(tmp_path)

    proc, peer_ran = _run(tmp_path, caller, str(caller), "--text")

    assert proc.returncode == 1
    assert peer_ran is False
    assert proc.stdout == ""
    assert "this project itself" in proc.stderr
    assert "read the files directly" in proc.stderr


def test_a_subdirectory_of_the_caller_project_still_dispatches(tmp_path):
    caller = _caller(tmp_path)
    sub = caller / "sub"
    sub.mkdir(exist_ok=True)

    proc, peer_ran = _run(tmp_path, caller, str(sub))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert peer_ran is True
    assert json.loads(proc.stdout)["answer"] == "PEER ANSWER"


def test_an_unrelated_project_still_dispatches(tmp_path):
    caller = _caller(tmp_path)
    other = _project(tmp_path / "other")

    proc, peer_ran = _run(tmp_path, caller, str(other))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert peer_ran is True


def test_a_caller_without_a_project_root_still_dispatches(tmp_path):
    # $HOME is never a project root, so a cwd directly under it has none.
    home = tmp_path / "home"
    bare = home / "bare"
    bare.mkdir(parents=True)
    target = _project(tmp_path / "other")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake = fake_bin / "claude"
    fake.write_text(CLAUDE_FAKE)
    fake.chmod(0o755)
    marker = tmp_path / "peer-ran"

    # No project supplies the gate here, so the global scope has to.
    config = tmp_path / "config" / "capabilities"
    config.mkdir(parents=True, exist_ok=True)
    (config / "settings.json").write_text(json.dumps({
        "capabilities": {"askproject": {"enabled": True}},
    }) + "\n")

    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["PEER_MARKER"] = str(marker)
    env.pop("CLAUDE_PROJECT_DIR", None)

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(target), "what is here?", "--quiet"],
        cwd=bare, env=env, text=True, capture_output=True, timeout=30)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert marker.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_a_git_worktree_of_the_same_repository_still_dispatches(tmp_path):
    caller = _caller(tmp_path)
    subprocess.run(["git", "init", "-q", "."], cwd=caller, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=caller, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=caller, check=True)
    subprocess.run(["git", "add", "-A"], cwd=caller, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=caller, check=True)
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "worktree", "add", "-q", str(worktree), "-b", "wt"],
                   cwd=caller, check=True)

    # Same repository, another path: a different project, so it dispatches.
    proc, peer_ran = _run(tmp_path, caller, str(worktree))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert peer_ran is True


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_a_caller_inside_a_nested_worktree_is_not_refused_its_enclosing_checkout(tmp_path):
    """`.git` is a file in a linked worktree, which the root markers do not see,
    so root detection settles on the enclosing checkout. The guard must abstain
    there rather than refuse a legitimate dispatch."""
    caller = _caller(tmp_path)
    subprocess.run(["git", "init", "-q", "."], cwd=caller, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=caller, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=caller, check=True)
    subprocess.run(["git", "add", "-A"], cwd=caller, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=caller, check=True)
    nested = caller / ".worktrees" / "feat"
    subprocess.run(["git", "worktree", "add", "-q", str(nested), "-b", "feat"],
                   cwd=caller, check=True)
    assert (nested / ".git").is_file()

    proc, peer_ran = _run(tmp_path, nested, str(caller))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert peer_ran is True


def test_the_session_key_is_not_canonicalised(tmp_path):
    """Sessions are resumed against the raw target string; rewriting the key
    would orphan every session already recorded."""
    caller = _caller(tmp_path)
    other = _project(tmp_path / "other")
    spelling = f"{other}/../other"

    proc, peer_ran = _run(tmp_path, caller, spelling)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert peer_ran is True

    state = json.loads(
        (caller / "capabilities" / "askproject" / "state" / "sessions.json").read_text())
    assert list(state) == [spelling]
