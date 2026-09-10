#!/usr/bin/env python3
"""Tests for `database create` — one endpoint per engine, and its pass-through.

Run with: uv run --with httpx --with pytest pytest capabilities/coolify/tests/test_database_create.py
(the coolify bin declares httpx in its PEP-723 header, so bare python3 cannot import it)
"""

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_capability = Path(__file__).resolve().parents[1]
_coolify_path = next((path for path in (
    _capability / "bin" / "coolify", _capability / "coolify")
    if path.is_file()), _capability / "bin" / "coolify")
_code = _coolify_path.read_text()
coolify_module = types.ModuleType("coolify_database")
coolify_module.__file__ = str(_coolify_path)
exec(_code, coolify_module.__dict__)
sys.modules["coolify_database"] = coolify_module


def _record_calls(response=None):
    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path,
                      "params": params, "body": json_body})
        return response if response is not None else {"uuid": "db-uuid"}

    return calls, mock_request


def _args(**overrides):
    """The argparse namespace `database create` produces, before overrides."""
    base = dict(
        engine="postgresql",
        project="proj-uuid",
        server="srv-uuid",
        environment="production",
        environment_uuid=None,
        name=None,
        description=None,
        image=None,
        instant_deploy=False,
        destination=None,
        set_string=None,
        set_json=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("engine", [
    "postgresql", "mysql", "mariadb", "mongodb",
    "redis", "keydb", "dragonfly", "clickhouse",
])
def test_create_posts_to_the_engines_own_endpoint(engine):
    """Coolify exposes one create path per engine; every one is reachable."""
    calls, mock_request = _record_calls()

    with patch.object(coolify_module, "_request", side_effect=mock_request):
        result = coolify_module.cmd_database_create(None, _args(engine=engine))

    assert result == {"uuid": "db-uuid"}
    assert calls == [{
        "method": "POST",
        "path": f"/databases/{engine}",
        "params": None,
        "body": {
            "project_uuid": "proj-uuid",
            "server_uuid": "srv-uuid",
            "environment_name": "production",
        },
    }]


def test_declared_engines_match_the_dispatched_paths():
    """The argument surface and the endpoint set are one declaration."""
    assert coolify_module.DATABASE_ENGINES == (
        "postgresql", "mysql", "mariadb", "mongodb",
        "redis", "keydb", "dragonfly", "clickhouse",
    )


def test_named_optional_fields_map_onto_the_body():
    calls, mock_request = _record_calls()
    args = _args(
        name="orders",
        description="order store",
        image="postgres:16",
        destination="dest-uuid",
        instant_deploy=True,
    )

    with patch.object(coolify_module, "_request", side_effect=mock_request):
        coolify_module.cmd_database_create(None, args)

    assert calls[0]["body"] == {
        "project_uuid": "proj-uuid",
        "server_uuid": "srv-uuid",
        "environment_name": "production",
        "name": "orders",
        "description": "order store",
        "image": "postgres:16",
        "destination_uuid": "dest-uuid",
        "instant_deploy": True,
    }


def test_environment_uuid_is_accepted_instead_of_the_name():
    calls, mock_request = _record_calls()
    args = _args(environment=None, environment_uuid="env-uuid")

    with patch.object(coolify_module, "_request", side_effect=mock_request):
        coolify_module.cmd_database_create(None, args)

    body = calls[0]["body"]
    assert body["environment_uuid"] == "env-uuid"
    assert "environment_name" not in body


def test_both_environment_forms_are_sent_when_both_are_given():
    calls, mock_request = _record_calls()
    args = _args(environment="production", environment_uuid="env-uuid")

    with patch.object(coolify_module, "_request", side_effect=mock_request):
        coolify_module.cmd_database_create(None, args)

    body = calls[0]["body"]
    assert body["environment_name"] == "production"
    assert body["environment_uuid"] == "env-uuid"


def test_missing_environment_is_refused_before_the_request():
    """Coolify needs one of the two; refuse locally rather than spend a 422."""
    calls, mock_request = _record_calls()
    args = _args(environment=None, environment_uuid=None)

    with (
        patch.object(coolify_module, "_request", side_effect=mock_request),
        pytest.raises(SystemExit) as exc,
    ):
        coolify_module.cmd_database_create(None, args)

    assert exc.value.code == 6
    assert calls == []


@pytest.mark.parametrize("engine,field,value", [
    ("postgresql", "postgres_password", "s3cret"),
    ("mysql", "mysql_root_password", "s3cret"),
    ("mariadb", "mariadb_database", "orders"),
    ("mongodb", "mongo_initdb_root_username", "root"),
    ("redis", "redis_password", "s3cret"),
    ("keydb", "keydb_password", "s3cret"),
    ("dragonfly", "dragonfly_password", "s3cret"),
    ("clickhouse", "clickhouse_admin_user", "admin"),
])
def test_engine_fields_pass_through_as_strings(engine, field, value):
    """The engine's own surface is the caller's to name; nothing is modelled."""
    calls, mock_request = _record_calls()
    args = _args(engine=engine, set_string=[f"{field}={value}"])

    with patch.object(coolify_module, "_request", side_effect=mock_request):
        coolify_module.cmd_database_create(None, args)

    assert calls[0]["path"] == f"/databases/{engine}"
    assert calls[0]["body"][field] == value


def test_set_keeps_a_value_that_looks_like_json_as_a_string():
    calls, mock_request = _record_calls()
    args = _args(set_string=["postgres_password=12345", "postgres_db=true"])

    with patch.object(coolify_module, "_request", side_effect=mock_request):
        coolify_module.cmd_database_create(None, args)

    assert calls[0]["body"]["postgres_password"] == "12345"
    assert calls[0]["body"]["postgres_db"] == "true"


def test_set_json_types_booleans_numbers_and_arrays():
    calls, mock_request = _record_calls()
    args = _args(set_json=[
        "is_public=true",
        "public_port=5432",
        'tags=["staging","orders"]',
    ])

    with patch.object(coolify_module, "_request", side_effect=mock_request):
        coolify_module.cmd_database_create(None, args)

    body = calls[0]["body"]
    assert body["is_public"] is True
    assert body["public_port"] == 5432
    assert body["tags"] == ["staging", "orders"]


def test_a_value_containing_equals_survives_intact():
    calls, mock_request = _record_calls()
    args = _args(set_string=["postgres_initdb_args=--data-checksums=on"])

    with patch.object(coolify_module, "_request", side_effect=mock_request):
        coolify_module.cmd_database_create(None, args)

    assert calls[0]["body"]["postgres_initdb_args"] == "--data-checksums=on"


def test_pass_through_is_applied_after_the_named_flags():
    calls, mock_request = _record_calls()
    args = _args(name="named-flag", set_string=["name=pass-through"])

    with patch.object(coolify_module, "_request", side_effect=mock_request):
        coolify_module.cmd_database_create(None, args)

    assert calls[0]["body"]["name"] == "pass-through"


@pytest.mark.parametrize("bad", ["postgres_password", "=value", ""])
def test_malformed_set_is_refused_as_input(bad):
    calls, mock_request = _record_calls()

    with (
        patch.object(coolify_module, "_request", side_effect=mock_request),
        pytest.raises(SystemExit) as exc,
    ):
        coolify_module.cmd_database_create(None, _args(set_string=[bad]))

    assert exc.value.code == 6
    assert calls == []


def test_set_json_refuses_a_value_that_is_not_json():
    calls, mock_request = _record_calls()

    with (
        patch.object(coolify_module, "_request", side_effect=mock_request),
        pytest.raises(SystemExit) as exc,
    ):
        coolify_module.cmd_database_create(None, _args(set_json=["is_public=yes"]))

    assert exc.value.code == 6
    assert calls == []


def test_create_is_declared_a_write_verb():
    assert "database" in coolify_module.WRITE_VERBS
    assert "databases" not in coolify_module.WRITE_VERBS


_READ_ONLY_CONNECTION = {
    "id": "production",
    "allow_write": False,
    "base_url": "https://coolify.example",
    "token": "test-token",
}


def test_create_obeys_a_read_only_connection(capsys):
    """Creating is a write: a read-only connection refuses it with exit 4.

    The project/global policy gate also exits 4, so this pins the refusal to the
    connection's write gate by neutralising the policy gate and reading the code.
    """
    argv = [
        "coolify", "database", "create", "--engine", "postgresql",
        "--project", "proj-uuid", "--server", "srv-uuid",
        "--environment", "production",
    ]
    calls, mock_request = _record_calls()

    with (
        patch.object(sys, "argv", argv),
        patch.object(coolify_module, "_gate", lambda: None),
        patch.object(coolify_module, "_resolve_conn",
                     return_value=_READ_ONLY_CONNECTION),
        patch.object(coolify_module, "_request", side_effect=mock_request),
        pytest.raises(SystemExit) as exc,
    ):
        coolify_module.main()

    assert exc.value.code == 4
    assert '"code": "read_only"' in capsys.readouterr().err
    assert calls == []


def test_create_runs_on_a_writable_connection():
    """The same command reaches the engine endpoint once writes are allowed."""
    argv = [
        "coolify", "database", "create", "--engine", "redis",
        "--project", "proj-uuid", "--server", "srv-uuid",
        "--environment", "production", "--set", "redis_password=s3cret",
    ]
    calls, mock_request = _record_calls()

    with (
        patch.object(sys, "argv", argv),
        patch.object(coolify_module, "_gate", lambda: None),
        patch.object(coolify_module, "_resolve_conn",
                     return_value=dict(_READ_ONLY_CONNECTION, allow_write=True)),
        patch.object(coolify_module, "_request", side_effect=mock_request),
    ):
        coolify_module.main()

    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/databases/redis"
    assert calls[0]["body"]["redis_password"] == "s3cret"


def test_reading_databases_stays_open_on_a_read_only_connection():
    """The plural read verb shares the family but must not be gated."""
    argv = ["coolify", "databases"]
    calls, mock_request = _record_calls(response=[])

    with (
        patch.object(sys, "argv", argv),
        patch.object(coolify_module, "_gate", lambda: None),
        patch.object(coolify_module, "_resolve_conn",
                     return_value=_READ_ONLY_CONNECTION),
        patch.object(coolify_module, "_request", side_effect=mock_request),
    ):
        coolify_module.main()

    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == "/databases"


def test_help_documents_the_verb_its_engines_and_the_pass_through():
    help_text = coolify_module.__doc__ or ""
    assert "database create --engine <engine>" in help_text
    for engine in coolify_module.DATABASE_ENGINES:
        assert engine in help_text
    assert "--set KEY=VALUE" in help_text
    assert "--set-json KEY=JSON" in help_text
    assert "validates neither against the engine" in help_text
    assert "--environment-uuid <uuid>" in help_text
