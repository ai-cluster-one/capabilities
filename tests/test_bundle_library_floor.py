"""A release refuses a bundle script, other than the executable, whose own
header could run it on a contract library older than the first to read the
store setting the manager writes: install and update bring only the
executable's library forward."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
NAME = "floorfix"


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_bundle_library_floor", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()


def _header(requirement: str) -> str:
    return ("#!/usr/bin/env -S uv run --script\n# /// script\n"
            "# requires-python = \">=3.11\"\n# dependencies = [\n"
            f"#     \"{requirement}\",\n# ]\n# ///\nprint('ok')\n")


def _bundle(tmp_path: Path, files: dict[str, str]) -> Path:
    capdir = tmp_path / "src" / "capabilities" / NAME
    script = capdir / "bin" / NAME
    script.parent.mkdir(parents=True)
    script.write_text(M._capability_skeleton(NAME, False))
    script.chmod(0o755)
    for rel, text in files.items():
        (capdir / rel).parent.mkdir(parents=True, exist_ok=True)
        (capdir / rel).write_text(text)
    return capdir


def _floor_failures(failures) -> list[str]:
    return [f for f in failures if f.startswith("contract-range: service/")]


@pytest.mark.parametrize("requirement", ["capabilities-contract>=0.12,<1",
                                         "capabilities-contract>=0.37,<1",
                                         "capabilities-contract"])
def test_a_service_script_on_an_older_floor_is_refused_at_release(tmp_path, requirement):
    capdir = _bundle(tmp_path, {"service/profiles.py": _header(requirement)})
    problems = M._bundle_library_problems(capdir)
    assert len(problems) == 1 and "service/profiles.py" in problems[0]
    assert "capabilities-contract>=0.38,<1" in problems[0]
    _catalog, failures = M._catalog_payload(tmp_path / "src", audit=False)
    assert _floor_failures(failures.get(NAME, []))
    _catalog, failures = M._catalog_payload_scoped(tmp_path / "src", {NAME}, {}, audit=False)
    assert _floor_failures(failures.get(NAME, []))


def test_the_executable_tests_and_scripts_on_the_floor_are_not_refused(tmp_path):
    capdir = _bundle(tmp_path, {
        "service/profiles.py": _header("capabilities-contract>=0.38,<1"),
        "service/daemon.py": _header("httpx>=0.27"),
        "tests/test_x.py": _header("capabilities-contract>=0.8,<1"),
    })
    assert M._bundle_library_problems(capdir) == []
    _catalog, failures = M._catalog_payload(tmp_path / "src", audit=False)
    assert _floor_failures(failures.get(NAME, [])) == []
