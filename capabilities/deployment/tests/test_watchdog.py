"""The watchdog: what a supervisor cannot see.

launchd restarts a process that died. A process that is alive and doing
nothing - a deadlocked interpreter, a service wedged behind a native lock -
looks healthy to it, and stays that way until a person notices. These cover
the decision the watchdog makes instead, which is worth being careful about:
restarting a service is disruptive, and restarting one repeatedly buries the
evidence of whatever is actually wrong.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


CAP_ROOT = Path(__file__).resolve().parents[2]
CAPABILITY = CAP_ROOT / "deployment"
DEPLOYMENT = next((path for path in (
    CAPABILITY / "bin" / "deployment", CAPABILITY / "deployment")
    if path.is_file()), CAPABILITY / "bin" / "deployment")


def _module():
    spec = importlib.util.spec_from_loader(
        "deployment_under_test",
        importlib.machinery.SourceFileLoader("deployment_under_test",
                                             str(DEPLOYMENT)))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def watchdog(tmp_path, monkeypatch):
    """A project with one supervised service and every outside call faked."""
    module = _module()
    root = tmp_path / "project"
    (root / "deployment" / "launchd").mkdir(parents=True)
    runtime = {
        "profile": "host-agents",
        "compiler": {"host": {"supervisor": "launchd",
                              "agents_dir": "deployment/launchd"}},
        "services": {"telegram": {"doctor": ["telegram", "service", "doctor"]}},
    }
    monkeypatch.setattr(module, "_root", lambda: root)
    monkeypatch.setattr(module, "_load_runtime", lambda _root: runtime)
    monkeypatch.setattr(module, "_host_label_prefix", lambda *_: "proj")

    calls = {"kickstarts": [], "probe": {"ok": True, "detail": "fine"},
             "launchd": {"loaded": True, "pid": "123"}}

    monkeypatch.setattr(module, "_launchd_status",
                        lambda label: {"label": label, **calls["launchd"]})
    monkeypatch.setattr(module, "_probe_service",
                        lambda *_args, **_kw: calls["probe"])

    def kickstart(label):
        calls["kickstarts"].append(label)
        return {"restarted": True, "detail": "launchctl kickstart -k"}

    monkeypatch.setattr(module, "_kickstart", kickstart)
    module._test_root = root
    module._test_calls = calls
    return module


def _one(report):
    assert len(report["services"]) == 1
    return report["services"][0]


def _state(module):
    path = module._test_root / "deployment" / "launchd" / ".watchdog-state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def test_a_working_service_is_left_alone(watchdog):
    entry = _one(watchdog.cmd_watchdog(dry_run=False))
    assert entry["action"] == "none"
    assert watchdog._test_calls["kickstarts"] == []


def test_one_bad_answer_is_watched_not_acted_on(watchdog):
    """A service that is still starting answers badly once."""
    watchdog._test_calls["probe"] = {"ok": False, "detail": "doctor exited 1"}
    entry = _one(watchdog.cmd_watchdog(dry_run=False))
    assert entry["action"] == "watching"
    assert entry["failures"] == 1
    assert watchdog._test_calls["kickstarts"] == []


def test_the_second_bad_answer_restarts_it(watchdog):
    watchdog._test_calls["probe"] = {"ok": False, "detail": "doctor exited 1"}
    watchdog.cmd_watchdog(dry_run=False, now=1000.0)
    entry = _one(watchdog.cmd_watchdog(dry_run=False, now=1060.0))
    assert entry["action"] == "restarted"
    assert watchdog._test_calls["kickstarts"] == ["proj.telegram"]
    assert _state(watchdog)["telegram"]["last_restart"] == 1060.0


def test_a_restart_that_did_not_help_is_not_repeated_every_interval(watchdog):
    """The restarted service gets the same grace as any other - one bad answer
    is watched - and past that the cooldown holds until it has had real time."""
    watchdog._test_calls["probe"] = {"ok": False, "detail": "doctor exited 1"}
    watchdog.cmd_watchdog(dry_run=False, now=1000.0)
    watchdog.cmd_watchdog(dry_run=False, now=1060.0)
    assert watchdog._test_calls["kickstarts"] == ["proj.telegram"]

    actions = [_one(watchdog.cmd_watchdog(dry_run=False, now=moment))["action"]
               for moment in range(1120, 1360, 60)]
    assert actions[0] == "watching"
    assert set(actions[1:]) == {"cooling down"}
    assert watchdog._test_calls["kickstarts"] == ["proj.telegram"]


def test_once_the_cooldown_is_over_it_may_restart_again(watchdog):
    watchdog._test_calls["probe"] = {"ok": False, "detail": "doctor exited 1"}
    watchdog.cmd_watchdog(dry_run=False, now=1000.0)
    watchdog.cmd_watchdog(dry_run=False, now=1060.0)
    watchdog.cmd_watchdog(dry_run=False, now=1400.0)
    entry = _one(watchdog.cmd_watchdog(dry_run=False, now=1460.0))
    assert entry["action"] == "restarted"
    assert watchdog._test_calls["kickstarts"] == ["proj.telegram", "proj.telegram"]


def test_recovering_clears_the_count(watchdog):
    watchdog._test_calls["probe"] = {"ok": False, "detail": "doctor exited 1"}
    watchdog.cmd_watchdog(dry_run=False, now=1000.0)
    watchdog._test_calls["probe"] = {"ok": True, "detail": "fine"}
    watchdog.cmd_watchdog(dry_run=False, now=1060.0)
    watchdog._test_calls["probe"] = {"ok": False, "detail": "doctor exited 1"}
    entry = _one(watchdog.cmd_watchdog(dry_run=False, now=1120.0))
    assert entry["action"] == "watching"
    assert watchdog._test_calls["kickstarts"] == []


def test_a_probe_that_cannot_run_is_not_evidence_of_anything(watchdog):
    """Not knowing is not the same as knowing it is broken."""
    watchdog._test_calls["probe"] = {"ok": None, "detail": "cannot run doctor"}
    for moment in (1000.0, 1060.0, 1120.0):
        entry = _one(watchdog.cmd_watchdog(dry_run=False, now=moment))
        assert entry["action"] == "none"
    assert watchdog._test_calls["kickstarts"] == []


def test_an_agent_a_person_never_loaded_is_not_started_here(watchdog):
    watchdog._test_calls["launchd"] = {"loaded": False, "pid": None}
    watchdog._test_calls["probe"] = {"ok": False, "detail": "doctor exited 1"}
    entry = _one(watchdog.cmd_watchdog(dry_run=False))
    assert entry["action"] == "none"
    assert watchdog._test_calls["kickstarts"] == []


def test_a_job_launchd_is_already_restarting_is_left_to_it(watchdog):
    watchdog._test_calls["launchd"] = {"loaded": True, "pid": None}
    watchdog._test_calls["probe"] = {"ok": False, "detail": "doctor exited 1"}
    entry = _one(watchdog.cmd_watchdog(dry_run=False))
    assert entry["action"] == "none"
    assert watchdog._test_calls["kickstarts"] == []


def test_a_dry_run_says_what_it_would_do_and_writes_nothing(watchdog):
    watchdog._test_calls["probe"] = {"ok": False, "detail": "doctor exited 1"}
    watchdog.cmd_watchdog(dry_run=True, now=1000.0)
    entry = _one(watchdog.cmd_watchdog(dry_run=True, now=1060.0))
    assert entry["action"] in ("watching", "would restart")
    assert watchdog._test_calls["kickstarts"] == []
    assert _state(watchdog) == {}


def test_a_disabled_watchdog_does_nothing(watchdog, monkeypatch):
    runtime = {
        "profile": "host-agents",
        "compiler": {"host": {"supervisor": "launchd",
                              "agents_dir": "deployment/launchd",
                              "watchdog": {"enabled": False}}},
        "services": {"telegram": {"doctor": ["telegram", "service", "doctor"]}},
    }
    monkeypatch.setattr(watchdog, "_load_runtime", lambda _root: runtime)
    report = watchdog.cmd_watchdog(dry_run=False)
    assert report["services"] == []
    assert "disabled" in report["skipped"]


def test_a_container_profile_has_no_launchd_to_watch(watchdog, monkeypatch):
    runtime = {"profile": "agent-box", "services": {"telegram": {}}}
    monkeypatch.setattr(watchdog, "_load_runtime", lambda _root: runtime)
    report = watchdog.cmd_watchdog(dry_run=False)
    assert report["services"] == []
    assert "host agents" in report["skipped"]


@pytest.mark.parametrize("raw,key,expected", [
    ({"interval_seconds": 5}, "interval_seconds", 60),        # below the floor
    ({"interval_seconds": 120}, "interval_seconds", 120),
    ({"failures_before_restart": 0}, "failures_before_restart", 2),
    ({"failures_before_restart": 3}, "failures_before_restart", 3),
    ({"probe_timeout_seconds": 999}, "probe_timeout_seconds", 30),
    ({"enabled": "yes"}, "enabled", True),                    # not a bool
    ({"interval_seconds": True}, "interval_seconds", 60),     # bool is not a count
])
def test_unusable_settings_fall_back_to_the_default(raw, key, expected):
    assert _module()._watchdog_config(raw)[key] == expected
