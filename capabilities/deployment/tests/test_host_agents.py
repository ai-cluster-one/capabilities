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


def test_every_enabled_service_becomes_one_agent(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram", "automations"))
    result = _sync(root, env)
    assert result["ok"], result["findings"]

    agents = _agents(root)
    assert sorted(agents) == ["project.automations.plist",
                              "project.telegram.plist",
                              "project.watchdog.plist"]
    telegram = agents["project.telegram.plist"]
    assert telegram["Label"] == "project.telegram"
    # launchd resolves the program against its own PATH, so it is compiled in.
    assert Path(telegram["ProgramArguments"][0]).is_absolute()
    # The name macOS shows is this basename, so it carries the project.
    assert Path(telegram["ProgramArguments"][0]).name == "project-telegram"
    assert telegram["ProgramArguments"][1:] == []
    assert telegram["WorkingDirectory"] == str(root)
    assert telegram["RunAtLoad"] is True


def test_each_service_gets_a_launcher_named_for_its_project(tmp_path: Path) -> None:
    """Two projects supervising the same capability are told apart by a person
    reading a list of background items, which is a list of program names."""
    root, env = _host_project(tmp_path, ("telegram", "automations"))
    assert _sync(root, env)["ok"]

    launcher = root / "deployment" / "launchd" / "project-telegram"
    assert launcher.is_file()
    assert os.access(launcher, os.X_OK)
    body = launcher.read_text()
    assert body.startswith("#!")
    # The agent runs the service's runner; a clean stop stays a stop.
    assert "runner telegram --every 60 --restart on-failure -- telegram service run" in body
    assert body.rstrip().endswith('"$@"')

    plist = _agents(root)["project.telegram.plist"]
    assert plist["ProgramArguments"] == [str(launcher)]


def test_a_deliberate_stop_is_honoured_but_a_crash_is_not(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    telegram = _agents(root)["project.telegram.plist"]
    # The whole point of the host profile: a person can stop it.
    assert telegram["KeepAlive"] == {"SuccessfulExit": False}


def test_a_service_runs_at_standard_priority_and_its_watchdog_does_not(
        tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    agents = _agents(root)
    assert agents["project.telegram.plist"]["ProcessType"] == "Standard"
    assert agents["project.watchdog.plist"]["ProcessType"] == "Background"


def test_a_host_profile_compiles_no_container_artifacts(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    for name in ("Dockerfile", "docker-compose.yaml", "supervisord.conf",
                 "entrypoint.sh", ".dockerignore"):
        assert not (root / name).exists(), name
    runtime = json.loads((root / "deployment" / "runtime.json").read_text())
    assert "compose_file" not in runtime
    assert runtime["profile"] == "host-agents"


def test_the_agent_carries_a_path_because_launchd_supplies_none(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    environment = _agents(root)["project.telegram.plist"]["EnvironmentVariables"]
    entries = environment["PATH"].split(":")

    telegram_dir = str(Path(shutil.which("telegram")).parent)
    assert entries[0] == str(Path(telegram_dir).resolve())
    # The whole proven PATH follows, so the workers a service spawns are found
    # too, and every entry is a real directory rather than a hopeful guess.
    assert {"/usr/bin", "/bin"} <= set(entries)
    assert all(Path(entry).is_dir() for entry in entries)
    assert len(entries) == len(set(entries))


def test_a_per_session_shim_is_recorded_as_its_stable_target(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    stable = tmp_path / "toolchain" / "v1" / "bin"
    stable.mkdir(parents=True)
    shim = tmp_path / "shim"
    shim.symlink_to(tmp_path / "toolchain" / "v1")
    _sync(root, {**env, "PATH": f"{shim / 'bin'}:{env['PATH']}"})
    entries = _agents(root)["project.telegram.plist"][
        "EnvironmentVariables"]["PATH"].split(":")
    assert str(stable.resolve()) in entries
    assert str(shim / "bin") not in entries


def test_no_secret_is_written_into_an_agent(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    environment = _agents(root)["project.telegram.plist"]["EnvironmentVariables"]
    # Declared as required by the descriptor, resolved by the capability's own
    # credential cascade at run time — never compiled into a world-readable file.
    assert "TELEGRAM_API_HASH" not in environment


def test_a_declared_default_overrides_the_descriptor(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("automations",))
    _sync(root, env)
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    # The descriptor's own default lands in the compiled service first.
    assert (runtime["services"]["automations"]["environment_defaults"]
            ["AUTOMATIONS_ENVIRONMENT"]) == "production"
    agent = _agents(root)["project.automations.plist"]
    assert agent["EnvironmentVariables"]["AUTOMATIONS_ENVIRONMENT"] == "production"

    runtime["services"]["automations"]["environment_defaults"] = {
        "AUTOMATIONS_ENVIRONMENT": "development"}
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")
    _sync(root, env)
    agent = _agents(root)["project.automations.plist"]
    assert agent["EnvironmentVariables"]["AUTOMATIONS_ENVIRONMENT"] == "development"


def test_an_edited_agent_is_reported_as_drift(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    plist = root / "deployment" / "launchd" / "project.telegram.plist"
    plist.write_text(plist.read_text().replace("<integer>10</integer>",
                                               "<integer>99</integer>"))
    proc = _run(root, env, "sync", "--check")
    assert proc.returncode == 7, proc.stdout
    report = json.loads(proc.stdout)
    assert [item["path"] for item in report["drift"]] == [
        "deployment/launchd/project.telegram.plist"]


def test_next_hands_over_launchctl_and_keeps_manual_control(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    proc = _run(root, env, "next", "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    # The watchdog is handed over with the rest: an agent nobody loaded is an
    # agent that never restarts anything.
    assert payload["host_agents"] == ["project.telegram", "project.watchdog"]
    assert any("launchctl bootstrap" in step for step in payload["provider_steps"])
    assert any("service stop" in step for step in payload["provider_steps"])
    control = payload["manual_control"]
    assert control["restart"] == [
        "launchctl kickstart -k gui/$UID/project.telegram",
        "launchctl kickstart -k gui/$UID/project.watchdog"]
    # launchctl is the switch for a job launchd owns; `service stop` only
    # reaches a daemon the capability started itself.
    assert control["stop"][0] == "launchctl kill TERM gui/$UID/project.telegram"
    assert control["start"][0] == "launchctl kickstart gui/$UID/project.telegram"
    assert control["remove"][0] == "launchctl bootout gui/$UID/project.telegram"


def test_doctor_reports_an_agent_that_was_never_installed(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    proc = _run(root, env, "doctor")
    report = json.loads(proc.stdout)
    assert report["host_agents"] == [
        {"label": "project.telegram", "installed": False, "loaded": False,
         "pid": None, "last_exit_status": None, "program": None,
         "spawn_type": None}]
    assert any("not installed" in finding["message"]
               for finding in report["findings"])


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


def test_agent_logs_land_in_a_directory_that_exists(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    agent = _agents(root)["project.telegram.plist"]
    # launchd creates no directory for these, so they belong beside the agents.
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert Path(agent[key]).parent == root / "deployment" / "launchd"
        assert Path(agent[key]).parent.is_dir()


def test_a_command_missing_from_this_machine_is_refused(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    telegram = shutil.which("telegram")
    assert telegram, "the test needs telegram installed to hide it"
    hidden = str(Path(telegram).parent)
    stripped = {**env, "PATH": ":".join(
        entry for entry in env["PATH"].split(":") if entry != hidden)}
    proc = _run(root, stripped, "sync")
    assert proc.returncode == 6, proc.stdout
    report = json.loads(proc.stdout)
    assert any("not on PATH here" in finding["message"]
               for finding in report["findings"])


def test_next_removes_the_installed_agent_before_relinking(tmp_path: Path) -> None:
    """macOS records an agent's program when the file appears and keeps that
    record across a reload, so a changed program only reaches the background
    list once the installed file has gone away and come back."""
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    proc = _run(root, env, "next", "--json")
    steps = " ".join(json.loads(proc.stdout)["provider_steps"])
    assert "rm -f" in steps
    assert steps.index("bootout") < steps.index("rm -f") < steps.index("ln -sf")
    assert steps.index("ln -sf") < steps.index("bootstrap")


def test_doctor_reports_the_program_launchd_holds(tmp_path: Path) -> None:
    """Without it there is nothing to compare the compiled agent against."""
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    report = json.loads(_run(root, env, "doctor").stdout)
    assert "program" in report["host_agents"][0]


def test_the_watchdog_is_scheduled_rather_than_supervised(tmp_path: Path) -> None:
    """It looks and exits. Keeping it alive would be a busy loop, and running
    it at load would judge services that are still coming up."""
    root, env = _host_project(tmp_path, ("telegram",))
    assert _sync(root, env)["ok"]

    plist = _agents(root)["project.watchdog.plist"]
    assert plist["Label"] == "project.watchdog"
    assert plist["StartInterval"] == 60
    assert "KeepAlive" not in plist
    assert plist["RunAtLoad"] is False
    assert plist["WorkingDirectory"] == str(root)

    launcher = root / "deployment" / "launchd" / "project-watchdog"
    assert plist["ProgramArguments"] == [str(launcher)]
    assert "watchdog" in launcher.read_text()


def test_the_watchdog_can_reach_the_doctors_it_has_to_run(tmp_path: Path) -> None:
    """launchd hands a job a bare environment, and a watchdog that cannot run
    a probe would quietly never restart anything."""
    root, env = _host_project(tmp_path, ("telegram",))
    assert _sync(root, env)["ok"]

    path = _agents(root)["project.watchdog.plist"]["EnvironmentVariables"]["PATH"]
    telegram = shutil.which("telegram")
    if telegram:
        assert str(Path(telegram).parent) in path.split(os.pathsep)


def test_a_watchdog_turned_off_before_the_first_sync_is_never_written(
        tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime.setdefault("compiler", {}).setdefault("host", {})["watchdog"] = {
        "enabled": False}
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")

    _run(root, env, "sync", "--adopt")
    assert "project.watchdog.plist" not in _agents(root)


def test_turning_the_watchdog_off_says_the_agent_is_still_there(
        tmp_path: Path) -> None:
    """sync writes and never deletes, so an agent already loaded goes on
    running - and a person who thinks they turned it off should be told."""
    root, env = _host_project(tmp_path, ("telegram",))
    assert _sync(root, env)["ok"]
    assert "project.watchdog.plist" in _agents(root)

    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["compiler"]["host"]["watchdog"] = {"enabled": False}
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")

    proc = _run(root, env, "sync", "--adopt")
    report = json.loads(proc.stdout)
    messages = [f["message"] for f in report["findings"]]
    assert any("watchdog is disabled but its agent is still here" in m
               for m in messages), messages


def test_a_declared_interval_reaches_the_agent(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    assert _sync(root, env)["ok"]
    runtime_path = root / "deployment" / "runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["compiler"]["host"]["watchdog"] = {"interval_seconds": 180}
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")

    _run(root, env, "sync", "--adopt")
    assert _agents(root)["project.watchdog.plist"]["StartInterval"] == 180


# --- An agent loaded from an older compilation --------------------------------
# A job keeps the process it started when sync rewrites its launcher, so an
# agent whose launcher once ran `<capability> service run` directly goes on
# running the service outside the gate and the lease. These fake launchd and
# the process table; the decision and the launchctl sequence are what is real.

OLD_WRAPPER = "/Users/someone/bin/telegram service run"


def _module():
    loader = importlib.machinery.SourceFileLoader("deployment_under_test", str(DEPLOYMENT))
    spec = importlib.util.spec_from_loader("deployment_under_test", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    """A host project whose telegram agent launchd holds, running `process`."""
    root, env = _host_project(tmp_path, ("telegram",))
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "CAPABILITIES_HOME"):
        monkeypatch.setenv(key, env[key])
    monkeypatch.chdir(root)
    module = _module()
    launcher = root / "deployment" / "launchd" / "project-telegram"
    state = {"process": OLD_WRAPPER, "pid": "4242", "program": str(launcher),
             "calls": [], "answers": {}}

    def status(label):
        if label != "project.telegram":
            return {"label": label, "installed": False, "loaded": False,
                    "pid": None, "program": None}
        return {"label": label, "installed": True, "loaded": True,
                "pid": state["pid"], "program": state["program"]}

    def launchctl(*argv):
        state["calls"].append(argv)
        return state["answers"].get(argv[0], (0, ""))

    monkeypatch.setattr(module, "_launchd_status", status)
    monkeypatch.setattr(module, "_process_args", lambda pid: state["process"])
    monkeypatch.setattr(module, "_launchctl", launchctl)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    module._test = (root, state)
    return module


def _sync_here(module, check=False):
    root, _state = module._test
    return module._sync(root, module._load_runtime(root), check=check)


def test_sync_replaces_an_agent_that_runs_its_service_outside_the_runner(loaded) -> None:
    root, state = loaded._test
    installed = Path(os.environ["HOME"]) / "Library" / "LaunchAgents" / "project.telegram.plist"
    installed.parent.mkdir(parents=True)
    installed.write_text("an older compilation, copied in\n")
    result = _sync_here(loaded)
    assert result["ok"], result["findings"]
    assert result["agents"] == [{"label": "project.telegram", "action": "replaced",
                                 "ran": OLD_WRAPPER}]
    assert [argv[0] for argv in state["calls"]] == ["bootout", "bootstrap"]
    assert state["calls"][0][1].endswith("/project.telegram")
    assert state["calls"][1][2] == str(installed)
    # The installed copy goes, so launchd records the compiled program afresh.
    assert installed.is_symlink()
    assert installed.resolve() == (root / "deployment" / "launchd"
                                   / "project.telegram.plist").resolve()


def test_an_agent_running_its_runner_is_left_alone(loaded) -> None:
    _root, state = loaded._test
    state["process"] = ("uv run --script /opt/caps/bin/deployment runner telegram "
                        "--every 60 --restart on-failure -- /opt/caps/bin/telegram service run")
    result = _sync_here(loaded)
    assert result["ok"] and result["agents"] == [] and state["calls"] == []


def test_an_agent_with_no_process_is_judged_by_the_program_it_would_start(loaded) -> None:
    _root, state = loaded._test
    state["pid"] = None
    assert _sync_here(loaded)["agents"] == [] and state["calls"] == []
    state["program"] = "/Users/someone/bin/telegram"
    assert _sync_here(loaded)["agents"][0]["action"] == "replaced"


def test_check_reports_the_agent_and_touches_nothing(loaded) -> None:
    _root, state = loaded._test
    result = _sync_here(loaded, check=True)
    assert not result["ok"] and state["calls"] == []
    finding = next(f for f in result["findings"] if f["path"] == "project.telegram")
    assert finding["severity"] == "error" and OLD_WRAPPER in finding["message"]


def test_an_agent_taken_down_but_not_loaded_again_is_an_error(loaded) -> None:
    _root, state = loaded._test
    state["answers"]["bootstrap"] = (5, "Bootstrap failed: 5: Input/output error")
    result = _sync_here(loaded)
    assert result["agents"][0]["action"] == "unloaded"
    assert result["agents"][0]["step"] == "bootstrap"
    assert not result["ok"]
    assert any(f["path"] == "project.telegram" and f["severity"] == "error"
               for f in result["findings"])


def test_doctor_names_an_agent_running_its_service_outside_the_runner(loaded, capsys) -> None:
    class Args:
        target = None
    with pytest.raises(SystemExit) as exit_:
        loaded.cmd_doctor(Args())
    assert exit_.value.code == 6
    report = json.loads(capsys.readouterr().out)
    finding = next(f for f in report["findings"] if f["path"] == "project.telegram")
    assert finding["severity"] == "error"
    assert "project.telegram" in finding["message"] and OLD_WRAPPER in finding["message"]


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
