"""A capability's declared context contribution in the composed project context.

A manifest may declare `contribution`, a Markdown file inside the capability's
own state folder in the project envelope and the most bytes to compose from
it. Where the capability is enabled the manager places that file directly
after its stub line, reading the file and nothing else; whatever stops the
read costs that block alone, which one line pointing to `<name> render`
replaces. The manager validates the declaration wherever it validates a
manifest.
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
DECLARED = {"path": "state/context.md", "max_bytes": 200}


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


def _render(project: Path, name: str, text: str | bytes,
            rel: str = "state/context.md") -> Path:
    path = project / "capabilities" / name / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)
    return path


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
    _render(project, "demo", "\n### Recent\n\n- the first entry\n- the second entry\n\n")
    _ids(project, "demo")

    sections = _sections(_fragment(tmp_path, project))

    assert sections["demo"].startswith(
        "\nWork with demo records. Run `demo help`.\n\n"
        "### Recent\n\n- the first entry\n- the second entry\n\n"
        "- Identifiers (1): run `capabilities ids demo`")
    assert sections["plain"].strip() == "Work with plain records. Run `plain help`."


def test_the_host_modes_carry_the_contribution(tmp_path):
    _snapshot(tmp_path, "demo", DECLARED)
    project = _project(tmp_path, "demo")
    _render(project, "demo", "- remembered here\n")
    env = _env(tmp_path)
    env["CLAUDE_PROJECT_DIR"] = str(project)

    _ok(_manager(env, project, "context", "--claude"))

    placed = (project / ".claude" / "rules" / "CAPABILITIES.md").read_text()
    assert "Run `demo help`.\n\n- remembered here\n" in placed


def test_an_empty_contribution_adds_nothing(tmp_path):
    _snapshot(tmp_path, "demo", DECLARED)
    project = _project(tmp_path, "demo")
    _render(project, "demo", "\n  \n")

    section = _sections(_fragment(tmp_path, project))["demo"]

    assert section.strip() == "Work with demo records. Run `demo help`."


def test_a_disabled_capability_contributes_nothing(tmp_path):
    _snapshot(tmp_path, "demo", DECLARED)
    _snapshot(tmp_path, "plain")
    project = _project(tmp_path, "plain")
    _render(project, "demo", "- must not appear\n")

    fragment = _fragment(tmp_path, project)

    assert "must not appear" not in fragment
    assert "demo" not in _sections(fragment)


def _break(kind: str, tmp_path: Path, project: Path) -> None:
    if kind == "missing":
        return
    if kind == "oversized":
        _render(project, "demo", "x" * (DECLARED["max_bytes"] + 1))
    elif kind == "not utf-8":
        _render(project, "demo", b"\xff\xfe broken")
    elif kind == "a directory":
        (project / "capabilities" / "demo" / "state" / "context.md").mkdir(parents=True)
    elif kind == "outside the state folder":
        outside = tmp_path / "elsewhere.md"
        outside.write_text("- read from elsewhere\n")
        link = project / "capabilities" / "demo" / "state" / "context.md"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
    else:
        raise AssertionError(kind)


FAILURES = ["missing", "oversized", "not utf-8", "a directory", "outside the state folder"]


def _assert_only_demo_degraded(fragment: str) -> None:
    sections = _sections(fragment)
    demo = sections["demo"]
    assert demo.startswith("\nWork with demo records. Run `demo help`.\n\n"
                           "The context contribution of `demo` is unavailable: ")
    unavailable = [line for line in demo.splitlines() if "unavailable" in line]
    assert len(unavailable) == 1 and unavailable[0].endswith("`demo render` rebuilds it.")
    assert "read from elsewhere" not in demo
    # The capability keeps the rest of its block, and the other keeps all of its own.
    assert "- Identifiers (1): run `capabilities ids demo`" in demo
    assert sections["other"].startswith(
        "\nWork with other records. Run `other help`.\n\n- kept by the other\n\n")
    assert "unavailable" not in sections["other"]


@pytest.mark.parametrize("kind", FAILURES)
def test_each_unreadable_contribution_costs_only_its_own_block(tmp_path, kind):
    _snapshot(tmp_path, "demo", DECLARED)
    _snapshot(tmp_path, "other", DECLARED)
    project = _project(tmp_path, "demo", "other")
    _render(project, "other", "- kept by the other\n")
    _break(kind, tmp_path, project)
    _ids(project, "demo")

    _assert_only_demo_degraded(_fragment(tmp_path, project))


@pytest.mark.parametrize("declared", [
    {"path": "/etc/passwd", "max_bytes": 200},
    {"path": "state/../identifiers.json", "max_bytes": 200},
    {"path": "state/context.md", "max_bytes": 0},
    {"path": "state/context.md", "max_bytes": 200, "extra": True},
    "state/context.md",
])
def test_a_malformed_declaration_costs_only_its_own_block(tmp_path, declared):
    _snapshot(tmp_path, "demo", declared)
    _snapshot(tmp_path, "other", DECLARED)
    project = _project(tmp_path, "demo", "other")
    _render(project, "other", "- kept by the other\n")
    _render(project, "demo", "- must not appear\n")
    _ids(project, "demo")

    fragment = _fragment(tmp_path, project)

    _assert_only_demo_degraded(fragment)
    assert "declaration is malformed" in _sections(fragment)["demo"]
    assert "must not appear" not in fragment


def test_the_contribution_is_read_from_the_envelope_the_project_resolves(tmp_path):
    """The envelope is asked for, never assumed at <root>/capabilities."""
    _snapshot(tmp_path, "demo", DECLARED)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    envelope = project / "body" / "capabilities"
    envelope.mkdir(parents=True)
    (envelope / "settings.json").write_text(json.dumps({
        "capabilities": {"demo": {"enabled": True}}}))
    (envelope / "demo" / "state").mkdir(parents=True)
    (envelope / "demo" / "state" / "context.md").write_text("- from the moved envelope\n")
    env = _env(tmp_path)
    env.update({"CAPABILITIES_PROJECT_ENVELOPE": str(envelope),
                "CAPABILITIES_PROJECT_ENVELOPE_ROOT": str(project)})

    result = _manager(env, project, "context", "--fragment")

    assert result.returncode == 0, result.stderr
    assert "Run `demo help`.\n\n- from the moved envelope\n" in result.stdout


# --- the manifest declaration ----------------------------------------------------

def _manifest(**extra) -> dict:
    return {"name": NAME, "summary": "x", "credentials": {"scope": "project", "keys": []},
            "docs": {"base": "", "topics": []}, "state": True, "inventory": None,
            "post_install": [], **extra}


ACCEPTED = [
    DECLARED,
    {"path": "state/context.md", "max_bytes": 1},
    {"path": "state/render/block.md", "max_bytes": M.CONTRIBUTION_MAX_BYTES},
]

REFUSED = {
    "not an object": "state/context.md",
    "null": None,
    "an unknown key": dict(DECLARED, extra="rides in"),
    "path missing": {"max_bytes": 200},
    "an absolute path": dict(DECLARED, path="/tmp/context.md"),
    "a path outside state": dict(DECLARED, path="context.md"),
    "a path escaping state": dict(DECLARED, path="state/../connections.json"),
    "a dot segment": dict(DECLARED, path="state/./context.md"),
    "an empty segment": dict(DECLARED, path="state//context.md"),
    "a backslash": dict(DECLARED, path="state\\context.md"),
    "not markdown": dict(DECLARED, path="state/context.txt"),
    "the state folder itself": dict(DECLARED, path="state/"),
    "max_bytes missing": {"path": "state/context.md"},
    "max_bytes zero": dict(DECLARED, max_bytes=0),
    "max_bytes negative": dict(DECLARED, max_bytes=-1),
    "max_bytes a string": dict(DECLARED, max_bytes="200"),
    "max_bytes a boolean": dict(DECLARED, max_bytes=True),
    "max_bytes over the ceiling": dict(DECLARED, max_bytes=M.CONTRIBUTION_MAX_BYTES + 1),
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
    results = _surfaces(tmp_path, REFUSED["a path escaping state"])
    audit = results["audit"]
    assert audit.returncode == 7, audit.stdout + audit.stderr
    assert any("contribution.path" in f for f in json.loads(audit.stdout)["failures"])
    check = results["source check"]
    assert check.returncode == 7, check.stdout + check.stderr
    assert "contribution.path" in json.dumps(json.loads(check.stdout)["failures"].get(NAME))
    install = results["install"]
    assert install.returncode == 6, install.stdout + install.stderr
    error = _error(install)
    assert error["code"] == "manifest_invalid" and "contribution.path" in error["hint"]
    assert not (tmp_path / "registry" / NAME).exists()
