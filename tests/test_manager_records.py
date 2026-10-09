from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"


def _project(tmp_path: Path, store: str | None = None) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "consumer"
    envelope = root / "capabilities"
    envelope.mkdir(parents=True)
    project_id = str(uuid.uuid4())
    slug = "fixture-" + project_id[:8]
    identity = {"schema": "capabilities.project.v1", "id": project_id, "slug": slug}
    if store is not None:
        identity["store"] = store
    (envelope / "project.json").write_text(json.dumps(identity))
    (envelope / "settings.json").write_text(json.dumps(
        {"capabilities": {"deployment": {"enabled": True}, "telegram": {"enabled": True}}}))
    env = dict(os.environ)
    for key in ("CAPABILITIES_READ_ONLY", "CAPABILITIES_PROJECT_ID",
                "CAPABILITIES_PROJECT_ID_ROOT", "CAPABILITIES_PROJECT_ENVELOPE_ROOT"):
        env.pop(key, None)
    env.update({
        "CLAUDE_PROJECT_DIR": str(root),
        "CAPABILITIES_PROJECT_ENVELOPE": str(envelope),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    })
    return root, env


def _run(argv: list[str], root: Path, env: dict[str, str], check: bool = True):
    result = subprocess.run(
        argv, cwd=root, env=env, text=True, capture_output=True, timeout=120)
    if check and result.returncode != 0:
        raise AssertionError(
            f"{argv} exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}")
    return result


def _no_database_file(tmp_path: Path) -> None:
    assert not list(tmp_path.rglob("*.db"))


def test_manager_get_and_set_use_the_project_records_files(tmp_path):
    root, env = _project(tmp_path)
    written = json.loads(_run(
        [str(MANAGER), "set", "telegram", "setting", "tail_size", "40"],
        root, env).stdout)
    assert written["records"]["mode"] == "files"
    value = json.loads(_run(
        [str(MANAGER), "get", "telegram", "setting", "tail_size"],
        root, env).stdout)
    assert value == 40
    settings = root / "capabilities" / "telegram" / "service" / "settings.json"
    assert json.loads(settings.read_text())["tail_size"] == 40
    _no_database_file(tmp_path)


def test_a_project_declaring_files_keeps_working(tmp_path):
    root, env = _project(tmp_path, store="files")
    listed = json.loads(_run([str(MANAGER), "list"], root, env).stdout)
    assert listed["enabled_not_installed"] == ["deployment", "telegram"]


def test_a_project_declaring_its_records_in_a_database_is_refused(tmp_path):
    """Records are kept only in files; a declaration pointing elsewhere is
    refused rather than read as files, which would answer from records it
    does not mean."""
    root, env = _project(tmp_path, store="db")
    refused = _run([str(MANAGER), "get", "telegram", "setting", "tail_size"],
                   root, env, check=False)
    assert refused.returncode == 6
    error = json.loads(refused.stderr.strip().splitlines()[-1])["error"]
    assert error["code"] == "bad_store_mode"
    assert "files" in error["message"]
    _no_database_file(tmp_path)


def test_manager_does_not_take_another_writers_collection(tmp_path):
    root, env = _project(tmp_path)
    refused = _run(
        [str(MANAGER), "set", "telegram", "identifier", "chat", "1"],
        root, env, check=False)
    assert refused.returncode == 6
    assert json.loads(refused.stderr)["error"]["code"] == "record_writer"


def test_relabel_writes_the_identity_and_nothing_else(tmp_path):
    root, env = _project(tmp_path)
    identity = json.loads((root / "capabilities" / "project.json").read_text())
    payload = json.loads(_run([str(MANAGER), "relabel", "fixture-renamed"], root, env).stdout)
    assert payload["ok"] is True
    assert payload["previous_slug"] == identity["slug"]
    assert payload["id"] == identity["id"]
    assert "store_registry" not in payload
    written = json.loads((root / "capabilities" / "project.json").read_text())
    assert written["slug"] == "fixture-renamed"
    assert written["id"] == identity["id"]
    _no_database_file(tmp_path)
    again = json.loads(_run([str(MANAGER), "relabel", "fixture-renamed"], root, env).stdout)
    assert again["unchanged"] is True


def test_relabel_refuses_a_label_that_is_not_one(tmp_path):
    root, env = _project(tmp_path)
    before = (root / "capabilities" / "project.json").read_text()
    refused = _run([str(MANAGER), "relabel", "Not A Label"], root, env, check=False)
    assert refused.returncode == 6
    assert json.loads(refused.stderr)["error"]["code"] == "bad_name"
    assert (root / "capabilities" / "project.json").read_text() == before


def test_manager_ids_renders_identifiers_from_the_files(tmp_path):
    root, env = _project(tmp_path)
    (root / "capabilities" / "deployment").mkdir()
    (root / "capabilities" / "deployment" / "identifiers.json").write_text(json.dumps(
        {"target": {"value": "local", "note": "the active target"}}))
    rendered = _run([str(MANAGER), "ids", "deployment"], root, env).stdout
    assert "**target**: `local`" in rendered
    assert "the active target" in rendered
