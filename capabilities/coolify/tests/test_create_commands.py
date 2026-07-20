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
_coolify_path = Path(__file__).parent.parent / "bin" / "coolify"
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
        assert result == response


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
        assert result == response


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
        assert len(body) == 3
        assert body[0] == {"key": "KEY1", "value": "value1"}
        assert body[1] == {"key": "KEY2", "value": "value2"}
        assert body[2] == {"key": "KEY3", "value": "value3"}
        assert result == response


if __name__ == "__main__":
    import traceback

    tests = [
        ("projects create", test_projects_create),
        ("app create with public git", test_app_create_public_git),
        ("app create with registry image", test_app_create_registry_image),
        ("service create encodes compose", test_service_create),
        ("env list typed application", test_env_list_typed_application),
        ("env list typed service", test_env_list_typed_service),
        ("env bulk", test_env_bulk),
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
