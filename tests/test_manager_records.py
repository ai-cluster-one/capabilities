from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
DEPLOYMENT = REPO / "capabilities" / "deployment" / "bin" / "deployment"
TELEGRAM = REPO / "capabilities" / "telegram" / "bin" / "telegram"

sys.path.insert(0, str(REPO / "contract"))
import store as S  # noqa: E402


def _project(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    root = tmp_path / "consumer"
    envelope = root / "capabilities"
    envelope.mkdir(parents=True)
    project_id = str(uuid.uuid4())
    slug = "fixture-" + project_id[:8]
    (envelope / "project.json").write_text(json.dumps({
        "schema": "capabilities.project.v1", "id": project_id,
        "slug": slug, "store": "db",
    }))
    store_path = tmp_path / "store.db"
    with S.SQLiteStore.open(str(store_path)) as store:
        store.migrate()
        store.project_register(project_id, slug)
        store.config_set("capabilities", "policy", "deployment",
                         {"enabled": True}, ("project", slug))
        store.config_set("capabilities", "policy", "telegram",
                         {"enabled": True}, ("project", slug))
    env = dict(os.environ)
    env.update({
        "CLAUDE_PROJECT_DIR": str(root),
        "CAPABILITIES_PROJECT_ENVELOPE": str(envelope),
        "CAPABILITIES_STORE_URL": str(store_path),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    })
    return root, env, store_path


def _run(argv: list[str], root: Path, env: dict[str, str], check: bool = True):
    result = subprocess.run(
        argv, cwd=root, env=env, text=True, capture_output=True, timeout=120)
    if check and result.returncode != 0:
        raise AssertionError(
            f"{argv} exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}")
    return result


def test_manager_get_and_set_use_the_project_records_adapter(tmp_path):
    root, env, _store_path = _project(tmp_path)
    written = json.loads(_run(
        [str(MANAGER), "set", "telegram", "setting", "tail_size", "40"],
        root, env).stdout)
    assert written["records"]["mode"] == "db"
    value = json.loads(_run(
        [str(MANAGER), "get", "telegram", "setting", "tail_size"],
        root, env).stdout)
    assert value == 40


def test_manager_lists_database_policy_without_a_settings_file(tmp_path):
    root, env, _store_path = _project(tmp_path)
    assert not (root / "capabilities" / "settings.json").exists()
    listed = json.loads(_run([str(MANAGER), "list"], root, env).stdout)
    assert listed["enabled_not_installed"] == ["deployment", "telegram"]


def test_manager_policy_verbs_write_the_database_not_settings_json(tmp_path):
    root, env, store_path = _project(tmp_path)
    _run([str(MANAGER), "enable", "slack", "--project"], root, env)
    assert not (root / "capabilities" / "settings.json").exists()
    identity = json.loads((root / "capabilities" / "project.json").read_text())
    with S.SQLiteStore.open(str(store_path)) as store:
        assert store.config_get(
            "capabilities", "policy", "slack",
            S.Scopes(identity["slug"], include_global=False)) == {"enabled": True}
    _run([str(MANAGER), "inherit", "slack", "--project"], root, env)
    with S.SQLiteStore.open(str(store_path)) as store:
        assert store.config_get(
            "capabilities", "policy", "slack",
            S.Scopes(identity["slug"], include_global=False)) is None


def test_manager_does_not_take_another_writers_collection(tmp_path):
    root, env, _store_path = _project(tmp_path)
    refused = _run(
        [str(MANAGER), "set", "telegram", "identifier", "chat", "1"],
        root, env, check=False)
    assert refused.returncode == 6
    assert json.loads(refused.stderr)["error"]["code"] == "record_writer"


def test_manager_ids_renders_identifiers_from_the_store(tmp_path):
    root, env, store_path = _project(tmp_path)
    identity = json.loads((root / "capabilities" / "project.json").read_text())
    with S.SQLiteStore.open(str(store_path)) as store:
        store.config_set("deployment", "identifier", "target", "local",
                         ("project", identity["slug"]), note="the active target")
    rendered = _run([str(MANAGER), "ids", "deployment"], root, env).stdout
    assert "**target**: `local`" in rendered
    assert "the active target" in rendered


def test_capability_context_edit_put_round_trips_a_store_document(tmp_path):
    root, env, store_path = _project(tmp_path)
    identity = json.loads((root / "capabilities" / "project.json").read_text())
    with S.SQLiteStore.open(str(store_path)) as store:
        store.context_put("deployment", "context", "before\n",
                          ("project", identity["slug"]), activate=True)
    checkout = json.loads(_run(
        [str(DEPLOYMENT), "context", "edit", "context"], root, env).stdout)
    path = Path(checkout["path"])
    assert path.read_text() == "before\n"
    path.write_text("after\n")
    landed = json.loads(_run(
        [str(DEPLOYMENT), "context", "put", "context"], root, env).stdout)
    assert landed["put"] == "context"
    with S.SQLiteStore.open(str(store_path)) as store:
        doc = store.context_read("deployment", "context", S.Scopes(identity["slug"]))
    assert doc is not None and doc["body"] == "after\n"


def test_telegram_service_status_is_initialized_from_store_records(tmp_path):
    root, env, store_path = _project(tmp_path)
    identity = json.loads((root / "capabilities" / "project.json").read_text())
    scope = ("project", identity["slug"])
    settings = {
        "connection": "local",
        "assistant_name": "Marvin",
        "direct_messages": {"mode": "allowed_users", "default_role": "direct_user"},
        "allowed_users": {},
        "allowed_groups": {},
        "defaults": {"worker": "stub"},
    }
    with S.SQLiteStore.open(str(store_path)) as store:
        for key, value in settings.items():
            store.config_set("telegram", "setting", key, value, scope)
        store.config_set("telegram", "setting", "connection.default", "local", scope)
        store.config_set("telegram", "connection", "local", {
            "api_id": 12345,
            "expected_account_id": 42,
        }, scope)
        store.config_set("telegram", "grant", "local", {
            "enabled": True,
            "allow_write": True,
        }, scope)
        store.context_put("telegram", "context", "test context\n", scope,
                          activate=True)
    status = json.loads(_run(
        [str(TELEGRAM), "service", "status", "--connection", "local"],
        root, env).stdout)
    assert status["initialized"] is True
    assert status["connection"] == "local"
    assert status["expected_account_id"] == 42
