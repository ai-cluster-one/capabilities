from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path


CAP_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = CAP_ROOT / "deployment" / "bin" / "deployment"
SERVICE_CAPABILITIES = ("telegram", "automations", "slack")


def _manifest(name: str) -> dict:
    script = CAP_ROOT / name / "bin" / name
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
    assert sorted(agents) == ["project.automations.plist", "project.telegram.plist"]
    telegram = agents["project.telegram.plist"]
    assert telegram["Label"] == "project.telegram"
    # launchd resolves the program against its own PATH, so it is compiled in.
    assert Path(telegram["ProgramArguments"][0]).is_absolute()
    assert Path(telegram["ProgramArguments"][0]).name == "telegram"
    assert telegram["ProgramArguments"][1:] == ["service", "run"]
    assert telegram["WorkingDirectory"] == str(root)
    assert telegram["RunAtLoad"] is True


def test_a_deliberate_stop_is_honoured_but_a_crash_is_not(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    telegram = _agents(root)["project.telegram.plist"]
    # The whole point of the host profile: a person can stop it.
    assert telegram["KeepAlive"] == {"SuccessfulExit": False}


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
    assert payload["host_agents"] == ["project.telegram"]
    assert any("launchctl bootstrap" in step for step in payload["provider_steps"])
    assert any("service stop" in step for step in payload["provider_steps"])
    control = payload["manual_control"]
    assert control["restart"] == ["launchctl kickstart -k gui/$UID/project.telegram"]
    # launchctl is the switch for a job launchd owns; `service stop` only
    # reaches a daemon the capability started itself.
    assert control["stop"] == ["launchctl kill TERM gui/$UID/project.telegram"]
    assert control["start"] == ["launchctl kickstart gui/$UID/project.telegram"]
    assert control["remove"] == ["launchctl bootout gui/$UID/project.telegram"]


def test_doctor_reports_an_agent_that_was_never_installed(tmp_path: Path) -> None:
    root, env = _host_project(tmp_path, ("telegram",))
    _sync(root, env)
    proc = _run(root, env, "doctor")
    report = json.loads(proc.stdout)
    assert report["host_agents"] == [
        {"label": "project.telegram", "installed": False, "loaded": False,
         "pid": None, "last_exit_status": None}]
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
