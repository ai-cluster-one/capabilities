"""A capability's title and summary in Russian and Estonian.

A manifest may carry `translations`: an object keyed by `ru` or `et`, each an
object of an optional `title` and `description`. The manager checks its shape
where it is declared and requires it nowhere, so a capability without it still
installs; the catalogue entry carries it where declared.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest


MANAGER = Path(__file__).resolve().parents[1] / "bin" / "capabilities"


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_manifest_translations", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()


def _manifest(**extra) -> dict:
    return {"name": "probe", "title": "Probe", "summary": "A probe.",
            "credentials": {"scope": "project", "keys": []},
            "docs": {"base": "", "topics": []}, "state": False,
            "inventory": None, "post_install": [], **extra}


def test_a_manifest_without_translations_still_validates():
    assert M._validate_manifest("probe", _manifest()) == []


def test_declared_translations_validate_and_each_key_is_optional():
    translations = {"ru": {"title": "Зонд", "description": "Проба."},
                    "et": {"description": "Sond."}}
    assert M._validate_manifest("probe", _manifest(translations=translations)) == []


@pytest.mark.parametrize("translations, starts", [
    ({"de": {"title": "Sonde"}}, "translations.de is not a known language"),
    ({"ru": {"summary": "Проба."}}, "translations.ru.summary is not one of"),
    ({"ru": {"title": "Две\nстроки"}}, "translations.ru.title is not one line"),
    ({"et": {"description": "Kaks\nrida"}}, "translations.et.description is not one line"),
    ({"ru": {"title": "ж" * 41}}, "translations.ru.title is longer than 40"),
    ({"et": {"title": ""}}, "translations.et.title is not a non-empty string"),
    ({"ru": "Зонд"}, "translations.ru is not an object"),
    (["ru"], "translations is not an object"),
])
def test_a_translation_outside_the_shape_is_refused(translations, starts):
    problems = M._validate_manifest("probe", _manifest(translations=translations))
    assert len(problems) == 1 and problems[0].startswith(starts), problems


def test_a_description_may_span_lines_where_the_english_summary_does():
    manifest = _manifest(summary="A probe.\nSecond line.",
                         translations={"ru": {"description": "Проба.\nВторая строка."}})
    assert M._validate_manifest("probe", manifest) == []


def test_the_catalogue_entry_carries_translations_only_where_declared(tmp_path):
    capdir = tmp_path / "probe"
    (capdir / "bin").mkdir(parents=True)
    (capdir / "bin" / "probe").write_text("NAME = 'probe'\n")
    translations = {"et": {"title": "Sond"}}
    entry = M._catalog_entry_of(capdir, "A probe.", {"title": "Probe",
                                                     "translations": translations})
    assert entry["translations"] == translations
    assert "translations" not in M._catalog_entry_of(capdir, "A probe.", {"title": "Probe"})
