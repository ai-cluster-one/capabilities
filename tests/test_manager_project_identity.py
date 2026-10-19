"""A project's id: read from the envelope's project.json alone, minted there by
`capabilities init`, and carried over once from a ContextKit binding that holds
one where project.json declares none. ContextKit is never asked for it."""

import json
import os
import subprocess
import sys
from pathlib import Path


MANAGER = Path(__file__).parents[1] / "bin" / "capabilities"
TASKS = Path(__file__).parents[1] / "capabilities" / "tasks" / "bin" / "tasks"


def _project(tmp_path: Path, *, bound: bool, contextkit_id: str | None = None,
             copy: dict | None = None) -> Path:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    if bound:
        config = project / ".contextkit" / "config.toml"
        config.parent.mkdir(parents=True)
        body = 'version = 1\ntype = "agent-project"\n'
        if contextkit_id:
            body += f'\n[identity]\nid = "{contextkit_id}"\n'
        config.write_text(body)
    if copy is not None:
        (project / "capabilities").mkdir()
        (project / "capabilities" / "project.json").write_text(json.dumps(copy))
    return project


def _contextkit(tmp_path: Path) -> Path:
    """A stand-in `contextkit` that answers the envelope question, logs every
    call, and fails anything else, so a call for the id would show."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "contextkit-calls.log"
    script = bin_dir / "contextkit"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'if [ "$1" = path ]; then printf "%s/capabilities\\n" "$(pwd)"; exit 0; fi\n'
        'echo "unexpected call" >&2; exit 2\n')
    script.chmod(0o755)
    return log


def _in_files_mode(project: Path) -> None:
    """An existing agent in files mode: a new one would start in the store."""
    (project / "capabilities").mkdir(exist_ok=True)
    (project / "capabilities" / "settings.json").write_text('{"capabilities": {}}\n')


def _env(tmp_path: Path, project: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "CAPABILITIES_HOME": str(tmp_path / "registry"),
        "CLAUDE_PROJECT_DIR": str(project),
        "PATH": f"{tmp_path / 'fakebin'}{os.pathsep}{env.get('PATH', '')}",
    })
    for key in ("CAPABILITIES_PROJECT_ENVELOPE", "CAPABILITIES_PROJECT_ENVELOPE_ROOT",
                "CAPABILITIES_PROJECT_ID", "CAPABILITIES_PROJECT_ID_ROOT",
                "CAPABILITIES_READ_ONLY"):
        env.pop(key, None)
    return env


def _run(tmp_path: Path, project: Path, *args: str,
         env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MANAGER), *args], cwd=project,
        env=env or _env(tmp_path, project), text=True, capture_output=True, timeout=60)


def _json(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _error(result: subprocess.CompletedProcess[str]) -> dict:
    line = next(line for line in reversed(result.stderr.splitlines())
                if line.lstrip().startswith("{"))
    return json.loads(line)["error"]


def _copy(project: Path) -> dict:
    return json.loads((project / "capabilities" / "project.json").read_text())


def _identity_calls(log: Path) -> int:
    if not log.exists():
        return 0
    return sum(1 for line in log.read_text().splitlines() if not line.startswith("path "))


def _probe(tmp_path: Path, project: Path, env: dict | None = None) -> dict:
    """What a capability (tasks) resolves for read and for write."""
    probe = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_loader('cap', loader=None)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        f"mod.__file__ = {str(TASKS)!r}\n"
        f"src = open({str(TASKS)!r}).read().replace('if __name__ == \"__main__\":\\n    main()\\n', '')\n"
        "exec(compile(src, mod.__file__, 'exec'), mod.__dict__)\n"
        "print(json.dumps({'read': mod._project_id(),"
        " 'write': mod._project_id(write=True, refuse=False),"
        " 'state': mod._project_id_state()['state']}))\n")
    result = subprocess.run([sys.executable, "-c", probe], cwd=project,
                            env=env or _env(tmp_path, project),
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_init_mints_an_id_offline_in_a_plain_project(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=False)
    _in_files_mode(project)
    log = _contextkit(tmp_path)

    result = _json(_run(tmp_path, project, "init"))
    minted = _copy(project)["id"]
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]

    assert minted.startswith("prj_") and len(minted) == 16
    assert identity == {"state": "declared", "id": minted, "project_json_id": minted,
                        "bound": False, "contextkit_id": None}
    # No database resolves here, so the agent row is reported, not written.
    assert result["agent"] == {"registered": False, "reason": "store_not_configured"}
    assert not log.exists()


def test_init_carries_a_contextkit_held_id_into_a_new_identity(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000a1")
    _in_files_mode(project)
    log = _contextkit(tmp_path)

    _json(_run(tmp_path, project, "init"))

    assert _copy(project)["id"] == "prj_0000000000a1"
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]
    assert identity["state"] == "declared" and identity["id"] == "prj_0000000000a1"
    assert _identity_calls(log) == 0


def test_init_mints_for_a_bound_project_whose_binding_holds_no_id(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True)
    _in_files_mode(project)
    log = _contextkit(tmp_path)

    _json(_run(tmp_path, project, "init"))

    assert _copy(project)["id"].startswith("prj_")
    assert _identity_calls(log) == 0


def test_init_fills_a_copy_that_has_no_id_and_rewrites_none(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000a2",
                       copy={"schema": "capabilities.project.v1", "slug": "p"})
    _contextkit(tmp_path)

    _json(_run(tmp_path, project, "init"))
    assert _copy(project)["id"] == "prj_0000000000a2"

    (project / "capabilities" / "project.json").write_text(json.dumps(
        {"schema": "capabilities.project.v1", "slug": "p", "id": "prj_00000000ffff"}))
    _run(tmp_path, project, "init")
    assert _copy(project)["id"] == "prj_00000000ffff"


def test_project_json_is_the_id_even_where_contextkit_holds_another(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000c1",
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000c2"})
    log = _contextkit(tmp_path)

    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]
    doctor = json.loads(_run(tmp_path, project, "doctor").stdout)
    relabel = _json(_run(tmp_path, project, "relabel", "other"))

    assert identity == {"state": "declared", "id": "prj_0000000000c2",
                        "project_json_id": "prj_0000000000c2",
                        "bound": True, "contextkit_id": "prj_0000000000c2"}
    assert not [f for f in doctor["findings"] if "project id" in f]
    assert relabel["id"] == "prj_0000000000c2" and _copy(project)["slug"] == "other"
    assert _probe(tmp_path, project)["write"] == "prj_0000000000c2"
    assert _identity_calls(log) == 0


def test_an_id_only_in_the_binding_is_copied_into_project_json_once(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000d1")
    log = _contextkit(tmp_path)

    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]

    assert (identity["state"], identity["id"]) == ("declared", "prj_0000000000d1")
    assert _copy(project) == {"schema": "capabilities.project.v1", "slug": "project",
                              "id": "prj_0000000000d1"}
    # Once in project.json, the binding is no longer consulted.
    config = project / ".contextkit" / "config.toml"
    config.write_text(config.read_text().replace("prj_0000000000d1", "prj_0000000000d9"))
    again = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]
    assert again["id"] == "prj_0000000000d1"
    assert _identity_calls(log) == 0


def test_a_capability_gets_the_binding_held_id_copied_through_the_manager(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000d2")
    log = _contextkit(tmp_path)
    env = _env(tmp_path, project)
    env["CAPABILITIES_MANAGER_BIN"] = str(MANAGER)

    answer = _probe(tmp_path, project, env)

    assert answer == {"read": "prj_0000000000d2", "write": "prj_0000000000d2",
                      "state": "declared"}
    assert _copy(project)["id"] == "prj_0000000000d2"
    assert _identity_calls(log) == 0


def test_the_read_only_switch_answers_the_binding_held_id_without_writing(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000d3")
    _contextkit(tmp_path)
    env = _env(tmp_path, project)
    env["CAPABILITIES_READ_ONLY"] = "1"

    identity = _json(_run(tmp_path, project, "path", "--json", "--identity",
                          env=env))["project_identity"]

    assert identity["id"] == "prj_0000000000d3"
    assert not (project / "capabilities" / "project.json").exists()


def test_a_project_without_any_id_is_undeclared(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=False,
                       copy={"schema": "capabilities.project.v1", "slug": "p"})

    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]
    relabel = _run(tmp_path, project, "relabel", "other")

    assert identity == {"state": "undeclared", "id": None, "project_json_id": None,
                        "bound": False, "contextkit_id": None}
    assert relabel.returncode == 6 and _error(relabel)["code"] == "no_project_identity"


def test_a_capability_reads_project_json_without_the_manager(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000e1",
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000e1"})
    _contextkit(tmp_path)
    # The envelope record is written by this manager call.
    _json(_run(tmp_path, project, "path", "--json"))
    env = _env(tmp_path, project)
    # A manager that cannot start proves project.json, not the manager, answered.
    env["CAPABILITIES_MANAGER_BIN"] = str(tmp_path / "no-manager-here")

    assert _probe(tmp_path, project, env) == {
        "read": "prj_0000000000e1", "write": "prj_0000000000e1", "state": "declared"}


def test_a_handed_down_id_is_used_and_one_project_json_contradicts_refuses_writes(
        tmp_path: Path) -> None:
    project = _project(tmp_path, bound=False,
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000f2"})
    env = _env(tmp_path, project)
    env.update({"CAPABILITIES_PROJECT_ID": "prj_0000000000f2",
                "CAPABILITIES_PROJECT_ID_ROOT": str(project)})

    identity = _json(_run(tmp_path, project, "path", "--json", "--identity",
                          env=env))["project_identity"]
    assert (identity["state"], identity["id"]) == ("handed", "prj_0000000000f2")

    env["CAPABILITIES_PROJECT_ID"] = "prj_0000000000f9"
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity",
                          env=env))["project_identity"]
    assert identity == {"state": "mismatch", "id": "prj_0000000000f2",
                        "handed_id": "prj_0000000000f9",
                        "project_json_id": "prj_0000000000f2",
                        "bound": False, "contextkit_id": None}
    relabel = _run(tmp_path, project, "relabel", "other", env=env)
    assert relabel.returncode == 6
    error = _error(relabel)
    assert error["code"] == "project_id_mismatch"
    assert "prj_0000000000f2" in error["message"] and "prj_0000000000f9" in error["message"]
    assert _probe(tmp_path, project, env)["write"] is None

    # A handoff scoped to another root is passed over.
    env["CAPABILITIES_PROJECT_ID_ROOT"] = str(tmp_path / "elsewhere")
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity",
                          env=env))["project_identity"]
    assert (identity["state"], identity["id"]) == ("declared", "prj_0000000000f2")


def test_resolving_the_envelope_alone_hands_the_id_down_beside_it(tmp_path: Path) -> None:
    """A capability that never asks for the id still hands it to its children
    with the envelope, where a write may stamp it."""
    probe = (
        "import importlib.util, json, os\n"
        "spec = importlib.util.spec_from_loader('cap', loader=None)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        f"mod.__file__ = {str(TASKS)!r}\n"
        f"src = open({str(TASKS)!r}).read().replace('if __name__ == \"__main__\":\\n    main()\\n', '')\n"
        "exec(compile(src, mod.__file__, 'exec'), mod.__dict__)\n"
        "mod._envelope_home(mod._project_root())\n"
        "print(json.dumps({k: os.environ.get(k) for k in ('CAPABILITIES_PROJECT_ID', 'CAPABILITIES_PROJECT_ID_ROOT')}))\n")
    project = _project(tmp_path, bound=False,
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000f3"})
    result = subprocess.run([sys.executable, "-c", probe], cwd=project,
                            env=_env(tmp_path, project), text=True,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "CAPABILITIES_PROJECT_ID": "prj_0000000000f3",
        "CAPABILITIES_PROJECT_ID_ROOT": str(project.resolve())}


BASE_CONTRACT = Path(__file__).parent / "fixtures" / "contract_identity_base.py"


def _base_contract_read(tmp_path: Path, project: Path) -> dict:
    """The released contract's identity read, run against this manager the way
    a capability stamped with it runs after a manager self-update."""
    probe = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "ns = {'__name__': 'base_contract'}\n"
        f"exec(compile(open({str(BASE_CONTRACT)!r}).read(), 'base_contract', 'exec'), ns)\n"
        "root = Path(sys.argv[1]).resolve()\n"
        "ns['_project_capabilities_dir'] = lambda r: r / 'capabilities'\n"
        "def die(code, slug, message, hint=None):\n"
        "    raise SystemExit(json.dumps({'exit': code, 'code': slug}))\n"
        "ns['_die'] = die\n"
        "state = ns['_project_id_state_at'](root)\n"
        "refusal = ns['_identity_refusal'](state)\n"
        "print(json.dumps({'state': state['state'], 'id': state['id'],"
        " 'write': None if refusal else state['id'],"
        " 'refusal': refusal and refusal.get('code')}))\n")
    env = _env(tmp_path, project)
    env["CAPABILITIES_MANAGER_BIN"] = str(MANAGER)
    result = subprocess.run([sys.executable, "-c", probe, str(project)], cwd=project,
                            env=env, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_the_released_contract_still_resolves_and_writes_in_a_bound_project(
        tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000b1",
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000b1"})
    _contextkit(tmp_path)

    assert _base_contract_read(tmp_path, project) == {
        "state": "adopted", "id": "prj_0000000000b1", "write": "prj_0000000000b1",
        "refusal": None}


def test_the_released_contract_still_resolves_an_id_only_the_binding_holds(
        tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000b2")
    _contextkit(tmp_path)

    assert _base_contract_read(tmp_path, project)["write"] == "prj_0000000000b2"
    assert _copy(project)["id"] == "prj_0000000000b2"


def test_the_released_contract_still_resolves_and_writes_in_a_plain_project(
        tmp_path: Path) -> None:
    project = _project(tmp_path, bound=False,
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000a1"})

    assert _base_contract_read(tmp_path, project) == {
        "state": "unbound", "id": "prj_0000000000a1", "write": "prj_0000000000a1",
        "refusal": None}


def test_a_new_agent_without_a_node_name_is_refused_before_anything_is_written(
        tmp_path: Path) -> None:
    project = _project(tmp_path, bound=False)
    _contextkit(tmp_path)
    env = _env(tmp_path, project)
    env.pop("AGENT_NODE_NAME", None)

    result = _run(tmp_path, project, "init", env=env)

    assert result.returncode == 6
    assert _error(result)["code"] == "node_unnamed"
    assert not (project / "capabilities").exists()


def test_a_new_agent_without_a_store_is_refused_before_anything_is_written(
        tmp_path: Path) -> None:
    project = _project(tmp_path, bound=False)
    _contextkit(tmp_path)
    env = _env(tmp_path, project)
    env["AGENT_NODE_NAME"] = "test-node"
    for key in [key for key in env if key.startswith("AGENTKIT_DB_")]:
        env.pop(key)

    result = _run(tmp_path, project, "init", env=env)

    assert result.returncode == 6
    assert _error(result)["code"] == "store_not_configured"
    assert not (project / "capabilities").exists()
