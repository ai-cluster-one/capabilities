"""A project's id: CAPABILITIES_PROJECT_ID, the environment's or else the one
the project root's .env.local (else .env) sets, minted there by `capabilities
init`, and moved there once from an older project.json or a ContextKit binding
that holds one. ContextKit is never asked for it."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


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


def _bound(project: Path) -> str | None:
    """The id the project's .env.local binds it to, or None."""
    try:
        lines = (project / ".env.local").read_text().splitlines()
    except FileNotFoundError:
        return None
    found = [line.split("=", 1)[1] for line in lines
             if line.startswith("CAPABILITIES_PROJECT_ID=")]
    assert len(found) <= 1, lines
    return found[0] if found else None


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
    minted = _bound(project)
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]

    assert len(minted) == 12 and all(c in "0123456789abcdef" for c in minted)
    # The id is the binding: init writes no project.json.
    assert not (project / "capabilities" / "project.json").exists()
    assert identity == {"state": "declared", "id": minted,
                        "bound": False, "contextkit_id": None}
    # No database resolves here, so the agent row is reported, not written.
    assert result["agent"] == {"registered": False, "reason": "store_not_configured"}
    assert not log.exists()


def test_init_carries_a_contextkit_held_id_into_a_new_identity(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000a1")
    _in_files_mode(project)
    log = _contextkit(tmp_path)

    _json(_run(tmp_path, project, "init"))

    assert _bound(project) == "prj_0000000000a1"
    assert not (project / "capabilities" / "project.json").exists()
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]
    assert identity["state"] == "declared" and identity["id"] == "prj_0000000000a1"
    assert _identity_calls(log) == 0


def test_init_mints_for_a_bound_project_whose_binding_holds_no_id(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True)
    _in_files_mode(project)
    log = _contextkit(tmp_path)

    _json(_run(tmp_path, project, "init"))

    bound = _bound(project)
    assert len(bound) == 12 and all(c in "0123456789abcdef" for c in bound)
    assert _identity_calls(log) == 0


def test_init_fills_a_copy_that_has_no_id_and_rewrites_none(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000a2",
                       copy={"schema": "capabilities.project.v1", "slug": "p"})
    _contextkit(tmp_path)

    _json(_run(tmp_path, project, "init"))
    assert _bound(project) == "prj_0000000000a2"
    assert _copy(project) == {"schema": "capabilities.project.v2", "slug": "p"}

    (project / "capabilities" / "project.json").write_text(json.dumps(
        {"schema": "capabilities.project.v1", "slug": "p", "id": "prj_00000000ffff"}))
    _json(_run(tmp_path, project, "init"))
    assert _bound(project) == "prj_0000000000a2"
    assert _copy(project) == {"schema": "capabilities.project.v2", "slug": "p"}


def test_a_project_json_id_moves_to_env_local_even_where_contextkit_holds_another(
        tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000c1",
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000c2"})
    log = _contextkit(tmp_path)

    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]
    doctor = json.loads(_run(tmp_path, project, "doctor").stdout)
    relabel = _json(_run(tmp_path, project, "relabel", "other"))

    assert identity == {"state": "declared", "id": "prj_0000000000c2",
                        "bound": True, "contextkit_id": "prj_0000000000c2"}
    assert _bound(project) == "prj_0000000000c2"
    assert not [f for f in doctor["findings"] if "project id" in f]
    assert relabel["id"] == "prj_0000000000c2"
    assert _copy(project) == {"schema": "capabilities.project.v2", "slug": "other"}
    assert _probe(tmp_path, project)["write"] == "prj_0000000000c2"
    assert _identity_calls(log) == 0


def test_an_id_only_in_the_binding_is_moved_into_env_local_once(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000d1")
    log = _contextkit(tmp_path)

    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]

    assert (identity["state"], identity["id"]) == ("declared", "prj_0000000000d1")
    assert _bound(project) == "prj_0000000000d1"
    assert not (project / "capabilities" / "project.json").exists()
    # Once in .env.local, the binding is no longer consulted.
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
    assert _bound(project) == "prj_0000000000d2"
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
    assert not (project / ".env.local").exists()


def test_a_project_it_cannot_write_is_answered_its_id_unmoved(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root writes any folder")
    project = _project(tmp_path, bound=False,
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000e4"})
    for path in (project, project / "capabilities", project / "capabilities" / "project.json"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    try:
        identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))
    finally:
        for path in (project, project / "capabilities", project / "capabilities" / "project.json"):
            path.chmod(0o755 if path.is_dir() else 0o644)

    assert identity["project_identity"]["id"] == "prj_0000000000e4"
    assert _copy(project)["id"] == "prj_0000000000e4"
    assert not (project / ".env.local").exists()


def test_a_project_without_any_id_is_undeclared(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=False,
                       copy={"schema": "capabilities.project.v1", "slug": "p"})

    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]
    relabel = _run(tmp_path, project, "relabel", "other")

    assert identity == {"state": "undeclared", "id": None,
                        "bound": False, "contextkit_id": None}
    assert relabel.returncode == 6 and _error(relabel)["code"] == "no_project_identity"


def test_a_capability_reads_env_local_without_the_manager(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000e1",
                       copy={"schema": "capabilities.project.v2", "slug": "p"})
    (project / ".env.local").write_text("OTHER=1\nCAPABILITIES_PROJECT_ID=prj_0000000000e1\n")
    _contextkit(tmp_path)
    # The envelope record is written by this manager call.
    _json(_run(tmp_path, project, "path", "--json"))
    env = _env(tmp_path, project)
    # A manager that cannot start proves .env.local, not the manager, answered.
    env["CAPABILITIES_MANAGER_BIN"] = str(tmp_path / "no-manager-here")

    assert _probe(tmp_path, project, env) == {
        "read": "prj_0000000000e1", "write": "prj_0000000000e1", "state": "declared"}


def test_env_local_wins_over_env_and_project_json_is_never_the_id(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=False,
                       copy={"schema": "capabilities.project.v2", "slug": "p"})
    (project / ".env").write_text("CAPABILITIES_PROJECT_ID=prj_0000000000e2\n")
    env = _env(tmp_path, project)
    env["CAPABILITIES_MANAGER_BIN"] = str(tmp_path / "no-manager-here")

    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]
    assert (identity["state"], identity["id"]) == ("declared", "prj_0000000000e2")
    assert _probe(tmp_path, project, env)["write"] == "prj_0000000000e2"

    (project / ".env.local").write_text("CAPABILITIES_PROJECT_ID=prj_0000000000e3\n")
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]
    assert identity["id"] == "prj_0000000000e3"
    assert _probe(tmp_path, project, env)["write"] == "prj_0000000000e3"


def test_the_environment_wins_over_the_env_files(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=False,
                       copy={"schema": "capabilities.project.v2", "slug": "p"})
    (project / ".env.local").write_text("CAPABILITIES_PROJECT_ID=prj_0000000000f2\n")
    env = _env(tmp_path, project)
    env.update({"CAPABILITIES_PROJECT_ID": "prj_0000000000f2",
                "CAPABILITIES_PROJECT_ID_ROOT": str(project)})

    identity = _json(_run(tmp_path, project, "path", "--json", "--identity",
                          env=env))["project_identity"]
    assert (identity["state"], identity["id"]) == ("handed", "prj_0000000000f2")

    # An id the environment sets with its root, as a deployment does, wins over
    # the file: it is the one a write stamps, and nothing refuses.
    env["CAPABILITIES_PROJECT_ID"] = "prj_0000000000f9"
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity",
                          env=env))["project_identity"]
    assert identity == {"state": "handed", "id": "prj_0000000000f9",
                        "bound": False, "contextkit_id": None}
    relabel = _json(_run(tmp_path, project, "relabel", "other", env=env))
    assert relabel["id"] == "prj_0000000000f9"
    assert _probe(tmp_path, project, env)["write"] == "prj_0000000000f9"
    assert _bound(project) == "prj_0000000000f2"

    # Without its root it binds no folder: the files answer.
    env.pop("CAPABILITIES_PROJECT_ID_ROOT")
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity",
                          env=env))["project_identity"]
    assert (identity["state"], identity["id"]) == ("declared", "prj_0000000000f2")

    # A handoff scoped to another root is passed over.
    env["CAPABILITIES_PROJECT_ID_ROOT"] = str(tmp_path / "elsewhere")
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity",
                          env=env))["project_identity"]
    assert (identity["state"], identity["id"]) == ("declared", "prj_0000000000f2")


def test_a_clone_without_its_binding_is_refused_a_new_id(tmp_path: Path) -> None:
    """A v2 project.json says the id is kept outside the repository: a clone
    that has none is not a new project, so init mints nothing."""
    project = _project(tmp_path, bound=False,
                       copy={"schema": "capabilities.project.v2", "slug": "p"})
    _in_files_mode(project)

    result = _run(tmp_path, project, "init")

    assert result.returncode == 6
    error = _error(result)
    assert error["code"] == "no_project_identity" and "init --id" in error["hint"]
    assert _bound(project) is None


def test_init_id_binds_a_clone_and_writes_only_the_id(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=False,
                       copy={"schema": "capabilities.project.v2", "slug": "p"})
    _in_files_mode(project)
    (project / ".env.local").write_text("OTHER=kept\n")

    result = _json(_run(tmp_path, project, "init", "--id", "prj_0000000000a7"))

    assert (project / ".env.local").read_text() == (
        "OTHER=kept\nCAPABILITIES_PROJECT_ID=prj_0000000000a7\n")
    assert _copy(project) == {"schema": "capabilities.project.v2", "slug": "p"}
    # No database resolves here, so the id is bound unverified.
    assert result["raised"] == {"id": "prj_0000000000a7", "verified": False,
                                "reason": "store_not_configured"}
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]
    assert (identity["state"], identity["id"]) == ("declared", "prj_0000000000a7")

    # The id is never rewritten.
    again = _run(tmp_path, project, "init", "--id", "prj_0000000000a8")
    assert again.returncode == 6 and _error(again)["code"] == "project_id_exists"
    assert _bound(project) == "prj_0000000000a7"
    _json(_run(tmp_path, project, "init", "--id", "prj_0000000000a7"))
    assert _bound(project) == "prj_0000000000a7"


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
    assert _bound(project) == "prj_0000000000b2"


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
    env["AGENT_NODE_ID"] = "test-node"

    result = _run(tmp_path, project, "init", env=env)

    assert result.returncode == 6
    assert _error(result)["code"] == "bad_node_id"
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
