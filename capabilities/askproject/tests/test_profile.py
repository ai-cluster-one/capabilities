"""The peer runs a profile callva-harness-runner finds: askproject's own folder,
then the library's machine folder, then its shipped set; used whole, then overridden.

Every assertion reads what the fake harness was actually handed by
callva-harness-runner - the claude command line, or the codex app-server
requests - rather than the source that asked for it. See _peer.py for how to
run the suite.
"""

import json

import pytest

from _peer import (Lab, effort_of, instructions_of, model_of, request, shipped_path,
                   thread_params, value)

READ_TEXT = "This is a READ-ONLY research query."
ACT_TEXT = "You are running with full write access inside THIS target project."
BRIDGE_TEXT = "via the askproject bridge"


@pytest.fixture()
def lab(tmp_path):
    return Lab(tmp_path)


def _ok(proc):
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- SHIPPED ------------------------------------------------------------------

def test_askproject_ships_no_profiles_of_its_own():
    from _peer import CAPABILITY
    assert not (CAPABILITY / "profiles").exists()


def test_the_library_ships_read_act_and_read_sandboxed():
    from callva.harness_runner import find_profile

    for name in ("read", "act", "read-sandboxed"):
        for harness, model in (("claude", "claude-opus-5-5"), ("codex", "gpt-6-sol")):
            profile = find_profile(name, harness, [])
            assert (profile.model, profile.effort, profile.timeout_seconds) == (
                model, "medium", 3600)


def test_read_on_claude_is_limited_by_instruction_only(lab):
    proc, result, launch = lab.ask("--engine", "claude")
    _ok(proc)
    argv = launch["argv"]
    assert (model_of(launch), effort_of(launch)) == ("claude-opus-5-5", "medium")
    assert value(argv, "--permission-mode") == "bypassPermissions"
    assert value(argv, "--setting-sources") == "project,local"
    for fence in ("--tools", "--allowedTools", "--disallowedTools", "--restricted",
                  "--settings", "--strict-mcp-config"):
        assert value(argv, fence) is None and fence not in argv, fence
    appended = instructions_of(launch)
    assert BRIDGE_TEXT in appended and READ_TEXT in appended
    assert appended.index(BRIDGE_TEXT) < appended.index(READ_TEXT)
    assert "--system-prompt" not in argv
    assert launch["env"]["CAPABILITIES_READ_ONLY"] == "1"
    assert result["profile"]["name"] == "read" and result["profile"]["source"] == "shipped"
    assert (result["mode"], result["model"], result["effort"]) == (
        "read", "claude-opus-5-5", "medium")


def test_read_on_codex_is_limited_by_instruction_only(lab):
    proc, result, launch = lab.ask("--engine", "codex")
    _ok(proc)
    params = thread_params(launch)
    assert (model_of(launch), effort_of(launch)) == ("gpt-6-sol", "medium")
    assert params["config"] == {"sandbox_mode": "danger-full-access"}
    assert params["approvalPolicy"] == "never"
    assert BRIDGE_TEXT in params["developerInstructions"]
    assert READ_TEXT in params["developerInstructions"]
    assert "baseInstructions" not in params
    assert launch["env"]["CAPABILITIES_READ_ONLY"] == "1"
    assert (result["profile"]["name"], result["model"], result["effort"]) == (
        "read", "gpt-6-sol", "medium")


def test_act_has_full_access_on_both_engines(lab):
    proc, result, launch = lab.ask("--engine", "claude", "--act")
    _ok(proc)
    argv = launch["argv"]
    assert value(argv, "--permission-mode") == "bypassPermissions"
    assert value(argv, "--setting-sources") is None
    assert ACT_TEXT in instructions_of(launch)
    assert "CAPABILITIES_READ_ONLY" not in launch["env"]
    assert (result["mode"], result["profile"]["name"]) == ("act", "act")
    assert (model_of(launch), effort_of(launch)) == ("claude-opus-5-5", "medium")

    proc, result, launch = lab.ask("--engine", "codex", "--act")
    _ok(proc)
    params = thread_params(launch)
    assert params["config"] == {"sandbox_mode": "danger-full-access"}
    assert params["approvalPolicy"] == "never"
    assert ACT_TEXT in params["developerInstructions"]
    assert "CAPABILITIES_READ_ONLY" not in launch["env"]
    assert (model_of(launch), effort_of(launch)) == ("gpt-6-sol", "medium")


def test_read_sandboxed_is_selectable_by_profile(lab):
    proc, result, launch = lab.ask("--profile", "read-sandboxed")
    _ok(proc)
    argv = launch["argv"]
    settings = json.loads(value(argv, "--settings"))
    assert settings["sandbox"]["enabled"] is True
    assert "Edit(./**)" in settings["permissions"]["deny"]
    assert settings["disableAllHooks"] is True
    assert value(argv, "--permission-mode") == "default"
    assert READ_TEXT in instructions_of(launch)
    assert launch["env"]["CAPABILITIES_READ_ONLY"] == "1"
    assert (result["mode"], result["profile"]["name"], result["profile"]["source"]) == (
        "read", "read-sandboxed", "shipped")

    proc, result, launch = lab.ask("--profile", "read-sandboxed", "--engine", "codex")
    _ok(proc)
    config = thread_params(launch)["config"]
    assert config["default_permissions"] == "read-sandboxed"
    assert config["permissions"]["read-sandboxed"]["extends"] == ":read-only"


def test_the_default_engine_is_claude(lab):
    proc, result, launch = lab.ask()
    _ok(proc)
    assert launch["harness"] == "claude" and result["engine"] == "claude"


# --- OPERATOR PROFILES --------------------------------------------------------

FAST = """
[claude]
model = "{model}"
effort = "low"
permission_mode = "plan"

[codex]
model = "{model}-codex"
effort = "low"
"""


def test_a_project_profile_is_picked_by_name(lab):
    path = lab.write_profile("project", "fast", FAST.format(model="m-project"))
    proc, result, launch = lab.ask("--profile", "fast")
    _ok(proc)
    assert (model_of(launch), effort_of(launch)) == ("m-project", "low")
    assert value(launch["argv"], "--permission-mode") == "plan"
    assert result["profile"] == {"name": "fast", "source": "folder", "path": str(path)}

    proc, result, launch = lab.ask("--profile", "fast", "--engine", "codex")
    _ok(proc)
    assert (model_of(launch), effort_of(launch)) == ("m-project-codex", "low")


def test_the_project_shadows_the_machine_which_shadows_the_shipped(lab):
    machine = lab.write_profile("machine", "fast", FAST.format(model="m-machine"))
    proc, result, launch = lab.ask("--profile", "fast")
    _ok(proc)
    assert model_of(launch) == "m-machine"
    assert result["profile"]["source"] == "machine"

    project = lab.write_profile("project", "fast", FAST.format(model="m-project"))
    proc, result, launch = lab.ask("--profile", "fast")
    _ok(proc)
    assert model_of(launch) == "m-project"

    listing = json.loads(lab.run("profiles").stdout)
    by_name = {entry["name"]: entry for entry in listing["profiles"]}
    assert by_name["fast"] == {"name": "fast", "source": "folder", "path": str(project),
                               "harnesses": ["claude", "codex"], "shadows": [str(machine)]}
    for name in ("read", "act", "read-sandboxed"):
        assert by_name[name]["source"] == "shipped"
        assert by_name[name]["harnesses"] == ["claude", "codex"]
    assert [entry["source"] for entry in listing["lookup"]] == ["folder", "machine", "shipped"]
    assert listing["lookup"][1]["dir"] == str(lab.config / "callva-harness-runner" / "profiles")


def test_askproject_has_no_machine_folder_of_its_own(lab):
    lab.write_profile("askproject/profiles", "old", FAST.format(model="m-old"))
    proc, result, launch = lab.ask("--profile", "old")
    assert proc.returncode == 1
    assert "no profile named 'old'" in result["error"]
    assert launch is None
    names = [e["name"] for e in json.loads(lab.run("profiles").stdout)["profiles"]]
    assert "old" not in names


def test_a_shipped_name_is_replaced_whole_by_an_operator_file(lab):
    # A project `read` with no env_set and no setting_sources: nothing of the
    # shipped `read` is merged into it.
    path = lab.write_profile("project", "read", """
[claude]
model = "m-own-read"
""")
    proc, result, launch = lab.ask()
    _ok(proc)
    assert model_of(launch) == "m-own-read"
    assert effort_of(launch) is None
    assert value(launch["argv"], "--setting-sources") is None
    assert value(launch["argv"], "--permission-mode") is None
    assert "CAPABILITIES_READ_ONLY" not in launch["env"]
    assert READ_TEXT not in instructions_of(launch)
    assert result["profile"] == {"name": "read", "source": "folder", "path": str(path)}

    listing = json.loads(lab.run("profiles").stdout)
    read = next(e for e in listing["profiles"] if e["name"] == "read")
    assert (read["source"], read["harnesses"]) == ("folder", ["claude"])
    assert read["shadows"] == [str(shipped_path("read"))]


def test_profiles_show_prints_the_resolved_file(lab):
    proc = lab.run("profiles", "show", "read")
    _ok(proc)
    shipped = shipped_path("read")
    assert proc.stdout == f"# shipped: {shipped}\n" + shipped.read_text()

    body = FAST.format(model="m-project")
    path = lab.write_profile("project", "read", body)
    assert lab.run("profiles", "show", "read").stdout == f"# folder: {path}\n" + body

    missing = lab.run("profiles", "show", "nope")
    assert missing.returncode == 3
    assert json.loads(missing.stderr)["error"]["code"] == "not_found"


@pytest.mark.parametrize("body, said", [
    ('[claude]\nfence = "read"\n', "'fence' was removed in 0.2.0"),
    ('[claude]\nengine = "claude"\n', "'engine' was renamed 'harness'"),
    ('[claude]\ncodex_config = { sandbox_mode = "read-only" }\n',
     "profile.codex_config: claude cannot honour it"),
    ('[claude]\nmodel = 5\n', "profile.model: expected string or null"),
    ('model = "x"\n', "is not a harness table"),
])
def test_a_profile_the_library_refuses_is_reported_and_nothing_runs(lab, body, said):
    lab.write_profile("project", "broken", body)
    proc, result, launch = lab.ask("--profile", "broken")
    assert proc.returncode == 1
    assert "refused by callva-harness-runner" in result["error"]
    assert said in result["error"]
    assert launch is None


def test_a_profile_without_the_engine_table_is_refused(lab):
    lab.write_profile("project", "claudeonly", '[claude]\n')
    proc, result, launch = lab.ask("--profile", "claudeonly", "--engine", "codex")
    assert proc.returncode == 1
    assert "has no [codex] table" in result["error"]
    assert launch is None


@pytest.mark.parametrize("name", ["nope", "../read", "a/b"])
def test_an_unknown_or_unsafe_profile_name_runs_nothing(lab, name):
    proc, result, launch = lab.ask("--profile", name)
    assert proc.returncode == 1
    assert "profile" in result["error"]
    assert launch is None


# --- OVERRIDES ----------------------------------------------------------------

def test_model_and_effort_resolve_flag_then_env_local_then_env_then_process(lab):
    layers = [
        ({"env": {"ASKPROJECT_MODEL": "m-process", "ASKPROJECT_EFFORT": "low"}},
         ("m-process", "low")),
        ({"env": {"ASKPROJECT_MODEL": "m-process", "ASKPROJECT_EFFORT": "low"},
          ".env": {"ASKPROJECT_MODEL": "m-dotenv", "ASKPROJECT_EFFORT": "high"}},
         ("m-dotenv", "high")),
        ({"env": {"ASKPROJECT_MODEL": "m-process", "ASKPROJECT_EFFORT": "low"},
          ".env": {"ASKPROJECT_MODEL": "m-dotenv", "ASKPROJECT_EFFORT": "high"},
          ".env.local": {"ASKPROJECT_MODEL": "m-local", "ASKPROJECT_EFFORT": "xhigh"}},
         ("m-local", "xhigh")),
    ]
    for engine in ("claude", "codex"):
        for chain, expected in layers:
            lab.dotenv(".env", chain.get(".env"))
            lab.dotenv(".env.local", chain.get(".env.local"))
            env = {**chain["env"], "ASKPROJECT_ENGINE": engine}
            proc, _, launch = lab.ask(env=env)
            _ok(proc)
            assert launch["harness"] == engine
            assert (model_of(launch), effort_of(launch)) == expected, (engine, chain)
        proc, _, launch = lab.ask("--model", "m-flag", "--effort", "max",
                                  env={**layers[2][0]["env"], "ASKPROJECT_ENGINE": engine})
        _ok(proc)
        assert (model_of(launch), effort_of(launch)) == ("m-flag", "max")


def test_engine_resolves_flag_then_env_local_then_env_then_process(lab):
    cases = [
        ((), {"ASKPROJECT_ENGINE": "codex"}, None, None, "codex"),
        ((), {"ASKPROJECT_ENGINE": "codex"}, {"ASKPROJECT_ENGINE": "claude"}, None, "claude"),
        ((), {"ASKPROJECT_ENGINE": "claude"}, {"ASKPROJECT_ENGINE": "claude"},
         {"ASKPROJECT_ENGINE": "codex"}, "codex"),
        (("--engine", "claude"), {}, None, {"ASKPROJECT_ENGINE": "codex"}, "claude"),
    ]
    for flags, env, dotenv, local, expected in cases:
        lab.dotenv(".env", dotenv)
        lab.dotenv(".env.local", local)
        proc, _, launch = lab.ask(*flags, env=env)
        _ok(proc)
        assert launch["harness"] == expected, (flags, env, dotenv, local)


def test_timeout_resolves_flag_then_env_local_then_env_then_process_then_profile(lab):
    # The peer works for three seconds: one second kills it, thirty lets it finish.
    slow = {"PEER_SLEEP": "3"}
    cases = [
        ((), {"ASKPROJECT_TIMEOUT": "1"}, None, None, "timeout"),
        ((), {"ASKPROJECT_TIMEOUT": "30"}, {"ASKPROJECT_TIMEOUT": "1"}, None, "timeout"),
        ((), {"ASKPROJECT_TIMEOUT": "1"}, {"ASKPROJECT_TIMEOUT": "1"},
         {"ASKPROJECT_TIMEOUT": "30"}, "ok"),
        (("--timeout", "30"), {"ASKPROJECT_TIMEOUT": "1"}, None, None, "ok"),
        (("--timeout", "1"), {"ASKPROJECT_TIMEOUT": "30"}, None, None, "timeout"),
        ((), {"ASKPROJECT_TIMEOUT": "soon"}, None, None, "ok"),
    ]
    for flags, env, dotenv, local, expected in cases:
        lab.dotenv(".env", dotenv)
        lab.dotenv(".env.local", local)
        proc, result, _ = lab.ask("--engine", "codex", *flags, env={**slow, **env})
        if expected == "timeout":
            assert proc.returncode == 1, (flags, env)
            assert "peer timed out after 1s" in result["error"]
        else:
            _ok(proc)

    # With nothing set, the profile's own timeout decides.
    lab.dotenv(".env", None)
    lab.dotenv(".env.local", None)
    lab.write_profile("project", "short", '[codex]\ntimeout_seconds = 1\n')
    proc, result, _ = lab.ask("--engine", "codex", "--profile", "short", env=slow)
    assert proc.returncode == 1
    assert "peer timed out after 1s" in result["error"]


def test_overrides_apply_over_an_operator_profile(lab):
    lab.write_profile("project", "fast", FAST.format(model="m-project"))
    proc, _, launch = lab.ask("--profile", "fast", "--model", "m-flag",
                              env={"ASKPROJECT_EFFORT": "high"})
    _ok(proc)
    assert (model_of(launch), effort_of(launch)) == ("m-flag", "high")
    assert value(launch["argv"], "--permission-mode") == "plan"


def test_a_non_positive_timeout_flag_is_refused(lab):
    proc, result, launch = lab.ask("--timeout", "0")
    assert proc.returncode == 1
    assert "--timeout" in result["error"]
    assert launch is None


def test_a_model_written_for_one_engine_never_reaches_the_other(lab):
    proc, _, launch = lab.ask("--engine", "codex", env={
        "ASKPROJECT_MODEL": "claude-sonnet-5", "ASKPROJECT_EFFORT": "high"})
    _ok(proc)
    assert (model_of(launch), effort_of(launch)) == ("gpt-6-sol", "medium")

    codex_chain = {"ASKPROJECT_ENGINE": "codex", "ASKPROJECT_MODEL": "gpt-6-luna",
                   "ASKPROJECT_EFFORT": "low"}
    lab.dotenv(".env", codex_chain)
    proc, _, launch = lab.ask("--engine", "claude")
    _ok(proc)
    assert (model_of(launch), effort_of(launch)) == ("claude-opus-5-5", "medium")

    proc, _, launch = lab.ask()
    _ok(proc)
    assert (launch["harness"], model_of(launch), effort_of(launch)) == (
        "codex", "gpt-6-luna", "low")


# --- resume -------------------------------------------------------------------

def test_resume_keeps_engine_mode_and_profile(lab):
    proc, first, _ = lab.ask()
    _ok(proc)
    proc, result, launch = lab.ask("-c")
    _ok(proc)
    assert launch["harness"] == "claude"
    assert value(launch["argv"], "--resume") == first["session_id"]
    assert value(launch["argv"], "--session-id") is None
    assert READ_TEXT in instructions_of(launch)
    assert (result["resumed"], result["mode"], result["profile"]["name"]) == (
        True, "read", "read")

    # A codex act thread resumed with no --engine stays on codex, in act mode.
    proc, _, _ = lab.ask("--engine", "codex", "--act")
    _ok(proc)
    proc, result, launch = lab.ask("-c", "--act", env={"ASKPROJECT_MODEL": "claude-sonnet-5"})
    _ok(proc)
    assert request(launch, "thread/resume")["threadId"] == "codex-thread"
    assert ACT_TEXT in instructions_of(launch)
    assert model_of(launch) == "gpt-6-sol"
    assert (result["engine"], result["mode"], result["resumed"]) == ("codex", "act", True)

    # The same act thread continued with --read takes the read profile.
    proc, result, launch = lab.ask("-c", "--read")
    _ok(proc)
    assert READ_TEXT in instructions_of(launch)
    assert launch["env"]["CAPABILITIES_READ_ONLY"] == "1"
    assert (result["mode"], result["profile"]["name"]) == ("read", "read")


def test_resume_keeps_an_operator_profile(lab):
    lab.write_profile("project", "fast", FAST.format(model="m-project"))
    proc, _, _ = lab.ask("--profile", "fast")
    _ok(proc)
    proc, result, launch = lab.ask("-c")
    _ok(proc)
    assert model_of(launch) == "m-project"
    assert result["profile"]["name"] == "fast"


# --- KEPT ---------------------------------------------------------------------

def test_a_read_peer_that_writes_is_reported_as_a_failure(lab):
    proc, result, _ = lab.ask(env={"PEER_MUTATE": "1"})
    assert proc.returncode == 1
    assert result["ok"] is False
    assert "READ MODE MUTATED THE TARGET" in result["error"]
    assert {"path": "written-by-peer.txt", "status": "??"} in result["git"]["files_changed"]


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_the_peer_environment_belongs_to_the_target(lab, engine):
    proc, _, launch = lab.ask("--engine", engine, env={
        "TELEGRAM_CHAT_ID": "caller-chat", "TELEGRAM_REAL_TELEGRAM": "/opt/telegram",
        "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "caller-session"})
    _ok(proc)
    env = launch["env"]
    assert launch["cwd"] in (str(lab.target), str(lab.target.resolve()))
    assert env["CLAUDE_PROJECT_DIR"] == str(lab.target)
    assert env["TELEGRAM_REAL_TELEGRAM"] == "/opt/telegram"
    for gone in ("TELEGRAM_CHAT_ID", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
        assert gone not in env, gone


def test_the_answer_carries_its_traceability(lab):
    proc, result, launch = lab.ask()
    _ok(proc)
    assert set(result) == {
        "ok", "error", "target", "engine", "mode", "profile", "model", "effort",
        "session_id", "peer_name", "resumed", "answer", "cost_usd", "duration_ms",
        "num_turns", "tokens", "git", "compiled_context", "permission_denials"}
    assert result["answer"] == "PEER ANSWER"
    assert result["session_id"] == value(launch["argv"], "--session-id")
    assert result["peer_name"] == f"target-{result['session_id'][:8]}"
    assert value(launch["argv"], "--name") == result["peer_name"]
    assert result["cost_usd"] == 0.02
    assert (result["duration_ms"], result["num_turns"]) == (5, 1)
    assert result["tokens"] == {"input": 4, "output": 3, "cache_read": 2, "cache_creation": 1}
    assert result["permission_denials"] == [{"tool_name": "Bash"}]
    assert result["git"]["repo"] is True
    assert result["git"]["head_before"] == result["git"]["head_after"]
    assert result["git"]["files_changed"] == []
    assert result["compiled_context"] is None

    targets = lab.run("targets", "--json")
    _ok(targets)
    [entry] = json.loads(targets.stdout)["targets"]
    assert entry["target"] == str(lab.target)
    assert (entry["last_session_id"], entry["engine"], entry["model"], entry["effort"],
            entry["profile"]) == (result["session_id"], "claude", "claude-opus-5-5",
                                  "medium", "read")

    proc, result, _ = lab.ask("--engine", "codex")
    _ok(proc)
    assert result["session_id"] == "codex-thread"
    assert result["cost_usd"] is None
    assert result["tokens"] == {"input": 3, "output": 2, "cache_read": 1, "cache_creation": None}
    assert result["permission_denials"] == []
