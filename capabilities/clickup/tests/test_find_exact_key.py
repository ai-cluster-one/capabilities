"""Tests for `find`'s custom-field lookup and the `create --external-id` preflight.

ClickUp's `=` filter on a text field matches every value that contains the
query, in any letter case, compares a currency field numerically, and ranks
the matches newest-created first, 100 to a page. The stand-in workspace below
answers the same way; nothing here reaches ClickUp.
Run with: uv run --no-project --with pytest --with httpx pytest capabilities/clickup/tests -q
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

CAPABILITY = Path(__file__).resolve().parents[1]
CLI = next((path for path in (
    CAPABILITY / "bin" / "clickup", CAPABILITY / "clickup")
    if path.is_file()), CAPABILITY / "bin" / "clickup")
module = types.ModuleType("clickup_capability")
module.__file__ = str(CLI)
exec(compile(CLI.read_text(), str(CLI), "exec"), module.__dict__)

WORKSPACE = "1000"
LIST = "2000"
KEY_FIELD = "00000000-0000-4000-8000-00000000000a"
KIND_FIELD = "00000000-0000-4000-8000-00000000000b"
KIND_OPTION = "00000000-0000-4000-8000-0000000000b1"
AMOUNT_FIELD = "00000000-0000-4000-8000-00000000000c"
NOTES_FIELD = "00000000-0000-4000-8000-00000000000d"


@pytest.fixture(autouse=True)
def isolate_records_adapter():
    module._RECORDS = None
    yield
    if module._RECORDS is not None:
        module._RECORDS.close()
    module._RECORDS = None


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A consuming project with clickup enabled and one connection that names
    its workspace and external-id field."""
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    capdir = root / "capabilities" / "clickup"
    capdir.mkdir(parents=True)
    (root / "capabilities" / "settings.json").write_text(json.dumps(
        {"capabilities": {"clickup": {"enabled": True}}}) + "\n")
    (capdir / "connections.json").write_text(json.dumps({
        "default": "main",
        "connections": {"main": {"secret_env": "CLICKUP_MAIN_TOKEN",
                                 "allow_write": True,
                                 "workspace_id": WORKSPACE,
                                 "external_id_field": KEY_FIELD}},
    }) + "\n")
    (root / ".env").write_text("CLICKUP_MAIN_TOKEN=pk_test_token\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    for key in ("CLICKUP_API_TOKEN", "CLICKUP_WORKSPACE_ID", "CLICKUP_TEAM_ID",
                "CLICKUP_EXTERNAL_ID_FIELD", "CLICKUP_EXTERNAL_ID_FIELD_ID",
                "CLICKUP_EXTERNAL_ID", "CAPABILITIES_READ_ONLY",
                "CAPABILITIES_AUTH_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(root)
    return root


class FakeClickUp:
    """A workspace whose task search answers the way ClickUp's does."""

    def __init__(self):
        self.tasks: list[dict] = []
        self.searches: list[dict] = []
        self.created: list[dict] = []
        self._clock = 1_700_000_000_000

    def add(self, key: str | None = None, *, closed: bool = False,
            parent: str | None = None, kind: int | None = None,
            amount: str | None = None, notes: str | None = None,
            custom_id: str | None = None) -> dict:
        self._clock += 1000
        fields = [{"id": KEY_FIELD, "name": "external_id", "type": "short_text",
                   **({"value": key} if key is not None else {})}]
        if kind is not None:
            fields.append({"id": KIND_FIELD, "name": "kind", "type": "drop_down",
                           "type_config": {"options": [
                               {"id": KIND_OPTION, "name": "a", "orderindex": kind}]},
                           "value": kind})
        if amount is not None:
            fields.append({"id": AMOUNT_FIELD, "name": "amount", "type": "currency",
                           "value": amount})
        if notes is not None:
            fields.append({"id": NOTES_FIELD, "name": "notes", "type": "text",
                           "value": notes})
        task = {"id": f"t{len(self.tasks) + 1}", "date_created": str(self._clock),
                "status": {"status": "complete" if closed else "open",
                           "type": "closed" if closed else "open"},
                "parent": parent, "custom_id": custom_id, "custom_fields": fields}
        self.tasks.append(task)
        return task

    @staticmethod
    def _matches(task: dict, flt: dict) -> bool:
        for f in task["custom_fields"]:
            if f["id"] != flt["field_id"] or "value" not in f:
                continue
            held, wanted = f["value"], str(flt["value"])
            if f["type"] in ("short_text", "text"):
                return wanted.lower() in held.lower()
            if f["type"] == "currency":
                try:
                    return float(held) == float(wanted)
                except ValueError:
                    return False
            options = (f.get("type_config") or {}).get("options") or []
            return any(o["id"] == wanted and o["orderindex"] == held for o in options)
        return False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v2")
        params = request.url.params
        if request.method == "GET" and path == f"/team/{WORKSPACE}/task":
            flt, = json.loads(params["custom_fields"])
            assert flt["operator"] == "="
            page = int(params.get("page", "0"))
            self.searches.append({"page": page,
                                  "include_closed": params.get("include_closed"),
                                  "subtasks": params.get("subtasks")})
            hits = [t for t in self.tasks if self._matches(t, flt)
                    and (params.get("include_closed") == "true"
                         or t["status"]["type"] != "closed")
                    and (params.get("subtasks") == "true" or not t["parent"])]
            hits.sort(key=lambda t: -int(t["date_created"]))
            return httpx.Response(200, json={
                "tasks": hits[page * 100:(page + 1) * 100],
                "last_page": (page + 1) * 100 >= len(hits)})
        if request.method == "GET" and path == f"/list/{LIST}/field":
            return httpx.Response(200, json={"fields": [
                {"id": KEY_FIELD, "name": "external_id", "type": "short_text"},
                {"id": KIND_FIELD, "name": "kind", "type": "drop_down"}]})
        if request.method == "POST" and path == f"/list/{LIST}/task":
            body = json.loads(request.content)
            task = self.add({f["id"]: f["value"]
                             for f in body.get("custom_fields") or []}.get(KEY_FIELD))
            task["name"] = body["name"]
            self.created.append(task)
            return httpx.Response(200, json=task)
        if request.method == "GET" and path.startswith("/task/"):
            assert params.get("custom_task_ids") == "true"
            assert params.get("team_id") == WORKSPACE
            wanted = path.removeprefix("/task/")
            for task in self.tasks:
                if task["custom_id"] == wanted:
                    return httpx.Response(200, json=task)
            return httpx.Response(404, json={"err": "Task not found"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def __enter__(self):
        original = httpx.Client

        def client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(self.handler)
            return original(**kwargs)

        self._patch = patch.object(module.httpx, "Client", side_effect=client)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


def run(capsys, *argv) -> tuple[int, str, str]:
    with patch.object(module.sys, "argv", ["clickup", *argv]):
        try:
            module.main()
            code = 0
        except SystemExit as e:
            code = int(e.code or 0)
    out = capsys.readouterr()
    return code, out.out, out.err


def found(capsys, *argv):
    code, out, err = run(capsys, "find", *argv)
    assert code == 0, err
    return json.loads(out)


# --- a key that is only part of longer keys is a miss -----------------------

@pytest.mark.parametrize("query", ["key-17", "ey-17", "y-1"])
def test_find_answers_null_when_the_key_only_sits_inside_longer_keys(
        project, capsys, query):
    with FakeClickUp() as fake:
        for key in ("key-170", "key-171", "pre-key-17", "key-17-copy"):
            fake.add(key)
        assert found(capsys, "--external-id", query) is None
        assert found(capsys, "--field", KEY_FIELD, "--value", query) is None


def test_find_reads_every_page_before_answering_null(project, capsys):
    with FakeClickUp() as fake:
        for n in range(250):
            fake.add(f"key-17{n}")
        assert found(capsys, "--external-id", "key-17") is None
        assert [s["page"] for s in fake.searches] == [0, 1, 2]


# --- the exact holder wins, wherever the search ranks it --------------------

@pytest.mark.parametrize("newer_containing", [1, 250])
def test_find_returns_the_exact_holder_wherever_the_search_ranks_it(
        project, capsys, newer_containing):
    with FakeClickUp() as fake:
        fake.add("key-170")
        holder = fake.add("key-17")
        for n in range(newer_containing):
            fake.add(f"other-key-17-{n}")
        assert found(capsys, "--external-id", "key-17")["id"] == holder["id"]
        assert found(capsys, "--field", KEY_FIELD, "--value", "key-17")["id"] == holder["id"]


@pytest.mark.parametrize("held, query", [
    ("key-17", "KEY-17"), ("key-17 ", "key-17"), (" key-17", "key-17")])
def test_find_compares_a_text_key_without_normalising_case_or_spaces(
        project, capsys, held, query):
    with FakeClickUp() as fake:
        fake.add(held)
        assert found(capsys, "--external-id", query) is None


def test_find_compares_a_long_text_field_exactly_too(project, capsys):
    with FakeClickUp() as fake:
        holder = fake.add(notes="note-17")
        fake.add(notes="other-note-17")
        assert found(capsys, "--field", NOTES_FIELD, "--value", "note-17")["id"] == holder["id"]
        assert found(capsys, "--field", NOTES_FIELD, "--value", "note-1") is None


# --- create dedups only against the exact key -------------------------------

def test_create_proceeds_when_the_key_only_sits_inside_longer_keys(project, capsys):
    with FakeClickUp() as fake:
        fake.add("key-170")
        fake.add("pre-key-17")
        code, out, err = run(capsys, "create", "--list", LIST, "--name", "n",
                             "--external-id", "key-17")
        assert code == 0, err
        result = json.loads(out)
        assert result["deduped"] is False
        assert result["external_id"] == "key-17"
        assert result["external_id_field"] == KEY_FIELD
        assert [t["id"] for t in fake.created] == [result["task"]["id"]]
        assert result["task"]["custom_fields"][0]["value"] == "key-17"


def test_create_dedups_against_the_exact_holder_not_a_newer_containing_key(
        project, capsys):
    with FakeClickUp() as fake:
        holder = fake.add("key-17")
        fake.add("other-key-17")
        code, out, err = run(capsys, "create", "--list", LIST, "--name", "n",
                             "--external-id", "key-17")
        assert code == 0, err
        result = json.loads(out)
        assert result == {"deduped": True, "external_id": "key-17",
                          "external_id_field": KEY_FIELD, "task": holder}
        assert fake.created == []


# --- what resolved before still resolves -------------------------------------

def test_find_still_resolves_a_closed_task_and_a_subtask(project, capsys):
    with FakeClickUp() as fake:
        parent = fake.add()
        closed = fake.add("key-17", closed=True)
        nested = fake.add("key-18", parent=parent["id"])
        assert found(capsys, "--external-id", "key-17")["id"] == closed["id"]
        assert found(capsys, "--external-id", "key-18")["id"] == nested["id"]
        assert {(s["include_closed"], s["subtasks"]) for s in fake.searches} == {
            ("true", "true")}


def test_find_resolves_a_field_named_through_its_list(project, capsys):
    with FakeClickUp() as fake:
        holder = fake.add("key-17")
        assert found(capsys, "--field", "external_id", "--list", LIST,
                     "--value", "key-17")["id"] == holder["id"]
        assert found(capsys, "--external-id", "key-17", "--field", "external_id",
                     "--list", LIST)["id"] == holder["id"]


def test_find_keeps_clickups_match_on_a_drop_down_field(project, capsys):
    with FakeClickUp() as fake:
        holder = fake.add("key-17", kind=0)
        assert found(capsys, "--field", KIND_FIELD,
                     "--value", KIND_OPTION)["id"] == holder["id"]


@pytest.mark.parametrize("query", ["0", "0.00", "0.0", "00"])
def test_find_keeps_clickups_numeric_match_on_a_currency_field(
        project, capsys, query):
    with FakeClickUp() as fake:
        holder = fake.add("key-17", amount="0")
        assert found(capsys, "--field", AMOUNT_FIELD, "--value", query)["id"] == holder["id"]
        assert found(capsys, "--field", AMOUNT_FIELD, "--value", "10") is None


def test_find_by_custom_task_id_is_unchanged(project, capsys):
    with FakeClickUp() as fake:
        holder = fake.add("key-17", custom_id="ABC-17")
        assert found(capsys, "ABC-17")["id"] == holder["id"]
        assert found(capsys, "ABC-18") is None
