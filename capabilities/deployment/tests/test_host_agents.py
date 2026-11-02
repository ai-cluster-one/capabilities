from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest


CAP_ROOT = Path(__file__).resolve().parents[2]
SERVICE_CAPABILITIES = ("telegram", "automations", "slack")


def _script(name: str) -> Path:
    root = CAP_ROOT / name
    return next((path for path in (root / "bin" / name, root / name)
                 if path.is_file()), root / "bin" / name)


DEPLOYMENT = _script("deployment")


def _manifest(name: str) -> dict:
    script = _script(name)
    proc = subprocess.run(
        [str(script), "manifest", "--json"], capture_output=True,
        text=True, timeout=30, check=True,
    )
    return json.loads(proc.stdout)


def _project(tmp_path: Path, enabled: tuple[str, ...] = ()) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / "capabilities").mkdir()
    caps = {"deployment": {"enabled": True}}
    caps.update({name: {"enabled": True} for name in enabled})
    (root / "capabilities" / "settings.json").write_text(
        json.dumps({"capabilities": caps}) + "\n"
    )
    registry = tmp_path / "registry"
    for name in SERVICE_CAPABILITIES:
        path = registry / name / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(_manifest(name)) + "\n")
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CAPABILITIES_HOME": str(registry),
    }
    return root, env


def _run(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DEPLOYMENT), *args], cwd=root, env=env, capture_output=True,
        text=True, timeout=60,
    )


def _host_project(tmp_path: Path, enabled: tuple[str, ...]) -> tuple[Path, dict[str, str]]:
    root, env = _project(tmp_path, enabled)
    proc = _run(root, env, "init", "--profile", "host-agents",
                "--target", "local", "--provider", "manual")
    assert proc.returncode == 0, proc.stderr
    return root, env


def _sync(root: Path, env: dict[str, str]) -> dict:
    proc = _run(root, env, "sync")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _agents(root: Path) -> dict[str, dict]:
    directory = root / "deployment" / "launchd"
    return {path.name: plistlib.loads(path.read_bytes())
            for path in sorted(directory.glob("*.plist"))}


def test_a_host_profile_compiles_no_container_artifacts(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    for name in ("Dockerfile", "docker-compose.yaml", "supervisord.conf",
                 "entrypoint.sh", ".dockerignore"):
        assert not (root / name).exists(), name
    runtime = json.loads((root / "deployment" / "runtime.json").read_text())
    assert "compose_file" not in runtime
    assert runtime["profile"] == "host-agents"


def test_a_host_profile_compiles_no_agent_for_a_service(tmp_path: Path) -> None:
    # The services run under the Mac's node manager; nothing here starts one.
    root, env = _host_project(tmp_path, ("telegram", "automations"))
    result = _sync(root, env)
    assert result["ok"], result["findings"]
    assert result["services"] == ["automations", "telegram"]
    assert _agents(root) == {}
    launchd = root / "deployment" / "launchd"
    assert not launchd.is_dir() or not any(launchd.iterdir())
    steps = " ".join(json.loads(_run(root, env, "next", "--json").stdout)["provider_steps"])
    assert "capabilities node install --apply" in steps
    assert "launchctl bootstrap" not in steps


def test_a_host_runtime_with_nothing_enabled_is_idle_not_invalid(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ())
    proc = _run(root, env, "doctor")
    assert proc.returncode == 0, proc.stdout
    report = json.loads(proc.stdout)
    assert report["ok"]
    assert report["host_agents"] == []


def test_a_host_runtime_declares_no_image_layout(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    # A host runtime is complete without the sections an image needs.
    assert set(runtime["compiler"]) == {"host"}
    assert runtime["compiler"]["host"]["supervisor"] == "launchd"

    runtime["compiler"]["container"] = {"agent_home": "/home/agent",
                                        "project_root": "/app"}
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")
    proc = _run(root, env, "sync", "--check")
    report = json.loads(proc.stdout)
    assert any("ignored by this profile" in finding["message"]
               for finding in report["findings"])
    # Sync strips it back out rather than carrying an image layout forward.
    _sync(root, env)
    assert set(json.loads(runtime_path.read_text())["compiler"]) == {"host"}


def test_an_unknown_host_supervisor_is_refused(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["compiler"]["host"]["supervisor"] = "systemd"
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")
    proc = _run(root, env, "doctor")
    assert proc.returncode == 6
    report = json.loads(proc.stdout)
    assert any("compiler.host.supervisor" in finding["message"]
               for finding in report["findings"])


def _module():
    loader = importlib.machinery.SourceFileLoader("deployment_under_test", str(DEPLOYMENT))
    spec = importlib.util.spec_from_loader("deployment_under_test", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_a_mac_handed_over_to_its_node_manager_is_not_told_to_load_its_agents(
        tmp_path, monkeypatch, capsys) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "CAPABILITIES_HOME"):
        monkeypatch.setenv(key, env[key])
    monkeypatch.chdir(root)
    _sync(root, env)
    module = _module()
    loaded = set()

    def status(label):
        return {"label": label, "installed": label == "capabilities.node",
                "loaded": label in loaded, "pid": None, "program": None}

    monkeypatch.setattr(module, "_launchd_status", status)
    # The node manager runs a project whose records are in the store.
    monkeypatch.setattr(module, "_records_in_store", lambda: True)
    assert module._handed_over()["label"] == "capabilities.node"
    monkeypatch.setattr(module, "_records_in_store", lambda: False)
    assert module._handed_over() is None
    monkeypatch.setattr(module, "_handed_over", lambda: status("capabilities.node"))

    def doctor():
        # Other findings of this bare project (no settings, an in-process
        # compile) are errors; what is asked here is what it says of launchd.
        with pytest.raises(SystemExit):
            module.cmd_doctor(type("Args", (), {"target": None})())
        return json.loads(capsys.readouterr().out)

    said = [f["message"] for f in doctor()["findings"]]
    assert not [m for m in said if "not installed" in m or "not loaded" in m], said
    payload = module._next_payload(root, None)
    assert payload["host_agents"] == []
    steps = " ".join(payload["provider_steps"])
    assert "capabilities service start telegram" in steps and "bootstrap" not in steps
    assert payload["manual_control"]["restart"] == ["capabilities service restart telegram"]
    # One loaded again beside the node manager is the one thing said.
    loaded.add("project.telegram")
    finding = next(f for f in doctor()["findings"] if f["path"] == "project.telegram")
    assert finding["severity"] == "warning"
    assert "`capabilities node install --apply`" in finding["hint"]
    assert "deployment handover" not in finding["hint"]
