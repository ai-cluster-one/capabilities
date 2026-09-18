"""Tests for railwayc's own API path: the `backups` read and doctor's API probe.

The HTTP layer is faked with httpx.MockTransport; nothing here reaches Railway.
Run with: uv run --no-project --with pytest --with httpx pytest capabilities/railwayc/tests -q
"""

from __future__ import annotations

import json
import re
import subprocess
import types
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

CAPABILITY = Path(__file__).resolve().parents[1]
CLI = next((path for path in (
    CAPABILITY / "bin" / "railwayc", CAPABILITY / "railwayc")
    if path.is_file()), CAPABILITY / "bin" / "railwayc")
module = types.ModuleType("railwayc_capability")
module.__file__ = str(CLI)
exec(compile(CLI.read_text(), str(CLI), "exec"), module.__dict__)

TOKEN = "railwayc-test-project-token"
PROJECT_ID = "project-0000"
ENV_ID = "environment-0000"
OTHER_ENV_ID = "environment-9999"

NOT_AUTHORIZED = {"errors": [{"message": "Not Authorized",
                              "extensions": {"code": "INTERNAL_SERVER_ERROR"}}],
                  "data": None}
TOKEN_NOT_FOUND = {"errors": [{"message": "Project Token not found",
                               "extensions": {"code": "INTERNAL_SERVER_ERROR"}}],
                   "data": None}


@pytest.fixture(autouse=True)
def isolate_records_adapter():
    module._RECORDS = None
    yield
    if module._RECORDS is not None:
        module._RECORDS.close()
    module._RECORDS = None


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A consuming project with railwayc enabled and one read-only connection
    whose token lives in the project .env — the standard configuration shape."""
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    capdir = root / "capabilities" / "railwayc"
    capdir.mkdir(parents=True)
    (root / "capabilities" / "settings.json").write_text(json.dumps(
        {"capabilities": {"railwayc": {"enabled": True}}}) + "\n")
    (capdir / "connections.json").write_text(json.dumps({
        "default": "prod",
        "connections": {"prod": {"secret_env": "RAILWAY_TOKEN_PROD",
                                 "allow_write": False}},
    }) + "\n")
    (root / ".env").write_text(f"RAILWAY_TOKEN_PROD={TOKEN}\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.delenv("RAILWAY_TOKEN", raising=False)
    monkeypatch.delenv("CAPABILITIES_AUTH_CONTEXT", raising=False)
    monkeypatch.chdir(root)
    return root


def _data_for(query: str, variables: dict) -> dict:
    """A healthy Railway: one volume with an instance in the token's
    environment and one in another environment."""
    if "projectToken" in query:
        return {"projectToken": {"projectId": PROJECT_ID, "environmentId": ENV_ID}}
    if "volumes" in query:
        assert variables == {"id": PROJECT_ID}
        return {"project": {"volumes": {"edges": [{"node": {
            "id": "volume-0000", "name": "data",
            "volumeInstances": {"edges": [
                {"node": {"id": "instance-0000", "serviceId": "service-0000",
                          "environmentId": ENV_ID}},
                {"node": {"id": "instance-9999", "serviceId": "service-0000",
                          "environmentId": OTHER_ENV_ID}},
            ]}}}]}}}
    if "volumeInstanceBackupScheduleList" in query:
        assert variables == {"id": "instance-0000"}
        return {"volumeInstanceBackupScheduleList": [
            {"id": "schedule-0000", "name": "daily", "kind": "DAILY",
             "cron": "0 3 * * *", "retentionSeconds": 604800,
             "createdAt": "2026-01-01T00:00:00.000Z"}]}
    if "volumeInstanceBackupList" in query:
        assert variables == {"id": "instance-0000"}
        return {"volumeInstanceBackupList": [
            {"id": "backup-0000", "externalId": "ext-0000", "name": "daily-1",
             "createdAt": "2026-01-02T03:00:00.000Z",
             "expiresAt": "2026-01-09T03:00:00.000Z",
             "scheduleId": "schedule-0000", "creatorId": None,
             "usedMB": 12, "referencedMB": 34, "volumeInstanceSizeMB": 5000}]}
    raise AssertionError(f"unexpected query: {query}")


class FakeRailway:
    """Records every request and answers from a script or the healthy default."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests: list[dict] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url),
                              "headers": dict(request.headers),
                              "query": body["query"],
                              "variables": body.get("variables") or {}})
        if self.responses:
            nxt = self.responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return httpx.Response(200, json={"data": _data_for(body["query"],
                                                           body.get("variables") or {})})

    def __enter__(self):
        original = httpx.Client

        def client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(self.handler)
            return original(**kwargs)

        self._patches = [
            patch.object(module.httpx, "Client", side_effect=client),
            patch.object(module.time, "sleep", side_effect=self.sleeps.append),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


def run(capsys, *argv) -> tuple[int, str, str]:
    with patch.object(module.sys, "argv", ["railwayc", *argv]):
        try:
            module.main()
            code = 0
        except SystemExit as e:
            code = int(e.code or 0)
    out = capsys.readouterr()
    return code, out.out, out.err


_REAL_RUN = subprocess.run


def _railway_only(fake):
    """Intercept only a `railway` child; the contract's own plumbing may still
    shell out to the manager."""
    def run_(argv, **kwargs):
        if argv and argv[0] == "railway":
            return fake(argv, **kwargs)
        return _REAL_RUN(argv, **kwargs)
    return run_


def _never(argv, **kwargs):
    raise AssertionError(f"railway must not run: {argv}")


_no_subprocess = _railway_only(_never)


# --- the verb is declared a read, not assumed one ---------------------------

def test_backups_is_declared_a_native_read():
    assert "backups" in module.READ_VERBS
    assert not (module.READ_VERBS & module.WRITE_VERBS)


def test_every_query_is_a_query_and_never_asks_me():
    queries = [module.Q_PROJECT_TOKEN, module.Q_VOLUMES, module.Q_SCHEDULES,
               module.Q_BACKUPS]
    for q in queries:
        assert q.lstrip().startswith("query")
        assert "mutation" not in q
        assert not re.search(r"\bme\b", q)


# --- backups --------------------------------------------------------------

def test_backups_on_read_only_connection_lists_instances_in_scope(project, capsys):
    with FakeRailway() as fake, patch.object(module.subprocess, "run", _no_subprocess):
        code, out, err = run(capsys, "backups")
    assert code == 0, err
    report = json.loads(out)
    assert report["connection"] == "prod"
    assert report["project_id"] == PROJECT_ID
    assert report["environment_id"] == ENV_ID
    assert [v["id"] for v in report["volumes"]] == ["volume-0000"]
    instances = report["volumes"][0]["instances"]
    assert [i["id"] for i in instances] == ["instance-0000"]   # other env filtered
    assert instances[0]["schedules"] == [{
        "id": "schedule-0000", "name": "daily", "kind": "DAILY",
        "cron": "0 3 * * *", "retention_seconds": 604800,
        "created_at": "2026-01-01T00:00:00.000Z"}]
    assert instances[0]["backups"][0]["id"] == "backup-0000"
    assert instances[0]["backups"][0]["volume_instance_size_mb"] == 5000
    # projectToken, volumes, schedules, backups — one instance in scope
    assert len(fake.requests) == 4
    for req in fake.requests:
        assert req["url"] == module.RAILWAY_GRAPHQL_URL
        assert req["headers"]["project-access-token"] == TOKEN
        assert "authorization" not in req["headers"]
        assert "mutation" not in req["query"]
        assert req["query"].lstrip().startswith("query")


def test_backups_accepts_connection_flag_anywhere(project, capsys):
    with FakeRailway():
        code, out, _ = run(capsys, "--connection", "prod", "backups")
    assert code == 0
    assert json.loads(out)["connection"] == "prod"
    with FakeRailway():
        code, _, err = run(capsys, "backups", "--connection", "nope")
    assert code == 6
    assert json.loads(err)["error"]["code"] == "unknown_connection"


def test_backups_refuses_extra_arguments_without_network(project, capsys):
    with FakeRailway() as fake:
        code, _, err = run(capsys, "backups", "extra")
    assert code == 6
    assert json.loads(err)["error"]["code"] == "bad_usage"
    assert fake.requests == []


def test_not_authorized_envelope_on_http_200_maps_to_exit_2(project, capsys):
    with FakeRailway([httpx.Response(200, json=NOT_AUTHORIZED)]) as fake:
        code, _, err = run(capsys, "backups")
    assert code == 2
    error = json.loads(err)["error"]
    assert error["code"] == "auth_failed"
    assert "Not Authorized" in error["message"]
    assert len(fake.requests) == 1          # auth is not retried


def test_rejected_token_envelope_on_http_200_maps_to_exit_2(project, capsys):
    with FakeRailway([httpx.Response(200, json=TOKEN_NOT_FOUND)]) as fake:
        code, _, err = run(capsys, "backups")
    assert code == 2
    error = json.loads(err)["error"]
    assert error["code"] == "auth_failed"
    assert "Project Token not found" in error["message"]
    assert len(fake.requests) == 1


def test_rejected_token_is_classed_once_for_both_paths():
    for text in ("Project Token not found", "Not Authorized",
                 "Unauthorized. Please provide a valid token"):
        assert module._rejected_token(text), text
    assert not module._rejected_token("ordinary upstream failure")


def test_doctor_api_block_names_a_rejected_token_as_auth(project, capsys):
    with FakeRailway([httpx.Response(200, json=TOKEN_NOT_FOUND)]), \
            patch.object(module.subprocess, "run", _fake_status()):
        code, out, _ = run(capsys, "doctor")
    assert code == 2
    api = json.loads(out)["connections"]["prod"]["api"]
    assert api["ok"] is False
    assert "Project Token not found" in api["error"]
    assert "invalid, revoked" in api["hint"]


def test_not_authorized_on_an_instance_id_maps_to_exit_2_not_3(project, capsys):
    ok = lambda q, v: httpx.Response(200, json={"data": _data_for(q, v)})
    scripted = [None, None, httpx.Response(200, json=NOT_AUTHORIZED)]

    class Fake(FakeRailway):
        def handler(self, request):
            body = json.loads(request.content)
            if len(self.requests) < 2:
                self.requests.append(body)
                return ok(body["query"], body.get("variables") or {})
            self.requests.append(body)
            return scripted[2]

    with Fake():
        code, _, err = run(capsys, "backups")
    assert code == 2
    assert json.loads(err)["error"]["code"] == "auth_failed"


def test_validation_error_maps_to_exit_6(project, capsys):
    body = {"errors": [{"message": 'Cannot query field "nope" on type "Query".',
                        "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}]}
    with FakeRailway([httpx.Response(400, json=body)]) as fake:
        code, _, err = run(capsys, "backups")
    assert code == 6
    assert json.loads(err)["error"]["code"] == "invalid_query"
    assert len(fake.requests) == 1


def test_5xx_maps_to_exit_5_after_bounded_retries(project, capsys):
    responses = [httpx.Response(503, text="bad gateway")] * module.MAX_RETRIES
    with FakeRailway(responses) as fake:
        code, _, err = run(capsys, "backups")
    assert code == 5
    error = json.loads(err)["error"]
    assert error["code"] == "server_error"
    assert error["status"] == 503
    assert len(fake.requests) == module.MAX_RETRIES
    assert fake.sleeps == [1.5 * n for n in range(1, module.MAX_RETRIES)]


def test_timeout_maps_to_exit_5_after_bounded_retries(project, capsys):
    responses = [httpx.ReadTimeout("slow")] * module.MAX_RETRIES
    with FakeRailway(responses) as fake:
        code, _, err = run(capsys, "backups")
    assert code == 5
    assert json.loads(err)["error"]["code"] == "timeout"
    assert len(fake.requests) == module.MAX_RETRIES


def test_network_error_maps_to_exit_5_without_retry(project, capsys):
    with FakeRailway([httpx.ConnectError("refused")]) as fake:
        code, _, err = run(capsys, "backups")
    assert code == 5
    assert json.loads(err)["error"]["code"] == "network_error"
    assert len(fake.requests) == 1


def test_429_honours_retry_after_then_succeeds(project, capsys):
    limited = httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")
    with FakeRailway([limited]) as fake:
        code, out, err = run(capsys, "backups")
    assert code == 0, err
    assert fake.sleeps[0] == 7.0
    assert json.loads(out)["project_id"] == PROJECT_ID


def test_429_exhausted_maps_to_exit_5(project, capsys):
    responses = [httpx.Response(429, headers={"Retry-After": "1"})] * module.MAX_RETRIES
    with FakeRailway(responses) as fake:
        code, _, err = run(capsys, "backups")
    assert code == 5
    assert json.loads(err)["error"]["code"] == "rate_limited"
    assert len(fake.requests) == module.MAX_RETRIES
    assert fake.sleeps == [1.0] * (module.MAX_RETRIES - 1)


# --- the forwarded surface is untouched -------------------------------------

def test_forwarded_write_on_read_only_connection_exits_4_without_network(project, capsys):
    with FakeRailway() as fake, patch.object(module.subprocess, "run", _no_subprocess):
        code, _, err = run(capsys, "redeploy", "-s", "web")
    assert code == 4
    assert json.loads(err)["error"]["code"] == "read_only"
    assert fake.requests == []


def test_forwarded_read_still_forwards_with_the_token_in_env(project, capsys):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(argv, 0)

    with FakeRailway() as fake, \
            patch.object(module.subprocess, "run", _railway_only(fake_run)), \
            patch.object(module.shutil, "which", return_value="/usr/bin/railway"):
        code, _, _ = run(capsys, "volume", "list")
    assert code == 0
    assert seen["argv"] == ["railway", "volume", "list"]
    assert seen["env"]["RAILWAY_TOKEN"] == TOKEN
    assert fake.requests == []


# --- doctor reports both paths ----------------------------------------------

STATUS_JSON = json.dumps({"id": PROJECT_ID, "name": "example",
                          "workspace": {"name": "example-workspace"},
                          "environments": {"edges": [{"node": {"name": "production"}}]},
                          "services": {"edges": [{"node": {"name": "web"}}]}})


def _fake_status(returncode=0, stdout=STATUS_JSON, stderr=""):
    def fake_run(argv, **kwargs):
        assert argv == ["railway", "status", "--json"]
        assert kwargs["env"]["RAILWAY_TOKEN"] == TOKEN
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
    return _railway_only(fake_run)


def test_doctor_reports_cli_and_api_probes_separately(project, capsys):
    with FakeRailway() as fake, patch.object(module.subprocess, "run", _fake_status()):
        code, out, err = run(capsys, "doctor")
    assert code == 0, err
    report = json.loads(out)
    assert report["ok"] is True
    conn = report["connections"]["prod"]
    assert conn["project"] == {"id": PROJECT_ID, "name": "example"}
    assert conn["api"] == {"ok": True, "project_id": PROJECT_ID,
                           "environment_id": ENV_ID}
    assert len(fake.requests) == 1
    assert "projectToken" in fake.requests[0]["query"]
    assert fake.requests[0]["headers"]["project-access-token"] == TOKEN
    assert "authorization" not in fake.requests[0]["headers"]


def test_doctor_names_the_api_path_when_only_it_fails(project, capsys):
    with FakeRailway([httpx.Response(200, json=NOT_AUTHORIZED)]), \
            patch.object(module.subprocess, "run", _fake_status()):
        code, out, _ = run(capsys, "doctor")
    assert code == 2
    conn = json.loads(out)["connections"]["prod"]
    assert conn["ok"] is False
    assert "error" not in conn                      # the CLI path was healthy
    assert conn["project"]["id"] == PROJECT_ID
    assert conn["api"]["ok"] is False
    assert "Not Authorized" in conn["api"]["error"]


def test_doctor_names_the_cli_path_when_only_it_fails(project, capsys):
    with FakeRailway(), patch.object(module.subprocess, "run", _fake_status(
            returncode=1, stdout="", stderr="Unauthorized. Please provide a valid token")):
        code, out, _ = run(capsys, "doctor")
    assert code == 2
    conn = json.loads(out)["connections"]["prod"]
    assert conn["ok"] is False
    assert "Unauthorized" in conn["error"]           # the CLI path
    assert conn["api"] == {"ok": True, "project_id": PROJECT_ID,
                           "environment_id": ENV_ID}


def test_doctor_without_railway_on_path_still_proves_the_api(project, capsys):
    def missing(argv, **kwargs):
        raise FileNotFoundError("railway")

    with FakeRailway(), patch.object(module.subprocess, "run", _railway_only(missing)):
        code, out, _ = run(capsys, "doctor")
    assert code == 5
    conn = json.loads(out)["connections"]["prod"]
    assert "could not run railway" in conn["error"]
    assert conn["api"]["ok"] is True


# --- help is truthful -------------------------------------------------------

def test_help_discloses_backups_and_its_exit_codes():
    text = module.__doc__ or ""
    assert "railwayc backups [--connection <id>]" in text
    assert "Project-Access-Token" in text
    assert "allow_write: false" in text
    for fragment in ("not a forward", "Not Authorized", "never as not-found"):
        assert fragment in text, fragment
    assert "`me`" in text
