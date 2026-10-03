#!/usr/bin/env python3
"""Tests for secrets in and out: `database create` and `env set` take secrets
from stdin, a file or an env key; every answer redacts passwords unless
--reveal prints them to a terminal; `projects create <name>`.

Run with: uv run --with httpx --with pytest pytest capabilities/coolify/tests/test_secret_hygiene.py
"""

import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_capability = Path(__file__).resolve().parents[1]
_coolify_path = next((path for path in (
    _capability / "bin" / "coolify", _capability / "coolify")
    if path.is_file()), _capability / "bin" / "coolify")
coolify_module = types.ModuleType("coolify_secret_hygiene")
coolify_module.__file__ = str(_coolify_path)
exec(_coolify_path.read_text(), coolify_module.__dict__)
sys.modules["coolify_secret_hygiene"] = coolify_module

FIXTURES = _capability / "tests" / "fixtures"
PASSWORD = "Fx7pQ2LmW9zR"
CONN = {"id": "exp", "allow_write": True,
        "base_url": "https://coolify.example.com", "token": "1|test-token"}


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def _run(argv, response=None, stdin="", tty=False, conn=CONN):
    """Run the CLI in-process against a recorded response; return
    (exit code, stdout, stderr, requests)."""
    calls = []

    def fake_request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path, "params": params,
                      "body": json_body})
        value = response(method, path) if callable(response) else response
        return {} if value is None else value

    out, err = io.StringIO(), io.StringIO()
    out.isatty = lambda: tty
    code = 0
    with (
        patch.object(sys, "argv", ["coolify", *argv]),
        patch.object(sys, "stdin", io.StringIO(stdin)),
        patch.object(sys, "stdout", out),
        patch.object(sys, "stderr", err),
        patch.object(coolify_module, "_gate", lambda: None),
        patch.object(coolify_module, "_resolve_conn", return_value=conn),
        patch.object(coolify_module, "_request", side_effect=fake_request),
    ):
        try:
            coolify_module.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue(), calls


DB_CREATE = ["database", "create", "--engine", "postgresql", "--project", "proj-uuid",
             "--server", "srv-uuid", "--environment", "production"]


# ── database create: the password off the command line ─────────────────────

def test_database_password_from_stdin():
    code, out, err, calls = _run(
        [*DB_CREATE, "--set-stdin", "postgres_password"],
        _fixture("database_create_postgresql.json"), stdin=PASSWORD + "\n")

    assert code == 0, err
    assert calls[0]["body"]["postgres_password"] == PASSWORD
    assert err == ""
    assert PASSWORD not in out


def test_database_password_from_a_file(tmp_path):
    secret = tmp_path / "pg-password"
    secret.write_text(PASSWORD + "\n")
    code, out, err, calls = _run(
        [*DB_CREATE, "--set-file", f"postgres_password={secret}"],
        _fixture("database_create_postgresql.json"))

    assert code == 0, err
    assert calls[0]["body"]["postgres_password"] == PASSWORD
    assert err == ""


def test_database_password_from_an_env_key(monkeypatch, tmp_path):
    monkeypatch.setattr(coolify_module, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(coolify_module, "CREDENTIALS_ENV", tmp_path / "none.env")
    monkeypatch.setenv("PG_PASSWORD_FOR_TEST", PASSWORD)
    code, out, err, calls = _run(
        [*DB_CREATE, "--set-env", "postgres_password=PG_PASSWORD_FOR_TEST"],
        _fixture("database_create_postgresql.json"))

    assert code == 0, err
    assert calls[0]["body"]["postgres_password"] == PASSWORD


def test_the_argv_form_still_works_with_a_warning():
    code, out, err, calls = _run(
        [*DB_CREATE, "--set", f"postgres_password={PASSWORD}"],
        _fixture("database_create_postgresql.json"))

    assert code == 0, err
    assert calls[0]["body"]["postgres_password"] == PASSWORD
    warning = json.loads(err)["warning"]
    assert "deprecated" in warning and "--set-stdin postgres_password" in warning
    assert PASSWORD not in err


def test_a_non_secret_set_carries_no_warning():
    code, _, err, calls = _run([*DB_CREATE, "--set", "postgres_db=app"], {"uuid": "u"})

    assert code == 0 and err == ""
    assert calls[0]["body"]["postgres_db"] == "app"


@pytest.mark.parametrize("extra", [
    ["--set-stdin", "postgres_password", "--set-stdin", "postgres_user"],
    ["--set", "postgres_password=x", "--set-stdin", "postgres_password"],
    ["--set-file", "postgres_password"],
    ["--set-env", "postgres_password="],
])
def test_ambiguous_or_malformed_secret_sources_are_refused(extra):
    code, _, err, calls = _run([*DB_CREATE, *extra], {"uuid": "u"}, stdin="pw")

    assert code == 6
    assert calls == []


def test_database_create_redacts_the_connection_urls():
    code, out, err, _ = _run([*DB_CREATE, "--set-stdin", "postgres_password"],
                             _fixture("database_create_postgresql.json"),
                             stdin=PASSWORD)

    assert code == 0, err
    answer = json.loads(out)
    assert answer["uuid"] == "<db-uuid>"
    assert answer["internal_db_url"] == \
        "postgres://app:<redacted>@<db-uuid>:5432/postgres"
    assert answer["external_db_url"] == \
        "postgres://app:<redacted>@<server-ip>:54321/postgres"
    assert PASSWORD not in out


def test_reveal_refuses_when_stdout_is_not_a_terminal():
    code, out, err, calls = _run([*DB_CREATE, "--set-stdin", "postgres_password",
                                  "--reveal"],
                                 _fixture("database_create_postgresql.json"),
                                 stdin=PASSWORD, tty=False)

    assert code == 4
    assert json.loads(err)["error"]["code"] == "reveal_needs_terminal"
    assert calls == [] and out == ""


def test_reveal_prints_the_full_url_to_a_terminal():
    code, out, err, _ = _run([*DB_CREATE, "--set-stdin", "postgres_password",
                              "--reveal"],
                             _fixture("database_create_postgresql.json"),
                             stdin=PASSWORD, tty=True)

    assert code == 0, err
    assert json.loads(out)["internal_db_url"] == \
        f"postgres://app:{PASSWORD}@<db-uuid>:5432/postgres"


# ── every read redacts ──────────────────────────────────────────────────────

def test_a_database_read_redacts_urls_and_password_fields():
    code, out, err, _ = _run(["databases", "<db-uuid>"],
                             _fixture("database_get_postgresql.json"))

    assert code == 0, err
    answer = json.loads(out)
    assert answer["postgres_password"] == "<redacted>"
    assert answer["internal_db_url"].startswith("postgres://app:<redacted>@")
    assert answer["external_db_url"].startswith("postgres://app:<redacted>@")
    assert answer["postgres_user"] == "app"
    assert answer["status"] == "running:healthy"
    assert PASSWORD not in out


def test_a_database_read_reveals_to_a_terminal():
    code, out, _, _ = _run(["databases", "<db-uuid>", "--reveal"],
                           _fixture("database_get_postgresql.json"), tty=True)

    assert code == 0
    assert json.loads(out)["postgres_password"] == PASSWORD


@pytest.mark.parametrize("argv,response", [
    (["applications", "app-uuid"],
     {"uuid": "app-uuid", "docker_compose_raw": f"DATABASE_URL=postgres://u:{PASSWORD}@db:5432/x"}),
    (["services", "svc-uuid"],
     {"uuid": "svc-uuid", "connection": f"redis://default:{PASSWORD}@cache:6379/0"}),
    (["logs", "app-uuid"],
     {"logs": f"connecting to postgresql://app:{PASSWORD}@db:5432/app ok"}),
    (["env", "list", "app-uuid"],
     [{"key": "DATABASE_URL", "value": f"postgres://u:{PASSWORD}@db/x",
       "real_value": f"postgres://u:{PASSWORD}@db/x"}]),
    (["projects", "proj-uuid"],
     {"uuid": "proj-uuid", "note": f"mysql://root:{PASSWORD}@h:3306/d"}),
    (["resources"], [{"uuid": "r", "name": f"postgres://a:{PASSWORD}@h/d"}]),
    (["servers", "srv-uuid"], {"uuid": "srv-uuid", "url": f"https://u:{PASSWORD}@h"}),
    (["deployments", "dep-uuid"], {"uuid": "dep-uuid", "logs": f"x://u:{PASSWORD}@h"}),
])
def test_every_read_redacts_passwords_in_urls(argv, response):
    code, out, err, _ = _run(argv, response)

    assert code == 0, err
    assert PASSWORD not in out
    assert ":<redacted>@" in out


@pytest.mark.parametrize("argv", [
    ["projects", "--reveal"], ["applications", "--reveal"], ["logs", "a", "--reveal"],
    ["env", "list", "a", "--reveal"], ["resources", "--reveal"],
])
def test_every_read_refuses_reveal_without_a_terminal(argv):
    code, _, err, calls = _run(argv, [])

    assert code == 4 and calls == []
    assert json.loads(err)["error"]["code"] == "reveal_needs_terminal"


def test_redaction_leaves_urls_without_a_password_alone():
    value = {"fqdn": "https://app.example.com:8443/x", "git": "git@host:/srv/git/a.git",
             "ssh": "ssh://git@host:22/repo", "empty": "postgres://u:@h/d",
             "password_reset_url": None, "count": 3}
    assert coolify_module._redact(value) == value


# ── env set: the value off the command line ────────────────────────────────

ENV_SET = ["env", "set", "app-uuid", "DB_PASSWORD"]


def _env_response(method, path):
    return [] if method == "GET" else {"uuid": "env-uuid"}


def test_env_set_value_from_stdin_by_flag_and_by_default():
    for extra in (["--value-stdin"], []):
        code, out, err, calls = _run([*ENV_SET, *extra], _env_response,
                                     stdin=PASSWORD + "\n")
        assert code == 0, err
        assert calls[-1] == {"method": "POST", "path": "/applications/app-uuid/envs",
                             "params": None,
                             "body": {"key": "DB_PASSWORD", "value": PASSWORD}}
        assert err == ""


def test_env_set_value_from_a_file(tmp_path):
    secret = tmp_path / "value"
    secret.write_text(PASSWORD)
    code, _, err, calls = _run([*ENV_SET, "--value-file", str(secret)], _env_response)

    assert code == 0, err
    assert calls[-1]["body"]["value"] == PASSWORD


def test_env_set_value_from_an_env_key(monkeypatch, tmp_path):
    monkeypatch.setattr(coolify_module, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(coolify_module, "CREDENTIALS_ENV", tmp_path / "none.env")
    monkeypatch.setenv("APP_DB_PASSWORD_FOR_TEST", PASSWORD)
    code, _, err, calls = _run([*ENV_SET, "--value-env", "APP_DB_PASSWORD_FOR_TEST"],
                               _env_response)

    assert code == 0, err
    assert calls[-1]["body"]["value"] == PASSWORD


def test_env_set_positional_value_still_works_with_a_warning():
    code, _, err, calls = _run([*ENV_SET, PASSWORD], _env_response)

    assert code == 0
    assert calls[-1]["body"]["value"] == PASSWORD
    warning = json.loads(err)["warning"]
    assert "deprecated" in warning and "--value-stdin" in warning
    assert PASSWORD not in err


def test_env_set_refuses_two_values():
    code, _, _, calls = _run([*ENV_SET, PASSWORD, "--value-stdin"], _env_response,
                             stdin="other")

    assert code == 6 and calls == []


def test_env_set_on_a_read_only_connection_is_refused_before_any_request():
    code, _, err, calls = _run([*ENV_SET, "--value-stdin"], _env_response,
                               stdin=PASSWORD, conn=dict(CONN, allow_write=False))

    assert code == 4 and calls == []


# ── projects create <name> ──────────────────────────────────────────────────

def test_projects_create_takes_the_name_as_its_argument():
    code, out, err, calls = _run(["projects", "create", "c1-throwaway"],
                                 _fixture("project_create.json"))

    assert code == 0, err
    assert calls == [{"method": "POST", "path": "/projects", "params": None,
                      "body": {"name": "c1-throwaway"}}]
    assert err == ""
    assert json.loads(out) == _fixture("project_create.json")


def test_projects_create_with_a_description():
    code, _, err, calls = _run(["projects", "create", "p", "--description", "d"], {})

    assert code == 0, err
    assert calls[0]["body"] == {"name": "p", "description": "d"}


@pytest.mark.parametrize("argv", [
    ["projects", "create", "--name", "p"],
    ["projects", "", "create", "--name", "p"],
    ["projects", "create", "p", "--name", "p"],
])
def test_the_old_forms_keep_working_with_a_warning(argv):
    code, _, err, calls = _run(argv, {})

    assert code == 0
    assert calls[0]["body"] == {"name": "p"}
    assert "projects create <name>" in json.loads(err)["warning"]


@pytest.mark.parametrize("argv", [
    ["projects", "create"],
    ["projects", "create", "a", "--name", "b"],
    ["projects", "create", "a", "b"],
    ["projects", "a", "b"],
])
def test_projects_create_without_one_clear_name_is_refused(argv):
    code, _, _, calls = _run(argv, {})

    assert code == 6 and calls == []


def test_projects_list_and_get_are_unchanged():
    _, out, _, calls = _run(["projects"], [{"uuid": "p1", "name": "one", "id": 1}])
    assert calls[0]["path"] == "/projects" and calls[0]["method"] == "GET"
    assert json.loads(out) == [{"uuid": "p1", "name": "one"}]
    _, out, _, calls = _run(["projects", "p1"], {"uuid": "p1", "environments": []})
    assert calls[0]["path"] == "/projects/p1"


def test_projects_create_obeys_a_read_only_connection():
    code, _, err, calls = _run(["projects", "create", "p"], {},
                               conn=dict(CONN, allow_write=False))

    assert code == 4 and calls == []
    assert json.loads(err)["error"]["code"] == "read_only"


def test_help_documents_the_new_surface():
    help_text = coolify_module.__doc__ or ""
    for phrase in ("connect <name> --url <url>", "--token-stdin", "projects create <name>",
                   "--set-stdin KEY", "--set-file KEY=PATH", "--set-env KEY=ENV_KEY",
                   "--value-stdin", "SECRETS", "--reveal"):
        assert phrase in help_text
