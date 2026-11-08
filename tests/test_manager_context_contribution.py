"""A capability's declared context contribution in the composed project context.

A manifest may declare `contribution`, the constant Markdown block itself.
Where the capability is enabled the manager places that block directly after
its stub line, taken from the manifest snapshot and reading no file and no
store; a declaration it does not accept costs that block alone, which one line
pointing to `capabilities update <name>` replaces. The manager validates the
declaration wherever it validates a manifest.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
NAME = "contribfix"
DECLARED = {"markdown": "\n### Recent\n\n- the first entry\n- the second entry\n\n"}
UNAVAILABLE = "The context contribution of `{name}` is unavailable: "


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_contribution_under_test", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("CAPABILITIES_READ_ONLY", "CLAUDE_PROJECT_DIR",
                "CAPABILITIES_AUTH_CONTEXT", "CAPABILITIES_PROJECT_ENVELOPE",
                "CAPABILITIES_PROJECT_ENVELOPE_ROOT", "CAPABILITIES_PROJECT_ID",
                "CAPABILITIES_PROJECT_ID_ROOT", "AGENTKIT_DB_URL",
                "CAPABILITIES_DEV_SESSION", "CAPABILITIES_WORKSPACE",
                "CAPABILITIES_CONTEXT_PROMPT"):
        env.pop(key, None)
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CAPABILITIES_BIN": str(tmp_path / "bin"),
    })
    (tmp_path / "home").mkdir(exist_ok=True)
    return env


def _manager(env: dict, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(MANAGER), *args], cwd=cwd, env=env,
                          text=True, capture_output=True, timeout=180,
                          stdin=subprocess.DEVNULL)


def _ok(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _error(result: subprocess.CompletedProcess) -> dict:
    for line in reversed(result.stderr.splitlines()):
        try:
            return json.loads(line)["error"]
        except (ValueError, KeyError, TypeError):
            continue
    raise AssertionError(result.stdout + result.stderr)


# --- composition -----------------------------------------------------------------

def _snapshot(tmp_path: Path, name: str, contribution: object = None) -> None:
    """A registry entry as composition reads it: the stub and the manifest."""
    entry = tmp_path / "registry" / name
    entry.mkdir(parents=True)
    (entry / name).write_text("#!/bin/sh\n")
    (entry / "stub").write_text(f"Work with {name} records. Run `{name} help`.\n")
    manifest = {"name": name, "docs": {"topics": []}}
    if contribution is not None:
        manifest["contribution"] = contribution
    (entry / "manifest.json").write_text(json.dumps(manifest))


def _project(tmp_path: Path, *enabled: str) -> Path:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / "capabilities").mkdir()
    (root / "capabilities" / "settings.json").write_text(json.dumps({
        "capabilities": {name: {"enabled": True} for name in enabled}}))
    return root


def _stale_file(project: Path, name: str) -> None:
    """The file an earlier release rendered, which composition no longer reads."""
    path = project / "capabilities" / name / "state" / "context.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- read from a rendered file\n")


def _ids(project: Path, name: str) -> None:
    (project / "capabilities" / name).mkdir(exist_ok=True)
    (project / "capabilities" / name / "identifiers.json").write_text(json.dumps({
        "board": {"value": "7", "note": "team board"}}))


def _sections(fragment: str) -> dict[str, str]:
    """Each capability's section of a composed fragment, keyed by name."""
    parts = re.split(r"(?m)^## (\S+)\n", fragment.split(M.CONTEXT_END, 1)[0])
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def _fragment(tmp_path: Path, project: Path) -> str:
    result = _manager(_env(tmp_path), project, "context", "--fragment")
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip().endswith(M.CONTEXT_END)
    return result.stdout


def test_a_declared_contribution_follows_its_stub_line(tmp_path):
    _snapshot(tmp_path, "demo", DECLARED)
    _snapshot(tmp_path, "plain")
    project = _project(tmp_path, "demo", "plain")
    _ids(project, "demo")

    sections = _sections(_fragment(tmp_path, project))

    assert sections["demo"].startswith(
        "\nWork with demo records. Run `demo help`.\n\n"
        "### Recent\n\n- the first entry\n- the second entry\n\n"
        "- Identifiers (1): run `capabilities ids demo`")
    assert sections["plain"].strip() == "Work with plain records. Run `plain help`."


def test_the_host_modes_carry_the_contribution(tmp_path):
    _snapshot(tmp_path, "demo", {"markdown": "- remembered here\n"})
    project = _project(tmp_path, "demo")
    env = _env(tmp_path)
    env["CLAUDE_PROJECT_DIR"] = str(project)

    _ok(_manager(env, project, "context", "--claude"))

    placed = (project / ".claude" / "rules" / "CAPABILITIES.md").read_text()
    assert "Run `demo help`.\n\n- remembered here\n" in placed


def test_the_block_is_the_declaration_and_no_file_is_read(tmp_path):
    _snapshot(tmp_path, "demo", DECLARED)
    project = _project(tmp_path, "demo")
    first = _fragment(tmp_path, project)
    _stale_file(project, "demo")

    again = _fragment(tmp_path, project)

    assert again == first
    assert "read from a rendered file" not in again
    assert "- the first entry\n- the second entry\n" in _sections(again)["demo"]


def test_a_disabled_capability_contributes_nothing(tmp_path):
    _snapshot(tmp_path, "demo", {"markdown": "- must not appear\n"})
    _snapshot(tmp_path, "plain")
    project = _project(tmp_path, "plain")

    fragment = _fragment(tmp_path, project)

    assert "must not appear" not in fragment
    assert "demo" not in _sections(fragment)


MALFORMED = {
    "the earlier file form": {"path": "state/context.md", "max_bytes": 200},
    "empty": {"markdown": ""},
    "blank": {"markdown": "\n  \n"},
    "not a string": {"markdown": 7},
    "an unknown key": {"markdown": "- must not appear\n", "extra": True},
    "over the ceiling": {"markdown": "- must not appear\n" + "x" * M.CONTRIBUTION_MAX_BYTES},
    "not an object": "- must not appear",
}


@pytest.mark.parametrize("shape", sorted(MALFORMED))
def test_a_malformed_declaration_costs_only_its_own_block(tmp_path, shape):
    _snapshot(tmp_path, "demo", MALFORMED[shape])
    _snapshot(tmp_path, "other", {"markdown": "- kept by the other\n"})
    project = _project(tmp_path, "demo", "other")
    _stale_file(project, "demo")
    _ids(project, "demo")

    fragment = _fragment(tmp_path, project)

    sections = _sections(fragment)
    demo = sections["demo"]
    assert demo.startswith("\nWork with demo records. Run `demo help`.\n\n"
                           + UNAVAILABLE.format(name="demo"))
    unavailable = [line for line in demo.splitlines() if "unavailable" in line]
    assert len(unavailable) == 1 and "declaration is malformed" in unavailable[0]
    assert unavailable[0].endswith("`capabilities update demo` installs one this manager reads.")
    assert "must not appear" not in fragment and "read from a rendered file" not in fragment
    # The capability keeps the rest of its block, and the other keeps all of its own.
    assert "- Identifiers (1): run `capabilities ids demo`" in demo
    assert sections["other"].startswith(
        "\nWork with other records. Run `other help`.\n\n- kept by the other\n\n")
    assert "unavailable" not in sections["other"]


# --- the memory marker -----------------------------------------------------------

# The line an earlier release composed above a memory contribution.
MEMORY_MARKER = "<!-- contextkit:memory-contribution -->"

def test_a_composed_memory_contribution_carries_no_marker(tmp_path):
    _snapshot(tmp_path, "memory", {"markdown": "- remembered here\n"})
    _snapshot(tmp_path, "demo", DECLARED)
    project = _project(tmp_path, "memory", "demo")

    fragment = _fragment(tmp_path, project)
    sections = _sections(fragment)

    # The manager writes no marker line above it: the contribution follows
    # the stub line as any other capability's does.
    assert MEMORY_MARKER not in fragment
    assert sections["memory"].startswith(
        "\nWork with memory records. Run `memory help`.\n\n- remembered here\n\n")


def test_a_memory_contribution_not_composed_carries_no_marker(tmp_path):
    _snapshot(tmp_path, "memory", MALFORMED["the earlier file form"])
    project = _project(tmp_path, "memory")
    _stale_file(project, "memory")

    fragment = _fragment(tmp_path, project)

    assert MEMORY_MARKER not in fragment
    assert UNAVAILABLE.format(name="memory") in fragment
    assert "read from a rendered file" not in fragment


# --- the manifest declaration ----------------------------------------------------

def _manifest(**extra) -> dict:
    return {"name": NAME, "summary": "x", "credentials": {"scope": "project", "keys": []},
            "docs": {"base": "", "topics": []}, "state": True, "inventory": None,
            "post_install": [], **extra}


ACCEPTED = [
    DECLARED,
    {"markdown": "x"},
    # Exactly the ceiling, in two-byte characters.
    {"markdown": "é" * (M.CONTRIBUTION_MAX_BYTES // 2)},
]

REFUSED = {
    "not an object": "- a block",
    "null": None,
    "an unknown key": dict(DECLARED, extra="rides in"),
    "markdown missing": {},
    "the earlier file form": {"path": "state/context.md", "max_bytes": 200},
    "empty": {"markdown": ""},
    "blank": {"markdown": " \n\t\n"},
    "a number": {"markdown": 7},
    "a list": {"markdown": ["- a block"]},
    "over the ceiling in bytes": {"markdown": "é" * (M.CONTRIBUTION_MAX_BYTES // 2 + 1)},
    "not encodable": {"markdown": "- a block \ud800"},
}


@pytest.mark.parametrize("declared", ACCEPTED)
def test_a_valid_declaration_validates(declared):
    assert M._validate_manifest(NAME, _manifest(contribution=declared)) == []


def test_a_manifest_without_a_contribution_is_validated_as_before():
    assert M._validate_manifest(NAME, _manifest()) == []


@pytest.mark.parametrize("shape", sorted(REFUSED))
def test_each_invalid_declaration_is_refused(shape):
    problems = M._validate_manifest(NAME, _manifest(contribution=REFUSED[shape]))
    assert problems and all(p.startswith("contribution") for p in problems), problems


def _script(contribution: object) -> str:
    """The manager's own core-only scaffold, declaring `contribution`."""
    text = M._capability_skeleton(NAME, True).replace(
        "TODO: describe the capability's smallest useful surface.",
        "Fixture capability for the context contribution.")
    text = text.replace(
        "Replace this scaffold check with a real readiness proof.",
        "Fixture readiness.")
    dispatch = (
        "    if argv[:1] == [\"manifest\"]:\n"
        "        _emit({\"name\": NAME, \"summary\": SUMMARY,\n"
        "               \"credentials\": {\"scope\": SCOPE, \"keys\": CRED_KEYS},\n"
        "               \"docs\": {\"base\": DOCS_BASE, \"topics\": []},\n"
        "               \"state\": STATE, \"inventory\": _inventory_declaration(),\n"
        "               \"post_install\": POST_INSTALL,\n"
        f"               \"contribution\": {contribution!r}}})\n"
        "        sys.exit(0)\n"
        "    _contract(argv)\n")
    assert "    _contract(argv)\n" in text
    return text.replace("    _contract(argv)\n", dispatch, 1)


def _surfaces(tmp_path: Path, contribution: object) -> dict[str, subprocess.CompletedProcess]:
    """Audit, source check and install of one fixture declaration."""
    env = _env(tmp_path)
    here = tmp_path / "home" / "nowhere"
    here.mkdir(parents=True)
    bundle = tmp_path / "bundles" / NAME
    (bundle / "bin").mkdir(parents=True)
    script = bundle / "bin" / NAME
    script.write_text(_script(contribution))
    script.chmod(0o755)
    out = {"audit": _manager(env, here, "audit", NAME, "--from", str(script))}
    workspace = Path(_ok(_manager(env, here, "source", "init", "fixture"))["path"])
    shutil.copytree(bundle, workspace / "capabilities" / NAME)
    out["source index"] = _manager(env, workspace, "source", "index", "fixture")
    out["source check"] = _manager(env, here, "source", "check", "fixture")
    out["install"] = _manager(env, here, "install", NAME, "--from", str(bundle))
    return out


def test_a_valid_declaration_passes_audit_source_check_and_install(tmp_path):
    for surface, result in _surfaces(tmp_path, DECLARED).items():
        assert result.returncode == 0, (surface, result.stdout + result.stderr)
    manifest = json.loads((tmp_path / "registry" / NAME / "manifest.json").read_text())
    assert manifest["contribution"] == DECLARED


def test_an_invalid_declaration_is_refused_at_audit_source_check_and_install(tmp_path):
    results = _surfaces(tmp_path, REFUSED["the earlier file form"])
    audit = results["audit"]
    assert audit.returncode == 7, audit.stdout + audit.stderr
    assert any("contribution.markdown" in f for f in json.loads(audit.stdout)["failures"])
    check = results["source check"]
    assert check.returncode == 7, check.stdout + check.stderr
    assert "contribution.markdown" in json.dumps(json.loads(check.stdout)["failures"].get(NAME))
    install = results["install"]
    assert install.returncode == 6, install.stdout + install.stderr
    error = _error(install)
    assert error["code"] == "manifest_invalid" and "contribution.markdown" in error["hint"]
    assert not (tmp_path / "registry" / NAME).exists()
