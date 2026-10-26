#!/usr/bin/env python3
"""Tests for `service set-image`: one Compose service's image replaced in a
service's stored Compose document, through PATCH /services/<uuid>.

Run with: uv run --with httpx --with pytest pytest capabilities/coolify/tests/test_service_set_image.py
"""

import base64
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_capability = Path(__file__).resolve().parents[1]
_coolify_path = next((path for path in (
    _capability / "bin" / "coolify", _capability / "coolify")
    if path.is_file()), _capability / "bin" / "coolify")
coolify_module = types.ModuleType("coolify_set_image")
coolify_module.__file__ = str(_coolify_path)
exec(_coolify_path.read_text(), coolify_module.__dict__)
sys.modules["coolify_set_image"] = coolify_module

COMPOSE = """services:
  agent:
    image: "registry.example/agent:0.0.9"
    restart: unless-stopped
    environment:
      PROJECT: "x"
    volumes:
      - "config:/home/agent/.config"
volumes:
  config: {}
"""


class Args:
    def __init__(self, image, compose_service=None, uuid="svc-uuid"):
        self.uuid, self.image, self.compose_service = uuid, image, compose_service


def _fake(detail):
    calls = []

    def request(c, method, path, params=None, json_body=None):
        calls.append({"method": method, "path": path, "body": json_body})
        return detail if method == "GET" else {"uuid": "svc-uuid"}

    return calls, request


def test_set_image_patches_only_the_image_line():
    calls, request = _fake({"uuid": "svc-uuid", "docker_compose_raw": COMPOSE})
    with patch.object(coolify_module, "_request", side_effect=request):
        answer = coolify_module.cmd_service_set_image(None, Args("registry.example/agent:0.0.10"))
    assert answer["changed"] is True
    assert answer["compose_service"] == "agent"
    assert answer["previous_image"] == "registry.example/agent:0.0.9"
    assert answer["image"] == "registry.example/agent:0.0.10"
    assert [c["method"] for c in calls] == ["GET", "PATCH"]
    assert calls[1]["path"] == "/services/svc-uuid"
    sent = base64.b64decode(calls[1]["body"]["docker_compose_raw"]).decode()
    assert sent == COMPOSE.replace("agent:0.0.9", "agent:0.0.10")


def test_set_image_names_the_service_in_a_document_of_several():
    raw = ("services:\n  web:\n    image: nginx:1  # pinned\n  'worker':\n"
           "    image: 'registry.example/w:1'\n    command: run\n")
    calls, request = _fake({"docker_compose_raw": raw})
    with patch.object(coolify_module, "_request", side_effect=request):
        answer = coolify_module.cmd_service_set_image(
            None, Args("registry.example/w:2", compose_service="worker"))
    assert answer["previous_image"] == "registry.example/w:1"
    sent = base64.b64decode(calls[1]["body"]["docker_compose_raw"]).decode()
    assert sent == raw.replace("'registry.example/w:1'", '"registry.example/w:2"')
    with patch.object(coolify_module, "_request", side_effect=request):
        with pytest.raises(SystemExit) as exc:
            coolify_module.cmd_service_set_image(None, Args("x:1"))
    assert exc.value.code == 6


def test_set_image_already_that_image_sends_nothing():
    calls, request = _fake({"docker_compose_raw": COMPOSE})
    with patch.object(coolify_module, "_request", side_effect=request):
        answer = coolify_module.cmd_service_set_image(None, Args("registry.example/agent:0.0.9"))
    assert answer["changed"] is False
    assert [c["method"] for c in calls] == ["GET"]


def test_set_image_without_the_document_is_an_ability_refusal():
    calls, request = _fake({"uuid": "svc-uuid"})
    with patch.object(coolify_module, "_request", side_effect=request):
        with pytest.raises(SystemExit) as exc:
            coolify_module.cmd_service_set_image(None, Args("registry.example/agent:1"))
    assert exc.value.code == 2
    assert [c["method"] for c in calls] == ["GET"]


@pytest.mark.parametrize("image", ["", "-x", "a b", "x;rm"])
def test_set_image_refuses_a_malformed_reference_before_any_request(image):
    calls, request = _fake({"docker_compose_raw": COMPOSE})
    with patch.object(coolify_module, "_request", side_effect=request):
        with pytest.raises(SystemExit) as exc:
            coolify_module.cmd_service_set_image(None, Args(image))
    assert exc.value.code == 6
    assert calls == []


def test_set_image_refuses_a_service_without_an_image():
    raw = "services:\n  app:\n    build: .\n"
    calls, request = _fake({"docker_compose_raw": raw})
    with patch.object(coolify_module, "_request", side_effect=request):
        with pytest.raises(SystemExit) as exc:
            coolify_module.cmd_service_set_image(None, Args("x:1"))
    assert exc.value.code == 6
    assert [c["method"] for c in calls] == ["GET"]
