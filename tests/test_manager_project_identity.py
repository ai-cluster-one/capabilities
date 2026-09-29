"""A project's id: ContextKit's where ContextKit binds the project, project.json's
otherwise, and project.json never more than a copy of ContextKit's."""

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


def _contextkit(tmp_path: Path, *, identity_exit: int = 0,
                identity_error: str = "no such command") -> Path:
    """A stand-in `contextkit` that answers both questions out of the project's
    own binding, the way the real one does, and logs each call."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "contextkit-calls.log"
    script = bin_dir / "contextkit"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'if [ "$1" = path ]; then printf "%s/capabilities\\n" "$(pwd)"; exit 0; fi\n'
        f"if [ {identity_exit} -ne 0 ]; then echo \"{identity_error}\" >&2; exit {identity_exit}; fi\n"
        'root="$5"\n'
        'id=$(sed -n \'s/^id = "\\(.*\\)"$/\\1/p\' "$root/.contextkit/config.toml")\n'
        'if [ -n "$id" ]; then id="\\"$id\\""; else id=null; fi\n'
        'printf \'{"project": "%s", "config": "%s/.contextkit/config.toml", "id": %s}\\n\' '
        '"$root" "$root" "$id"\n')
    script.chmod(0o755)
    return log


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
    env.pop("CAPABILITIES_PROJECT_ENVELOPE", None)
    env.pop("CAPABILITIES_PROJECT_ENVELOPE_ROOT", None)
    return env


def _run(tmp_path: Path, project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MANAGER), *args], cwd=project,
        env=_env(tmp_path, project), text=True, capture_output=True, timeout=60)


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
    return sum(1 for line in log.read_text().splitlines() if line.startswith("identity "))


def test_init_copies_contextkit_id_into_a_new_identity(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000a1")
    _contextkit(tmp_path)

    _json(_run(tmp_path, project, "init"))

    assert _copy(project)["id"] == "prj_0000000000a1"
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]
    assert identity["state"] == "adopted" and identity["id"] == "prj_0000000000a1"


def test_init_mints_no_id_while_contextkit_has_none(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True)
    _contextkit(tmp_path)

    _json(_run(tmp_path, project, "init"))

    # No copy at all: `contextkit identity adopt` reads an absent project.json
    # as "no id yet", and refuses one that carries no id.
    assert not (project / "capabilities" / "project.json").exists()
    assert (project / "capabilities" / "settings.json").is_file()
    report = _json(_run(tmp_path, project, "doctor"))
    assert report["project_identity"]["state"] == "pending"
    assert report["project_identity"]["id"] is None


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


def test_unbound_project_keeps_its_own_id_and_never_asks(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=False)
    log = _contextkit(tmp_path)

    _json(_run(tmp_path, project, "init"))
    minted = _copy(project)["id"]
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]

    assert minted.startswith("prj_")
    assert identity == {"state": "unbound", "id": minted, "bound": False,
                        "contextkit_id": None, "project_json_id": minted}
    assert not log.exists()


def test_pending_adoption_uses_the_existing_copy(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True,
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000b1"})
    _contextkit(tmp_path)

    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]

    assert identity["state"] == "pending" and identity["id"] == "prj_0000000000b1"
    report = json.loads(_run(tmp_path, project, "doctor").stdout)
    assert report["project_identity"]["state"] == "pending"
    assert not [f for f in report["findings"] if "project id" in f]


def test_a_mismatch_fails_doctor_and_names_both_ids(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000c1",
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000c2"})
    _contextkit(tmp_path)

    doctor = _run(tmp_path, project, "doctor")
    identity = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]

    assert doctor.returncode == 7
    report = json.loads(doctor.stdout)
    finding = next(f for f in report["findings"] if "project id" in f)
    assert "prj_0000000000c1" in finding and "prj_0000000000c2" in finding
    assert report["project_identity"]["state"] == "mismatch"
    # Reads keep ContextKit's id, which is the project's.
    assert identity["id"] == "prj_0000000000c1"


def test_relabel_refuses_a_mismatch_naming_both_ids(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000c1",
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000c2"})
    _contextkit(tmp_path)

    result = _run(tmp_path, project, "relabel", "other")

    assert result.returncode == 6
    error = _error(result)
    assert error["code"] == "project_id_mismatch"
    assert "prj_0000000000c1" in error["message"] and "prj_0000000000c2" in error["message"]
    assert _copy(project)["slug"] == "p"


def test_the_answer_is_recorded_until_the_binding_moves(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True,
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000d1"})
    log = _contextkit(tmp_path)

    first = _json(_run(tmp_path, project, "path", "--json", "--identity"))
    second = _json(_run(tmp_path, project, "path", "--json", "--identity"))
    assert first["project_identity"]["state"] == second["project_identity"]["state"] == "pending"
    assert _identity_calls(log) == 1

    config = project / ".contextkit" / "config.toml"
    config.write_text(config.read_text() + '\n[identity]\nid = "prj_0000000000d1"\n')
    third = _json(_run(tmp_path, project, "path", "--json", "--identity"))

    assert third["project_identity"]["state"] == "adopted"
    assert _identity_calls(log) == 2


def test_contextkit_that_cannot_answer_mints_nothing_and_is_reported(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True)
    log = _contextkit(tmp_path, identity_exit=2)

    _json(_run(tmp_path, project, "init"))
    path = _run(tmp_path, project, "path", "--json", "--identity")
    doctor = _run(tmp_path, project, "doctor")
    _run(tmp_path, project, "path", "--json", "--identity")

    assert not (project / "capabilities" / "project.json").exists()
    assert path.returncode == 6
    assert _error(path)["code"] == "contextkit_identity_failed"
    assert doctor.returncode == 7
    assert json.loads(doctor.stdout)["project_identity"]["state"] == "unresolved"
    # A failed answer is never recorded, so every question asks again.
    assert _identity_calls(log) == 4


def test_a_capability_reads_the_recorded_identity_without_the_manager(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000e1",
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000e1"})
    _contextkit(tmp_path)
    _json(_run(tmp_path, project, "path", "--json", "--identity"))
    env = _env(tmp_path, project)
    # A manager that cannot start proves the record, not the manager, answered.
    env["CAPABILITIES_MANAGER_BIN"] = str(tmp_path / "no-manager-here")
    probe = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_loader('cap', loader=None)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        f"mod.__file__ = {str(TASKS)!r}\n"
        f"src = open({str(TASKS)!r}).read().replace('if __name__ == \"__main__\":\\n    main()\\n', '')\n"
        "exec(compile(src, mod.__file__, 'exec'), mod.__dict__)\n"
        "print(json.dumps({'read': mod._project_id(), 'write': mod._project_id(write=True)}))\n")
    # The envelope record is written by the manager call above too.
    result = subprocess.run([sys.executable, "-c", probe], cwd=project, env=env,
                            text=True, capture_output=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"read": "prj_0000000000e1",
                                         "write": "prj_0000000000e1"}


def test_contextkit_without_the_identity_verb_is_pending_adoption(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True,
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000f1"})
    log = _contextkit(tmp_path, identity_exit=2, identity_error=(
        "contextkit: error: argument command: invalid choice: 'identity' "
        "(choose from 'help', 'path', 'build')"))

    first = _json(_run(tmp_path, project, "path", "--json", "--identity"))["project_identity"]
    doctor = json.loads(_run(tmp_path, project, "doctor").stdout)
    _json(_run(tmp_path, project, "path", "--json", "--identity"))

    assert (first["state"], first["id"]) == ("pending", "prj_0000000000f1")
    assert doctor["project_identity"]["state"] == "pending"
    assert not [f for f in doctor["findings"] if "project id" in f]
    # A ContextKit without the verb is an answer, and is recorded like one.
    assert _identity_calls(log) == 1


def test_a_handed_down_id_is_used_without_asking_contextkit(tmp_path: Path) -> None:
    project = _project(tmp_path, bound=True, contextkit_id="prj_0000000000f2",
                       copy={"schema": "capabilities.project.v1", "slug": "p",
                             "id": "prj_0000000000f2"})
    env = _env(tmp_path, project)
    env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep)
                                  if not (Path(p) / "contextkit").exists())
    env.update({"CAPABILITIES_PROJECT_ENVELOPE": str(project / "capabilities"),
                "CAPABILITIES_PROJECT_ENVELOPE_ROOT": str(project),
                "CAPABILITIES_PROJECT_ID": "prj_0000000000f2",
                "CAPABILITIES_PROJECT_ID_ROOT": str(project)})

    result = subprocess.run([sys.executable, str(MANAGER), "path", "--json", "--identity"],
                            cwd=project, env=env, text=True, capture_output=True, timeout=60)
    identity = _json(result)["project_identity"]
    assert (identity["state"], identity["id"]) == ("handed", "prj_0000000000f2")

    env["CAPABILITIES_PROJECT_ID_ROOT"] = str(tmp_path / "elsewhere")
    elsewhere = subprocess.run([sys.executable, str(MANAGER), "path", "--json", "--identity"],
                               cwd=project, env=env, text=True, capture_output=True, timeout=60)
    assert elsewhere.returncode == 6
    assert _error(elsewhere)["code"] == "contextkit_unavailable"


def test_resolving_the_envelope_alone_hands_the_id_down_beside_it(tmp_path: Path) -> None:
    """A capability that never asks for the id still hands it to its children
    with the envelope, where a write may stamp it, and hands nothing down on a
    mismatch."""
    _contextkit(tmp_path)
    probe = (
        "import importlib.util, json, os\n"
        "spec = importlib.util.spec_from_loader('cap', loader=None)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        f"mod.__file__ = {str(TASKS)!r}\n"
        f"src = open({str(TASKS)!r}).read().replace('if __name__ == \"__main__\":\\n    main()\\n', '')\n"
        "exec(compile(src, mod.__file__, 'exec'), mod.__dict__)\n"
        "mod._envelope_home(mod._project_root())\n"
        "print(json.dumps({k: os.environ.get(k) for k in ('CAPABILITIES_PROJECT_ID', 'CAPABILITIES_PROJECT_ID_ROOT')}))\n")
    for contextkit_id, copy_id, handed in (("prj_0000000000f3", "prj_0000000000f3", "prj_0000000000f3"),
                                           ("prj_0000000000f3", "prj_0000000000f4", None)):
        base = tmp_path / copy_id
        base.mkdir()
        project = _project(base, bound=True, contextkit_id=contextkit_id,
                           copy={"schema": "capabilities.project.v1", "slug": "p", "id": copy_id})
        env = _env(tmp_path, project)
        result = subprocess.run([sys.executable, "-c", probe], cwd=project, env=env,
                                text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "CAPABILITIES_PROJECT_ID": handed,
            "CAPABILITIES_PROJECT_ID_ROOT": str(project.resolve()) if handed else None}
