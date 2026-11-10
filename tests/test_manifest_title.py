"""A capability's fixed human name and the Lucide name of its line icon.

A manifest may carry `title` and `lucide_icon`. The manager checks the shape of
each where it is declared and requires neither at install, so a capability
from before them still installs; the catalogue refuses a title two
capabilities share.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest


MANAGER = Path(__file__).resolve().parents[1] / "bin" / "capabilities"


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_manifest_title", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()


def _manifest(**extra) -> dict:
    return {"name": "probe", "summary": "A probe.",
            "credentials": {"scope": "project", "keys": []},
            "docs": {"base": "", "topics": []}, "state": False,
            "inventory": None, "post_install": [], **extra}


def test_a_manifest_without_title_or_lucide_icon_still_validates():
    assert M._validate_manifest("probe", _manifest()) == []


def test_a_declared_title_and_lucide_icon_validate():
    assert M._validate_manifest(
        "probe", _manifest(title="Probe Tool",
                           lucide_icon="circle-question-mark")) == []


@pytest.mark.parametrize("title", ["", "  ", "Two\nLines", " Padded", "x" * 41, 7, None])
def test_a_title_that_is_not_one_bounded_line_is_refused(title):
    problems = M._validate_manifest("probe", _manifest(title=title))
    assert len(problems) == 1 and problems[0].startswith("title "), problems


@pytest.mark.parametrize("icon", ["", "List-Checks", "list_checks", "-list", "list-", "a--b", 3])
def test_a_lucide_icon_outside_the_closed_pattern_is_refused(icon):
    problems = M._validate_manifest("probe", _manifest(lucide_icon=icon))
    assert len(problems) == 1 and problems[0].startswith("lucide_icon "), problems


def test_the_catalogue_refuses_a_title_two_capabilities_share():
    entries = {"a": {"title": "Probe"}, "b": {"title": "probe"}, "c": {"title": "Other Probe"},
               "d": {"summary": "no title"}}
    failures: dict = {}
    M._catalog_title_failures(entries, failures)
    assert sorted(entries) == ["c", "d"]
    assert sorted(failures) == ["a", "b"]
    assert "also the title of b" in failures["a"][0]
