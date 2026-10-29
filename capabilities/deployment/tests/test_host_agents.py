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


# --- Handing the host agents over to the node's service manager --------------

def test_handover_takes_down_the_service_agents_and_the_watchdog_only(
        tmp_path, monkeypatch, capsys) -> None:
    root, env = _host_project(tmp_path, ())
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "CAPABILITIES_HOME"):
        monkeypatch.setenv(key, env[key])
    monkeypatch.chdir(root)
    module = _module()
    agents = root / "deployment" / "launchd"
    agents.mkdir(parents=True, exist_ok=True)
    library = Path(env["HOME"]) / "Library" / "LaunchAgents"
    library.mkdir(parents=True)
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict/></plist>\n'
    for label in ("project.telegram", "project.watchdog", "project.node-sync-production"):
        path = agents / f"{label}.plist"
        path.write_text(module._mark_generated(path, xml))
    (agents / "project.mine.plist").write_text(xml)
    (library / "project.telegram.plist").symlink_to(agents / "project.telegram.plist")
    (library / "project.watchdog.plist").write_text("copied in by hand\n")
    (library / "project.node-sync-production.plist").symlink_to(
        agents / "project.node-sync-production.plist")
    loaded = {"project.telegram", "project.watchdog", "project.node-sync-production"}
    calls = []

    def launchctl(*argv):
        calls.append(argv)
        label = argv[1].rsplit("/", 1)[-1]
        if argv[0] == "print":
            return (0, "") if label in loaded else (113, "not found")
        if argv[0] == "bootout":
            loaded.discard(label)
        return 0, ""

    monkeypatch.setattr(module, "_launchctl", launchctl)

    def handover(dry_run):
        module.cmd_handover(type("Args", (), {"dry_run": dry_run})())
        return json.loads(capsys.readouterr().out)

    preview = handover(True)
    assert [(a["label"], a["kind"], a["action"]) for a in preview["agents"]] == [
        ("project.telegram", "service", "would_hand_over"),
        ("project.watchdog", "watchdog", "would_hand_over")]
    assert all(argv[0] == "print" for argv in calls) and len(loaded) == 3

    done = handover(False)
    assert done["ok"] and [a["action"] for a in done["agents"]] == ["handed_over"] * 2
    assert [argv[1].rsplit("/", 1)[-1] for argv in calls if argv[0] == "bootout"] == [
        "project.telegram", "project.watchdog"]
    assert loaded == {"project.node-sync-production"}
    assert not (library / "project.telegram.plist").exists()
    assert (library / "project.node-sync-production.plist").is_symlink()
    kept = agents / "project.watchdog.plist.handed-over"
    assert done["agents"][1]["kept_at"] == str(kept) and kept.read_text() == "copied in by hand\n"
    # The compiled files stay; a second run finds nothing to hand over.
    assert (agents / "project.telegram.plist").is_file()
    again = handover(False)
    assert [a["action"] for a in again["agents"]] == ["absent", "absent"]


def test_handover_finds_every_loaded_agent_of_the_folder_whatever_compiled_it(
        tmp_path, monkeypatch, capsys) -> None:
    """An older compilation's agents under another label, loaded from a file
    put in ~/Library/LaunchAgents, are this folder's too."""
    root, env = _host_project(tmp_path, ("telegram",))
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "CAPABILITIES_HOME"):
        monkeypatch.setenv(key, env[key])
    monkeypatch.chdir(root)
    module = _module()
    library = Path(env["HOME"]) / "Library" / "LaunchAgents"
    library.mkdir(parents=True)
    launchers = root / "deployment" / "launchd"

    def agent(label, program, **extra):
        (library / f"{label}.plist").write_bytes(plistlib.dumps(
            {"Label": label, "ProgramArguments": [str(program)], **extra}))

    agent("legacy.telegram", launchers / "legacy-telegram")
    agent("legacy.watchdog", launchers / "legacy-watchdog")
    agent("legacy.backup", root / "scripts" / "backup")
    agent("elsewhere.telegram", tmp_path / "elsewhere" / "telegram")
    agent("capabilities.node", "/usr/local/bin/capabilities", WorkingDirectory=str(root))
    loaded = {"legacy.telegram", "legacy.watchdog", "legacy.backup", "elsewhere.telegram",
              "capabilities.node"}

    def launchctl(*argv):
        label = argv[1].rsplit("/", 1)[-1]
        if argv[0] == "print":
            return (0, "") if label in loaded else (113, "not found")
        if argv[0] == "bootout":
            loaded.discard(label)
        return 0, ""

    monkeypatch.setattr(module, "_launchctl", launchctl)

    def handover(dry_run):
        module.cmd_handover(type("Args", (), {"dry_run": dry_run})())
        return json.loads(capsys.readouterr().out)

    preview = handover(True)
    assert [(a["label"], a["kind"], a.get("service"), a["action"])
            for a in preview["agents"]] == [
        ("legacy.backup", "other", None, "kept"),
        ("legacy.telegram", "service", "telegram", "would_hand_over"),
        ("legacy.watchdog", "watchdog", None, "would_hand_over")]
    done = handover(False)
    assert done["ok"] and [a["action"] for a in done["agents"]] == [
        "kept", "handed_over", "handed_over"]
    # What runs nothing the node manager takes over keeps running, and so
    # does what is not this folder's.
    assert loaded == {"legacy.backup", "elsewhere.telegram", "capabilities.node"}
    assert not (library / "legacy.telegram.plist").exists()
    assert (launchers / "legacy.telegram.plist.handed-over").is_file()


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
    assert finding["severity"] == "warning" and "deployment handover" in finding["hint"]
