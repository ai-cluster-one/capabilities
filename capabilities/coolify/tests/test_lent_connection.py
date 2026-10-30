#!/usr/bin/env python3
"""A connection lent to one process through COOLIFY_LENT_BASE_URL and
COOLIFY_LENT_TOKEN is that run's only connection: no registry is read, and
nothing selects another beside it.

Run with: uv run --with httpx --with pytest pytest capabilities/coolify/tests/test_lent_connection.py
"""

import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_capability = Path(__file__).resolve().parents[1]
_coolify_path = _capability / "bin" / "coolify"
coolify_module = types.ModuleType("coolify_lent_connection")
coolify_module.__file__ = str(_coolify_path)
exec(_coolify_path.read_text(), coolify_module.__dict__)
sys.modules["coolify_lent_connection"] = coolify_module


def _args(**given):
    return types.SimpleNamespace(**{"connection": None, "base_url": None, "token": None,
                                    **given})


def _no_registry():
    raise AssertionError("a lent connection reads no registry")


def test_a_lent_connection_is_the_runs_only_one_and_reads_no_registry(monkeypatch):
    monkeypatch.setenv("COOLIFY_LENT_BASE_URL", "https://coolify.example.com/")
    monkeypatch.setenv("COOLIFY_LENT_TOKEN", "1|lent-token")
    with patch.object(coolify_module, "_connections_registry", _no_registry):
        conn = coolify_module._resolve_conn(_args())
    assert conn == {"id": "lent", "allow_write": True, "base_url": "https://coolify.example.com",
                    "token": "1|lent-token", "ssh": None}


@pytest.mark.parametrize("given", [{"connection": "infra"}, {"base_url": "https://x.example"},
                                   {"token": "other"}])
def test_nothing_selects_another_connection_beside_a_lent_one(monkeypatch, capsys, given):
    monkeypatch.setenv("COOLIFY_LENT_BASE_URL", "https://coolify.example.com")
    monkeypatch.setenv("COOLIFY_LENT_TOKEN", "1|lent-token")
    with pytest.raises(SystemExit) as exit_:
        coolify_module._resolve_conn(_args(**given))
    assert exit_.value.code == 6
    assert "lent_connection" in capsys.readouterr().err


def test_half_a_lent_connection_is_refused(monkeypatch, capsys):
    monkeypatch.delenv("COOLIFY_LENT_BASE_URL", raising=False)
    monkeypatch.setenv("COOLIFY_LENT_TOKEN", "1|lent-token")
    with pytest.raises(SystemExit) as exit_:
        coolify_module._resolve_conn(_args())
    assert exit_.value.code == 6
    err = capsys.readouterr().err
    assert "lent_connection_incomplete" in err and "1|lent-token" not in err


def test_without_a_lent_connection_the_registry_selects_as_before(monkeypatch):
    monkeypatch.delenv("COOLIFY_LENT_BASE_URL", raising=False)
    monkeypatch.delenv("COOLIFY_LENT_TOKEN", raising=False)
    reg = {"default": "infra", "connections": {"infra": {
        "base_url": "https://coolify.example.com", "secret_env": "COOLIFY_INFRA_TOKEN",
        "allow_write": False}}}
    monkeypatch.setenv("COOLIFY_INFRA_TOKEN", "1|granted")
    with patch.object(coolify_module, "_connections_registry", lambda: (reg, None)), \
            patch.object(coolify_module, "_resolve_env_key", lambda key: ("1|granted", "env")):
        conn = coolify_module._resolve_conn(_args())
    assert conn["id"] == "infra" and conn["token"] == "1|granted" and conn["allow_write"] is False
