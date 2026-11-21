"""`capabilities audit` holds a capability's help to the layered law
(SHEBANG.md, "Agent-first help is the surface"): a short menu, no per-flag
detail in it, every verb it names answering `help <verb>`, and an unknown
`help <x>` answered with the pointer to the menu. A bundle the manager names
as pending reports its findings instead of failing, until it conforms.

The capability these tests drive is the manager's own scaffold given one
domain verb, `greet`.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
NAME = "helpfix"

# The pending set as this law was introduced. It only shrinks as each bundle
# is converted; a name beyond these is never added to it.
PENDING_AT_INTRODUCTION = frozenset({
    "agentrooms", "agenttemplates", "asana", "askproject", "assets", "automations",
    "calcomc", "calendar", "callva", "callvatest", "clickup", "context", "coolify",
    "deployment", "directo", "emta", "fathom", "geminitalk", "ical", "inbox",
    "instagram", "journal", "macmail", "mailbox", "memory", "notion", "railwayc",
    "rclonec", "resend", "routine", "signoz", "simplbooks", "skyscanner", "slack",
    "slacker", "stripe", "tasks", "teams", "telegram", "telegrambot", "vapic",
    "whatsapp", "windmill", "youtrack",
})


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_help_menu", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()

MENU_LINE = f"  {NAME} greet <who>              say hello to one person\n"
PAGE = (f'HELP_VERBS: dict[str, str] = {{"greet": "{NAME} greet <who> [--loud]\\n\\n'
        '  Says hello. Exit codes: 0; 6 without <who>.\\n"}')
DISPATCH = '''    if cleaned[:1] == ["greet"]:
        _emit({"hello": cleaned[1:]})
        return
'''


def _fixture(tmp_path: Path, break_: str | None = None) -> Path:
    text = M._capability_skeleton(NAME, True)
    line, page = MENU_LINE, PAGE
    if break_ == "no page":
        page = "HELP_VERBS: dict[str, str] = {}"
    elif break_ == "flag in the menu":
        line = MENU_LINE + "      --loud                  shout it\n"
    elif break_ == "past the ceiling":
        line = MENU_LINE + "".join(f"  {NAME} greet <who>  hello once more, {i}\n"
                                   for i in range(200))
    text = text.replace("\nVerbs:\n", "\nVerbs:\n" + line, 1)
    text = text.replace("HELP_VERBS: dict[str, str] = {}", page, 1)
    text = text.replace('    if cleaned == ["doctor"]:', DISPATCH + '    if cleaned == ["doctor"]:', 1)
    assert DISPATCH in text and line in text
    script = tmp_path / "src" / NAME / "bin" / NAME
    script.parent.mkdir(parents=True)
    script.write_text(text)
    script.chmod(0o755)
    return script


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("CAPABILITIES_", "CLAUDE_")):
            monkeypatch.delenv(key)
    for key, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                     ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
                     ("XDG_CACHE_HOME", "cache"), ("CAPABILITIES_HOME", "registry"),
                     ("CAPABILITIES_BIN", "bin")):
        (tmp_path / sub).mkdir(exist_ok=True)
        monkeypatch.setenv(key, str(tmp_path / sub))
    monkeypatch.chdir(tmp_path / "home")
    return tmp_path


def _help(failures: list[str]) -> list[str]:
    return [f for f in failures if M._check_id(f) == "help"]


def test_a_capability_whose_help_is_layered_passes(isolated):
    assert _help(M._audit_script(NAME, _fixture(isolated))) == []


@pytest.mark.parametrize("break_, says", [
    ("no page", f"`{NAME} help greet`"),
    ("flag in the menu", "per-flag detail"),
    ("past the ceiling", "ceiling"),
])
def test_each_departure_from_the_layered_law_fails(isolated, break_, says):
    failures = _help(M._audit_script(NAME, _fixture(isolated, break_)))
    assert failures and any(says in f for f in failures), failures


def test_a_pending_bundle_reports_and_one_that_conforms_must_leave(isolated, monkeypatch):
    monkeypatch.setattr(M, "HELP_MENU_PENDING", frozenset({NAME}))
    pending: list[str] = []
    failures = M._audit_script(NAME, _fixture(isolated, "no page"), pending_result=pending)
    assert _help(failures) == [] and any(f"`{NAME} help greet`" in p for p in pending)
    conforming = isolated / "conforming"
    conforming.mkdir()
    failures = _help(M._audit_script(NAME, _fixture(conforming)))
    assert failures and "HELP_MENU_PENDING" in failures[0], failures


def test_the_pending_set_only_shrinks_and_names_bundles_that_exist():
    assert M.HELP_MENU_PENDING <= PENDING_AT_INTRODUCTION, \
        sorted(M.HELP_MENU_PENDING - PENDING_AT_INTRODUCTION)
    bundles = {p.name for p in (REPO / "capabilities").iterdir() if p.is_dir()}
    assert M.HELP_MENU_PENDING <= bundles, sorted(M.HELP_MENU_PENDING - bundles)
