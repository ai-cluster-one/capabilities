"""The checkout profile builds the same box and fills it differently.

What is proven here is the difference itself: the image must not carry the
project, the body must be a mount rather than a layer, the initialization that
needs a checkout must move to boot, and the body must stay level with the branch
it tracks. The baked profile is asserted alongside each of these, because
the whole point of a separate profile is that it left the existing one alone.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


CAP_ROOT = Path(__file__).resolve().parents[2]
SERVICE_CAPABILITIES = ("telegram", "automations")


def _script(name: str) -> Path:
    root = CAP_ROOT / name
    return next((path for path in (root / "bin" / name, root / name)
                 if path.is_file()), root / "bin" / name)


DEPLOYMENT = _script("deployment")
# The compiler declares this name once; the suite re-declares it here so a
# rename has to move both and cannot pass silently.
SYNC_PROGRAM = "body-sync.sh"
SYNC_ENV = ("AGENT_BODY_SYNC", "AGENT_BODY_SYNC_INTERVAL", "AGENT_BODY_SYNC_QUIET")
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "desk", "GIT_AUTHOR_EMAIL": "desk@local",
    "GIT_COMMITTER_NAME": "desk", "GIT_COMMITTER_EMAIL": "desk@local",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


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


def _setup(root: Path, env: dict[str, str], profile: str) -> dict:
    proc = _run(root, env, "setup", "--profile", profile, "--provider", "manual", "--force")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_image_carries_the_boot_path_and_not_the_project(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    dockerfile = (root / "Dockerfile").read_text()
    assert "COPY --chown=${USERNAME}:${USERNAME} . /app" not in dockerfile
    assert ("COPY --chown=${USERNAME}:${USERNAME} deployment/capabilities.lock "
            "entrypoint.sh supervisord.conf " + SYNC_PROGRAM + " /opt/agent/") in dockerfile
    # A COPY carries the mode the build context held, so every generated program
    # the boot path runs is made executable, not only the entrypoint.
    assert ("RUN chmod +x /opt/agent/entrypoint.sh /opt/agent/" + SYNC_PROGRAM) in dockerfile
    # The lock and the entrypoint are read from the boot directory: the volume
    # mounts over the project root and would hide anything left under it.
    assert 'ENTRYPOINT ["/opt/agent/entrypoint.sh"]' in dockerfile
    assert "/opt/agent/capabilities.lock" in dockerfile
    assert "/app/deployment/capabilities.lock" not in dockerfile


def test_baked_profile_still_copies_the_project(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box")
    dockerfile = (root / "Dockerfile").read_text()
    assert "COPY --chown=${USERNAME}:${USERNAME} . /app" in dockerfile
    assert "/opt/agent" not in dockerfile
    assert 'ENTRYPOINT ["/app/entrypoint.sh"]' in dockerfile


def test_project_initialization_moves_from_build_to_boot(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    dockerfile = (root / "Dockerfile").read_text()
    entrypoint = (root / "entrypoint.sh").read_text()
    # Without a checkout at build time there is nothing to initialize against.
    assert "capabilities init" not in dockerfile
    assert "capabilities init --codex --claude" in entrypoint
    assert "capabilities doctor" in entrypoint


def test_body_is_a_mount_and_the_clone_is_declared(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    runtime = json.loads((root / "deployment" / "runtime.json").read_text())
    assert runtime["volumes"]["agent_body"]["mount"] == "/app"
    assert "agent_body" in runtime["services"]["agent"]["state"]
    assert "AGENT_REPO_URL" in runtime["services"]["agent"]["required_env"]
    assert runtime["services"]["agent"]["environment_defaults"]["AGENT_REPO_BRANCH"] == "main"
    compose = (root / "docker-compose.yaml").read_text()
    assert "- agent_body:/app" in compose
    # Compose passes only declared keys, so an undeclared one never reaches the
    # entrypoint and a fresh volume would have nothing to clone.
    assert 'AGENT_REPO_URL: "${AGENT_REPO_URL:-}"' in compose


def test_entrypoint_clones_only_into_an_empty_body(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    entrypoint = (root / "entrypoint.sh").read_text()
    assert 'if [ -d "$APP/.git" ]; then' in entrypoint
    # Emptiness is judged by files: a fresh named volume is seeded from the
    # image and inherits the empty mount directories it left behind.
    assert '-mindepth 1 -type f -print -quit' in entrypoint
    assert "refusing to touch it" in entrypoint
    assert "git clone --branch" in entrypoint
    assert subprocess.run(["bash", "-n", str(root / "entrypoint.sh")]).returncode == 0


def test_build_context_is_narrowed_to_the_copied_files(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    lines = [line for line in (root / ".dockerignore").read_text().splitlines()
             if line and not line.startswith("#")]
    assert lines[0] == "*"
    # Excluding a directory stops the walk into it, so each parent of a kept
    # file is re-admitted before its own contents are excluded again.
    assert lines.index("!deployment") < lines.index("deployment/*")
    assert lines.index("deployment/*") < lines.index("!deployment/capabilities.lock")
    assert "!entrypoint.sh" in lines
    assert "!supervisord.conf" in lines
    assert "!" + SYNC_PROGRAM in lines


def test_a_body_that_is_not_a_volume_is_refused(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    path = root / "deployment" / "runtime.json"
    runtime = json.loads(path.read_text())
    del runtime["volumes"]["agent_body"]
    path.write_text(json.dumps(runtime))
    payload = json.loads(_run(root, env, "doctor").stdout)
    assert [f for f in payload["findings"]
            if f["severity"] == "error" and "agent_body" in f["message"]]


def test_an_undeclared_repo_url_is_refused(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    path = root / "deployment" / "runtime.json"
    runtime = json.loads(path.read_text())
    agent = runtime["services"]["agent"]
    agent["required_env"] = [k for k in agent["required_env"] if k != "AGENT_REPO_URL"]
    path.write_text(json.dumps(runtime))
    payload = json.loads(_run(root, env, "doctor").stdout)
    assert [f for f in payload["findings"]
            if f["severity"] == "error" and "AGENT_REPO_URL" in f["message"]]


def test_next_names_the_repository_before_the_provider_steps(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    proc = _run(root, env, "next", "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert "AGENT_REPO_URL" in payload["provider_steps"][0]
    assert "GIT_DEPLOY_KEY_B64" in payload["provider_steps"][1]


def test_the_image_name_is_stated_once(tmp_path: Path) -> None:
    # A .env file lets the last assignment win, so a key restated lower down
    # silently overrides the value the heading above it explains.
    for profile in ("agent-box", "agent-box-checkout"):
        root, env = _project(tmp_path / profile, ("telegram",))
        _setup(root, env, profile)
        assignments = [line for line in (root / ".env.example").read_text().splitlines()
                       if line.startswith("AGENT_IMAGE=")]
        assert assignments == ["AGENT_IMAGE=agent-box"], (profile, assignments)


def test_env_example_ends_with_exactly_one_newline(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    body = (root / ".env.example").read_text()
    assert body.endswith("\n")
    assert not body.endswith("\n\n")


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, timeout=60, env=GIT_ENV, check=True)
    return proc.stdout.strip()


def _body_on_a_real_remote(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A checkout box whose body is a real clone of a remote a person also pushes to.

    The profile bakes the body's location into the program it renders, so the
    runtime says where the body is and `sync` renders a program pointed at it.
    Nothing about the program itself is adjusted for the test.
    """
    root, env = _project(tmp_path)
    _setup(root, env, "agent-box-checkout")
    body = tmp_path / "body"
    path = root / "deployment" / "runtime.json"
    runtime = json.loads(path.read_text())
    runtime["compiler"]["container"]["project_root"] = str(body)
    runtime["volumes"]["agent_body"]["mount"] = str(body)
    path.write_text(json.dumps(runtime, indent=2) + "\n")
    proc = _run(root, env, "sync")
    assert proc.returncode == 0, proc.stderr

    remote = tmp_path / "remote.git"
    desk = tmp_path / "desk"
    subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main",
                    str(remote)], check=True, timeout=60, env=GIT_ENV)
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(desk)],
                   check=True, timeout=60, env=GIT_ENV)
    (desk / "context").mkdir()
    (desk / "context" / "MISSION.md").write_text("the mission\n")
    _git(desk, "add", "-A")
    _git(desk, "commit", "--quiet", "-m", "the body as the workstation has it")
    _git(desk, "remote", "add", "origin", str(remote))
    _git(desk, "push", "--quiet", "-u", "origin", "main")
    subprocess.run(["git", "clone", "--quiet", "--branch", "main", str(remote),
                    str(body)], check=True, timeout=60, env=GIT_ENV)
    return root, body, desk


def _pass(root: Path, **settings: str) -> subprocess.CompletedProcess[str]:
    """One pass of the rendered program, run exactly as the box runs it."""
    return subprocess.run([str(root / SYNC_PROGRAM), "--once"], capture_output=True,
                          text=True, timeout=120, env={**GIT_ENV, **settings})


def test_the_profile_renders_a_sync_program_the_boot_path_runs_first(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram",))
    _setup(root, env, "agent-box-checkout")
    program = root / SYNC_PROGRAM
    # No project declares it: it is not a compiler.artifacts key at all.
    runtime = json.loads((root / "deployment" / "runtime.json").read_text())
    assert SYNC_PROGRAM not in runtime["compiler"]["artifacts"].values()
    assert program.is_file()
    assert subprocess.run(["bash", "-n", str(program)]).returncode == 0
    entrypoint = (root / "entrypoint.sh").read_text()
    boot = f"/opt/agent/{SYNC_PROGRAM} --once"
    # The self-healing requirement, as an ordering: a box that took in a broken
    # declaration has to take in the fix before anything reads the declaration.
    assert boot in entrypoint
    assert entrypoint.index('cd "$APP"') < entrypoint.index(boot)
    assert entrypoint.index(boot) < entrypoint.index("capabilities init")
    # A boot that could not reach the remote still boots.
    assert entrypoint[entrypoint.index(boot):].startswith(boot + " || echo")


def test_the_sync_program_reaches_for_nothing_but_git(tmp_path: Path) -> None:
    root, env = _project(tmp_path, ("telegram", "automations"))
    _setup(root, env, "agent-box-checkout")
    # Everything but the ownership marker the compiler stamps on what it writes.
    program = "\n".join(line for line in (root / SYNC_PROGRAM).read_text().splitlines()
                        if not line.startswith("# Generated by deployment sync"))
    # A sync that needed the project's configuration or its scheduler to be
    # healthy could not repair the box whose configuration is what broke.
    assert "capabilities" not in program
    assert "contextkit" not in program
    lock = [line.strip() for line in
            (root / "deployment" / "capabilities.lock").read_text().splitlines()
            if line.strip() and not line.startswith("#")]
    assert lock
    for name in lock:
        assert name not in program, name
    # The box runs whatever bash the base image ships, which is not the one this
    # workstation has.
    assert "declare -A" not in program
    assert "mapfile" not in program


def test_a_checkout_box_supervises_the_sync_ahead_of_everything_else(tmp_path: Path) -> None:
    # No capability service is embedded here at all: the Supervisor configuration
    # exists for the profile's own program.
    root, env = _project(tmp_path / "alone")
    _setup(root, env, "agent-box-checkout")
    config = (root / "supervisord.conf").read_text()
    assert f"command=/opt/agent/{SYNC_PROGRAM} --loop" in config
    assert "[program:body-sync]" in config
    assert "exec /usr/bin/supervisord" in (root / "entrypoint.sh").read_text()
    # What makes the runtime off switch work: a box told not to sync exits zero
    # and stays exited, while a program that actually died comes back. The start
    # window that bounds the second half is measured in its own test.
    block = config[config.index("[program:body-sync]"):]
    for setting in ("autorestart=unexpected", "exitcodes=0", "priority=5"):
        assert setting in block, setting
    # And a capability service, where there is one, starts behind it.
    other, env = _project(tmp_path / "with-service", ("telegram",))
    _setup(other, env, "agent-box-checkout")
    config = (other / "supervisord.conf").read_text()
    assert config.index("[program:body-sync]") < config.index("[program:telegram]")
    # The baked profile is left alone: nothing to supervise, no configuration.
    baked, env = _project(tmp_path / "baked")
    _setup(baked, env, "agent-box")
    assert not (baked / "supervisord.conf").exists()
    assert not (baked / SYNC_PROGRAM).exists()


def test_the_three_controls_are_declared_once_and_default_to_on(tmp_path: Path) -> None:
    root, env = _project(tmp_path / "checkout")
    _setup(root, env, "agent-box-checkout")
    agent = json.loads((root / "deployment" / "runtime.json").read_text())["services"]["agent"]
    compose = (root / "docker-compose.yaml").read_text()
    example = (root / ".env.example").read_text().splitlines()
    for key in SYNC_ENV:
        assert key in agent["optional_env"], key
        default = agent["environment_defaults"][key]
        # Compose passes only declared keys, so an undeclared one never reaches
        # the program and the box would run a window nobody chose.
        assert f'{key}: "${{{key}:-{default}}}"' in compose, key
        # A .env file lets the last assignment win, so a key restated lower down
        # silently overrides the value the heading above it explains.
        assert [line for line in example if line.startswith(key + "=")] == [f"{key}={default}"]
    assert agent["environment_defaults"]["AGENT_BODY_SYNC"] == "1"
    # The baked profile has no body to keep current and is told nothing about it.
    baked, env = _project(tmp_path / "baked")
    _setup(baked, env, "agent-box")
    for key in SYNC_ENV:
        assert key not in (baked / ".env.example").read_text(), key
        assert key not in (baked / "docker-compose.yaml").read_text(), key


def test_a_box_takes_in_what_was_pushed_and_sends_what_it_wrote(tmp_path: Path) -> None:
    root, body, desk = _body_on_a_real_remote(tmp_path)
    cloned_at = _git(body, "rev-parse", "HEAD")

    # What a person pushed after the box cloned reaches the box.
    (desk / "context" / "MISSION.md").write_text("the mission, corrected\n")
    _git(desk, "add", "-A")
    _git(desk, "commit", "--quiet", "-m", "the desk corrects the mission")
    _git(desk, "push", "--quiet", "origin", "main")
    proc = _pass(root, AGENT_BODY_SYNC_QUIET="0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _git(body, "rev-parse", "HEAD") != cloned_at
    assert (body / "context" / "MISSION.md").read_text() == "the mission, corrected\n"

    # What the box wrote and nobody committed deliberately leaves the box.
    (body / "context" / "NOTES.md").write_text("a note the box wrote\n")
    proc = _pass(root, AGENT_BODY_SYNC_QUIET="0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Body-Sync: agent-box-checkout" in _git(body, "log", "-1", "--format=%B")
    _git(desk, "fetch", "--quiet", "origin", "main")
    assert _git(desk, "cat-file", "-e", "origin/main:context/NOTES.md") == ""
    assert not _git(body, "status", "--porcelain")

    # And an operator who wants none of it says so once.
    (body / "context" / "QUIET.md").write_text("not to be sent\n")
    settled = _git(body, "rev-parse", "HEAD")
    proc = _pass(root, AGENT_BODY_SYNC="0", AGENT_BODY_SYNC_QUIET="0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "disabled by AGENT_BODY_SYNC=0" in proc.stdout
    assert _git(body, "rev-parse", "HEAD") == settled


def test_a_program_that_cannot_run_is_bounded_and_the_off_switch_is_not(tmp_path: Path) -> None:
    """Supervisor bounds a program by how long the process lives, and by nothing else.

    A process that exits inside the start window failed to start, so Supervisor
    backs off and says so. A process that exits after it is restarted at once,
    forever, while `status` still reads RUNNING. So the window is the only bound
    on a program that cannot run at all, and a deliberate no-op has to outlive it
    or the off switch is indistinguishable from a crash loop.
    """
    root, env = _project(tmp_path)
    _setup(root, env, "agent-box-checkout")
    block = (root / "supervisord.conf").read_text()
    block = block[block.index("[program:body-sync]"):]
    window = int(re.search(r"^startsecs=(\d+)$", block, re.M).group(1))
    assert window > 0

    # The off switch, measured rather than read: it exits zero, and it outlives
    # the window first, so Supervisor records EXITED rather than a start failure.
    started = time.monotonic()
    proc = subprocess.run([str(root / SYNC_PROGRAM), "--loop"], capture_output=True,
                          text=True, timeout=120, env={**GIT_ENV, "AGENT_BODY_SYNC": "0"})
    held = time.monotonic() - started
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "disabled by AGENT_BODY_SYNC=0" in proc.stdout
    assert held > window, held

    # The boot pass answers to no supervisor, so it is not made to wait.
    started = time.monotonic()
    proc = subprocess.run([str(root / SYNC_PROGRAM), "--once"], capture_output=True,
                          text=True, timeout=120, env={**GIT_ENV, "AGENT_BODY_SYNC": "0"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert time.monotonic() - started < window


def test_a_failing_pass_keeps_the_supervised_program_alive(tmp_path: Path) -> None:
    """A fault the box cannot fix is retried at the interval, not by respawning.

    Every refusal the program can reach is a failed pass rather than a failed
    process: it says so and waits for the next tick. That is what keeps the
    start window a bound on a program that cannot run at all, rather than one
    the everyday two-writer conflict trips over.
    """
    root, body, desk = _body_on_a_real_remote(tmp_path)
    # The everyday conflict: the box and a person edit the same file.
    (desk / "context" / "MISSION.md").write_text("the desk version\n")
    _git(desk, "add", "-A")
    _git(desk, "commit", "--quiet", "-m", "desk edits the mission")
    _git(desk, "push", "--quiet", "origin", "main")
    (body / "context" / "MISSION.md").write_text("the box version\n")
    _git(body, "add", "-A")
    _git(body, "commit", "--quiet", "-m", "the box edits the mission")

    proc = subprocess.Popen([str(root / SYNC_PROGRAM), "--loop"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={**GIT_ENV, "AGENT_BODY_SYNC_INTERVAL": "1",
                                 "AGENT_BODY_SYNC_QUIET": "0"})
    try:
        time.sleep(6)
        assert proc.poll() is None, "the supervised program exited on a conflicting pass"
    finally:
        proc.terminate()
        output = proc.communicate(timeout=30)[0]
    assert output.count("FATAL: rebasing onto origin/main conflicts") >= 2, output
    # And at the interval it was told, not as fast as it can fetch.
    assert output.count("FATAL: rebasing onto origin/main conflicts") <= 8, output
