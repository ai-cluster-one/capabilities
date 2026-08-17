#!/usr/bin/env python3
"""Tests for headless application update, source discovery, and diagnostics."""

import sys
import types
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

_coolify_path = Path(__file__).parent.parent / "bin" / "coolify"
_code = _coolify_path.read_text()
coolify_module = types.ModuleType("coolify_management")
coolify_module.__file__ = str(_coolify_path)
exec(_code, coolify_module.__dict__)
sys.modules["coolify_management"] = coolify_module


def test_app_update_sends_only_explicit_fields():
    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({
            "method": method,
            "path": path,
            "body": json_body,
        })
        return {"uuid": "app-uuid"}

    args = SimpleNamespace(
        uuid="app-uuid",
        domains="",
        ports_exposes="8000",
        health_check_enabled=False,
        health_check_path="/ready",
        health_check_port="8000",
    )
    with patch.object(coolify_module, "_request", side_effect=mock_request):
        result = coolify_module.cmd_app_update(None, args)

    assert result == {"uuid": "app-uuid"}
    assert calls == [{
        "method": "PATCH",
        "path": "/applications/app-uuid",
        "body": {
            "domains": "",
            "ports_exposes": "8000",
            "health_check_enabled": False,
            "health_check_path": "/ready",
            "health_check_port": "8000",
        },
    }]


def test_app_update_maps_repository_and_full_healthcheck_surface():
    args = SimpleNamespace(
        uuid="app-uuid",
        name="api",
        git_repository="owner/repo",
        git_branch="release",
        build_pack="dockerfile",
        base_directory="/mcp-server",
        dockerfile_location="/Dockerfile",
        docker_compose_location="/compose.yaml",
        health_check_enabled=True,
        health_check_path="/health",
        health_check_port="8000",
        health_check_host="localhost",
        health_check_scheme="http",
        health_check_return_code=200,
        health_check_interval=30,
        health_check_timeout=5,
        health_check_retries=3,
        health_check_start_period=10,
    )

    with patch.object(coolify_module, "_request", return_value={"uuid": "app-uuid"}) as request:
        coolify_module.cmd_app_update(None, args)

    body = request.call_args.kwargs["json_body"]
    assert body["name"] == "api"
    assert body["git_repository"] == "owner/repo"
    assert body["git_branch"] == "release"
    assert body["build_pack"] == "dockerfile"
    assert body["base_directory"] == "/mcp-server"
    assert body["dockerfile_location"] == "/Dockerfile"
    assert body["docker_compose_location"] == "/compose.yaml"
    assert body["health_check_enabled"] is True
    assert body["health_check_return_code"] == 200
    assert body["health_check_start_period"] == 10


def test_app_update_refuses_empty_patch():
    with pytest.raises(SystemExit) as exc:
        coolify_module.cmd_app_update(None, SimpleNamespace(uuid="app-uuid"))
    assert exc.value.code == 6


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("--auto-deploy-enabled", True),
        ("--no-auto-deploy-enabled", False),
    ],
)
def test_app_update_patches_only_explicit_auto_deploy_value(flag, expected):
    argv = ["coolify", "app", "update", "app-uuid", flag]
    connection = {
        "id": "production",
        "allow_write": True,
        "base_url": "https://coolify.example",
        "token": "test-token",
    }
    with (
        patch.object(sys, "argv", argv),
        patch.object(coolify_module, "_gate"),
        patch.object(coolify_module, "_resolve_conn", return_value=connection),
        patch.object(coolify_module, "_client", return_value=nullcontext(None)),
        patch.object(coolify_module, "_request", return_value={"uuid": "app-uuid"}) as request,
    ):
        coolify_module.main()

    request.assert_called_once_with(
        None,
        "PATCH",
        "/applications/app-uuid",
        json_body={"is_auto_deploy_enabled": expected},
    )


def test_app_update_rejects_conflicting_auto_deploy_flags():
    argv = [
        "coolify", "app", "update", "app-uuid",
        "--auto-deploy-enabled", "--no-auto-deploy-enabled",
    ]
    with (
        patch.object(sys, "argv", argv),
        patch.object(coolify_module, "_gate"),
        pytest.raises(SystemExit) as exc,
    ):
        coolify_module.main()

    assert exc.value.code == 2


def test_sources_lists_uuids_without_key_material():
    def mock_request(c, method, path, params=None, json_body=None):
        if path == "/github-apps":
            return [{
                "id": 1,
                "uuid": "github-uuid",
                "name": "main github",
                "organization": "example",
                "client_secret": "must-not-leak",
            }]
        if path == "/security/keys":
            return {"data": [{
                "id": 2,
                "uuid": "key-uuid",
                "name": "deploy key",
                "is_git_related": True,
                "private_key": "must-not-leak",
                "public_key": "also-not-needed",
            }]}
        raise AssertionError(path)

    with patch.object(coolify_module, "_request", side_effect=mock_request):
        result = coolify_module.cmd_sources(None)

    assert result["github_apps"] == [{
        "uuid": "github-uuid",
        "id": 1,
        "name": "main github",
        "organization": "example",
    }]
    assert result["private_deploy_keys"] == [{
        "uuid": "key-uuid",
        "id": 2,
        "name": "deploy key",
        "is_git_related": True,
    }]
    assert "private_key" not in str(result)
    assert "must-not-leak" not in str(result)


def test_doctor_reports_effective_abilities_with_non_mutating_gets():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/security/keys"):
            return httpx.Response(
                200,
                json=[{"uuid": "key-uuid", "private_key": "redacted-from-test-output"}],
            )
        if request.url.path.endswith("/enable"):
            return httpx.Response(405, json={"message": "POST request is required."})
        if request.url.path.endswith("/deploy"):
            return httpx.Response(
                403, json={"message": "Missing required permissions: deploy"},
            )
        raise AssertionError(request.url.path)

    with httpx.Client(
        base_url="https://coolify.example/api/v1",
        transport=httpx.MockTransport(handler),
    ) as client:
        abilities = coolify_module._detect_effective_abilities(client)

    assert abilities["read"] is True
    assert abilities["read:sensitive"] is True
    assert abilities["write"] is True
    assert abilities["deploy"] is False
    assert abilities["root"] is None
    assert all(method == "GET" for method, _ in seen)


def test_app_writes_obey_read_only_policy():
    argv = [
        "coolify", "app", "update", "app-uuid", "--ports-exposes", "8000",
    ]
    connection = {
        "id": "production",
        "allow_write": False,
        "base_url": "https://coolify.example",
        "token": "test-token",
    }
    with (
        patch.object(sys, "argv", argv),
        patch.object(coolify_module, "_resolve_conn", return_value=connection),
        pytest.raises(SystemExit) as exc,
    ):
        coolify_module.main()
    assert exc.value.code == 4


def test_app_update_sets_watch_paths():
    """watch_paths is accepted on app update and passed to the API."""
    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({
            "method": method,
            "path": path,
            "body": json_body,
        })
        return {"uuid": "app-uuid"}

    args = SimpleNamespace(
        uuid="app-uuid",
        watch_paths="backend/**\nmcp-server/**",
    )
    with patch.object(coolify_module, "_request", side_effect=mock_request):
        result = coolify_module.cmd_app_update(None, args)

    assert result == {"uuid": "app-uuid"}
    assert calls == [{
        "method": "PATCH",
        "path": "/applications/app-uuid",
        "body": {
            "watch_paths": "backend/**\nmcp-server/**",
        },
    }]


def test_app_update_clears_watch_paths_with_empty_string():
    """Empty string clears watch_paths (deploy on all changes)."""
    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({"body": json_body})
        return {"uuid": "app-uuid"}

    args = SimpleNamespace(uuid="app-uuid", watch_paths="")
    with patch.object(coolify_module, "_request", side_effect=mock_request):
        coolify_module.cmd_app_update(None, args)

    assert calls[0]["body"] == {"watch_paths": ""}


def test_help_and_manifest_declare_new_surface():
    help_text = coolify_module.__doc__ or ""
    assert "sources" in help_text
    assert "app update <uuid>" in help_text
    assert "--base-directory" in help_text
    assert "--health-check-start-period" in help_text
    assert "--watch-paths" in help_text
    assert "--auto-deploy-enabled / --no-auto-deploy-enabled" in help_text
    assert [entry["topic"] for entry in coolify_module._guide_menu()] == [
        "headless-apps"
    ]
