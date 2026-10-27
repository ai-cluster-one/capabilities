#!/usr/bin/env python3
"""Tests for coolify create commands (projects, app, service).

Run with: uv run --with httpx python3 capabilities/coolify/tests/test_create_commands.py
(the coolify bin declares httpx in its PEP-723 header, so bare python3 cannot import it)
"""

import sys
from pathlib import Path
from unittest.mock import patch
import types

# Load the coolify script as a module by reading and exec'ing it
_capability = Path(__file__).resolve().parents[1]
_coolify_path = next((path for path in (
    _capability / "bin" / "coolify", _capability / "coolify")
    if path.is_file()), _capability / "bin" / "coolify")
_code = _coolify_path.read_text()
coolify_module = types.ModuleType("coolify")
exec(_code, coolify_module.__dict__)
sys.modules["coolify"] = coolify_module


def test_projects_create():
    """projects create sends correct payload."""
    response = {
        "uuid": "project-uuid-123",
        "name": "Test Project",
        "environments": [{"name": "production", "uuid": "env-uuid-456"}]
    }

    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path, "body": json_body})
        return response

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        result = coolify_module.cmd_projects_create(None, "Test Project", "A test project")

        assert len(calls) == 1
        assert calls[0]["method"] == "POST"
        assert calls[0]["path"] == "/projects"
        assert calls[0]["body"]["name"] == "Test Project"
        assert calls[0]["body"]["description"] == "A test project"
        assert result == response


def test_projects_environment_create():
    """projects environment create posts the name to the project's environments."""
    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path, "body": json_body})
        return {"uuid": "env-uuid-789"}

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        result = coolify_module.cmd_projects_environment_create(None, "project-uuid-123", "staging")

    assert calls == [{"method": "POST", "path": "/projects/project-uuid-123/environments",
                      "body": {"name": "staging"}}]
    assert result == {"uuid": "env-uuid-789"}
    assert coolify_module._projects_call(
        ["environment", "create", "project-uuid-123", "staging"], None) == (
        "environment", "project-uuid-123", "staging")


def test_app_create_public_git():
    """app create with public git repository."""
    response = {"uuid": "app-uuid-123", "name": "My App"}

    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path, "body": json_body})
        return response

    # Mock args - use a simple object to control hasattr behavior
    class Args:
        project = "proj-uuid"
        server = "srv-uuid"
        environment = "production"
        git_repository = "https://github.com/user/repo"
        git_branch = "main"
        name = "My App"
        build_pack = "nixpacks"
        docker_compose_location = None
        dockerfile_location = None
        base_directory = "/apps/api"
        health_check_enabled = True
        health_check_path = "/health"

    args = Args()

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        result = coolify_module.cmd_app_create(None, args)

        assert len(calls) == 1
        assert calls[0]["method"] == "POST"
        assert calls[0]["path"] == "/applications/public"
        body = calls[0]["body"]
        assert body["project_uuid"] == "proj-uuid"
        assert body["server_uuid"] == "srv-uuid"
        assert body["environment_name"] == "production"
        assert body["git_repository"] == "https://github.com/user/repo"
        assert body["git_branch"] == "main"
        assert body["build_pack"] == "nixpacks"
        assert body["base_directory"] == "/apps/api"
        assert body["health_check_enabled"] is True
        assert body["health_check_path"] == "/health"
        assert result == response


def test_app_create_registry_image():
    """app create with registry image."""
    response = {"uuid": "app-uuid-456"}

    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path, "body": json_body})
        return response

    class Args:
        project = "proj-uuid"
        server = "srv-uuid"
        environment = "production"
        image = "nginx:latest"

    args = Args()

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        result = coolify_module.cmd_app_create(None, args)

        assert len(calls) == 1
        assert calls[0]["method"] == "POST"
        assert calls[0]["path"] == "/applications/dockerimage"
        body = calls[0]["body"]
        assert body["docker_registry_image_name"] == "nginx"
        assert body["docker_registry_image_tag"] == "latest"
        assert result == response


def test_service_create():
    """service create encodes compose file in base64."""
    import base64

    response = {"uuid": "svc-uuid-789"}

    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path, "body": json_body})
        return response

    compose_content = "version: '3'\nservices:\n  web:\n    image: nginx"

    class Args:
        compose = "/tmp/compose.yml"
        project = "proj-uuid"
        server = "srv-uuid"
        environment = "production"
        name = "My Service"
        instant_deploy = True
        destination = None

    args = Args()

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        with patch.object(Path, 'read_text', return_value=compose_content):
            result = coolify_module.cmd_service_create(None, args)

            assert len(calls) == 1
            assert calls[0]["method"] == "POST"
            assert calls[0]["path"] == "/services"
            body = calls[0]["body"]
            assert body["project_uuid"] == "proj-uuid"
            assert body["server_uuid"] == "srv-uuid"
            assert body["environment_name"] == "production"
            assert body["name"] == "My Service"
            assert body["instant_deploy"] is True

            # Verify base64 encoding
            decoded = base64.b64decode(body["docker_compose_raw"]).decode()
            assert decoded == compose_content
            assert result == response


def test_env_list_typed_application():
    """env list with --type application uses correct endpoint."""
    response = [{"uuid": "env-1", "key": "KEY", "value": "val"}]

    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path})
        return response

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        result = coolify_module.cmd_env_list_typed(None, "app-uuid", "application")

        assert len(calls) == 1
        assert calls[0]["path"] == "/applications/app-uuid/envs"
        assert result[0]["value"] == "val"
        assert result[0]["value_hidden"] is False


def test_env_list_typed_service():
    """env list with --type service uses correct endpoint."""
    response = [{"uuid": "env-1", "key": "KEY", "value": "val"}]

    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path})
        return response

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        result = coolify_module.cmd_env_list_typed(None, "svc-uuid", "service")

        assert len(calls) == 1
        assert calls[0]["path"] == "/services/svc-uuid/envs"
        assert result[0]["value"] == "val"
        assert result[0]["value_hidden"] is False


def test_env_bulk():
    """env bulk parses KEY=VALUE pairs and sends PATCH."""
    response = {"updated": 2}

    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path, "body": json_body})
        return response

    env_data = "KEY1=value1\nKEY2=value2\n# comment\nKEY3=value3"

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        result = coolify_module.cmd_env_bulk(None, "app-uuid", env_data, "application")

        assert len(calls) == 1
        assert calls[0]["method"] == "PATCH"
        assert calls[0]["path"] == "/applications/app-uuid/envs/bulk"
        body = calls[0]["body"]
        assert list(body) == ["data"]
        assert len(body["data"]) == 3
        assert body["data"][0] == {"key": "KEY1", "value": "value1"}
        assert body["data"][1] == {"key": "KEY2", "value": "value2"}
        assert body["data"][2] == {"key": "KEY3", "value": "value3"}
        assert result == response


def test_app_create_sets_memory_limit_and_no_generated_domain():
    """app create sends limits_memory and autogenerate_domain when given."""
    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path, "body": json_body})
        return {"uuid": "app-uuid-123"}

    class Args:
        project = "proj-uuid"
        server = "srv-uuid"
        environment = "production"
        git_repository = "git@github.com:org/repo.git"
        git_branch = "main"
        private_deploy_key = "key-uuid"
        build_pack = "dockerfile"
        base_directory = "/"
        dockerfile_location = "/dev/node/Dockerfile"
        docker_compose_location = None
        limits_memory = "4g"
        autogenerate_domain = False
        health_check_enabled = False

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        coolify_module.cmd_app_create(None, Args())

    [call] = calls
    assert call["path"] == "/applications/private-deploy-key"
    body = call["body"]
    assert body["limits_memory"] == "4g"
    assert body["autogenerate_domain"] is False
    assert body["health_check_enabled"] is False
    assert body["dockerfile_location"] == "/dev/node/Dockerfile"
    assert body["private_key_uuid"] == "key-uuid"


def test_storage_add_sends_a_volume_or_a_host_path():
    """storage add posts a named volume, or a host path Coolify never writes."""
    calls = []

    def mock_request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path, "body": json_body})
        return {"uuid": "storage-uuid"}

    class Volume:
        uuid = "app-uuid"
        volume = "config"
        host_path = None
        mount_path = "/home/agent/.config"

    class HostPath:
        uuid = "app-uuid"
        volume = None
        host_path = "/var/run/docker.sock"
        mount_path = "/var/run/docker.sock"

    with patch.object(coolify_module, '_request', side_effect=mock_request):
        coolify_module.cmd_storage_add(None, Volume())
        coolify_module.cmd_storage_add(None, HostPath())

    assert [(c["method"], c["path"]) for c in calls] == [
        ("POST", "/applications/app-uuid/storages")] * 2
    assert calls[0]["body"] == {"type": "persistent", "name": "config",
                                "mount_path": "/home/agent/.config"}
    assert calls[1]["body"] == {"type": "file", "is_host_file": True,
                                "fs_path": "/var/run/docker.sock",
                                "mount_path": "/var/run/docker.sock"}
    assert "storage" in coolify_module.WRITE_VERBS


if __name__ == "__main__":
    import traceback

    tests = [
        ("projects create", test_projects_create),
        ("projects environment create", test_projects_environment_create),
        ("app create with public git", test_app_create_public_git),
        ("app create with registry image", test_app_create_registry_image),
        ("service create encodes compose", test_service_create),
        ("env list typed application", test_env_list_typed_application),
        ("env list typed service", test_env_list_typed_service),
        ("env bulk", test_env_bulk),
        ("app create memory limit and no generated domain",
         test_app_create_sets_memory_limit_and_no_generated_domain),
        ("storage add volume or host path", test_storage_add_sends_a_volume_or_a_host_path),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        try:
            test_fn()
            print(f"✓ {name}")
            passed += 1
        except Exception:
            print(f"✗ {name}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
