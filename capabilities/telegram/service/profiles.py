#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "callva-harness-runner==0.10.0",
#     "capabilities-contract==0.4.1",
# ]
# ///
"""The harness profiles a telegram worker may run on, where each comes from,
and whether one fits.

A profile says how the model runs; it is a callva-harness-runner profile kept
in the store, in the `harness_profiles` table the runner owns. Every name
telegram reaches starts with `telegram-`: the prefix is the capability's scope,
so a room never runs another capability's profile and the runner's own shipped
profiles are out of reach. A name resolves to the project's active version,
then the global one, then the two this bundle ships in `profiles/` -
`telegram-claude` and `telegram-codex`. The version found is used whole, and a
version or file the runner refuses is an error, never a step to the next layer.

Fit is what a turn needs of a profile, checked after the runner has accepted
it: free text the daemon cuts at its reply marker, nobody asked to approve
anything, and a deadline of its own, since a delegated job has no other one.
The narrower settings whatsapp refuses are warnings here, because a telegram
room may run a narrower worker today.

A profile is saved only at the project's scope, under a name neither bundled
nor held globally, so a bundled or global profile is never changed from a chat.

The CLI does not carry the runner in its own dependency header: every worker
`telegram send` resolves that header, and the runner is large. This file's
header is where the runner is pinned; the CLI runs this file for every answer
that needs it, and imports it in-process only for what needs no runner.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import sys
from pathlib import Path

RUNNER_PIN = "callva-harness-runner==0.10.0"
PREFIX = "telegram-"
BUNDLED = ("telegram-claude", "telegram-codex")
HERE = Path(__file__).resolve().parent


class Refusal(Exception):
    """Why a profile cannot be read, used or saved, as the CLI reports it."""

    def __init__(self, exit_code: int, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.code = code
        self.message = message
        self.hint = hint


def runner():
    """The harness library, imported where a profile is read or saved."""
    from callva import harness_runner
    return harness_runner


def store_steps() -> dict:
    """The runner's steps for its profile table and the ledger they are applied
    under, as data a process without the runner can apply."""
    lib = runner()
    return {"owner": lib.PROFILE_STORE_OWNER,
            "steps": [list(step) for step in lib.PROFILE_STORE_STEPS],
            "major": lib.PROFILE_STORE_MAJOR, "minor": lib.PROFILE_STORE_MINOR}


# --- Names -------------------------------------------------------------------

def check_name(name: str) -> str:
    """Refuse a name telegram does not run: one without the prefix, or one the
    runner refuses as a profile name."""
    if not isinstance(name, str) or not name.startswith(PREFIX) or name == PREFIX:
        raise Refusal(6, "profile_name_refused",
                      f"profile {name!r} is not a telegram profile: a telegram room "
                      f"runs only profiles named {PREFIX}<something>",
                      f"`telegram profiles list` shows the {PREFIX} profiles this "
                      "project reaches")
    lib = runner()
    try:
        lib.check_profile_name(name)
    except lib.ProfileError as refused:
        raise Refusal(6, "profile_name_refused", str(refused)) from None
    return name


def bundled_file(name: str) -> Path:
    return HERE / "profiles" / f"{name}.toml"


# --- Fit ---------------------------------------------------------------------

def fit_problems(knobs: dict) -> list[str]:
    """Why a profile cannot carry a telegram turn, one sentence per knob; empty
    when it can."""
    problems = []
    harness = knobs.get("harness")
    if harness not in ("claude", "codex"):
        problems.append(f"harness {harness!r} is not claude or codex")
    if knobs.get("output_schema") is not None:
        problems.append("output_schema is set; the service reads free text and cuts it "
                        "at the reply marker")
    if harness == "claude" and knobs.get("permission_mode") != "bypassPermissions":
        problems.append("permission_mode must be bypassPermissions; a turn has nobody "
                        "to answer a permission prompt")
    if harness == "codex" and knobs.get("approval_policy") != "never":
        problems.append("approval_policy must be never; a turn has nobody to approve "
                        "an escalation")
    if knobs.get("timeout_seconds") is None:
        problems.append("timeout_seconds is not set; the runner's default of 600 would "
                        "end every delegated job at ten minutes, and a job has no "
                        "other deadline")
    return problems


def fit_warnings(knobs: dict) -> list[str]:
    """What narrows a worker below the access it has under the bundled profiles,
    one sentence per knob. A room may run that way; it is said, not refused."""
    warnings = []
    harness = knobs.get("harness")
    if harness == "claude":
        tools = knobs.get("tools")
        if tools is not None and "Bash" not in tools:
            warnings.append("tools leaves out Bash; a worker runs capability commands "
                            "through it")
        if "Bash" in (knobs.get("disallowed_tools") or []):
            warnings.append("disallowed_tools names Bash; a worker runs capability "
                            "commands through it")
        settings = knobs.get("settings")
        if isinstance(settings, dict) and (settings.get("sandbox") or {}).get("enabled"):
            warnings.append("settings enables the sandbox; a worker has full access "
                            "under the bundled profile")
    elif harness == "codex":
        config = knobs.get("codex_config") or {}
        if config.get("sandbox_mode") != "danger-full-access":
            warnings.append("codex_config.sandbox_mode is not danger-full-access; a "
                            "worker has full access under the bundled profile")
        if config.get("permissions") or config.get("default_permissions") \
                or config.get("permission_profile"):
            warnings.append("codex_config names a permissions profile; a worker has "
                            "full access under the bundled profile")
    return warnings


def rights(knobs: dict) -> str:
    """What a turn on this profile may do, in one phrase."""
    if knobs.get("harness") == "claude":
        tools = knobs.get("tools")
        return (f"permission_mode {knobs.get('permission_mode') or 'default'}, "
                + ("every tool" if tools is None else f"tools {', '.join(tools) or 'none'}"))
    config = knobs.get("codex_config") or {}
    return (f"approval_policy {knobs.get('approval_policy') or 'default'}, "
            f"sandbox_mode {config.get('sandbox_mode') or 'default'}")


# --- Resolution --------------------------------------------------------------

def stored(conn, project: str | None, schema: str | None = None) -> list:
    """Every active version a lookup from the project reaches, as the runner
    reads them. A store whose table does not exist yet holds none."""
    try:
        return runner().stored_profiles(conn, project=project, schema=schema)
    except Exception as exc:
        if type(exc).__name__ == "UndefinedTable":
            if not getattr(conn, "autocommit", True):
                conn.rollback()
            return []
        raise


def resolve(name: str, active: list, project: str | None, *, fit: bool = True) -> dict:
    """Where `name` resolves and the knobs it sets, or Refusal naming the name,
    the layer and the version or path. `active` is what `stored` answered for
    the project. The answer's `layer` is `project`, `global` or `bundled`; with
    `fit`, a profile that cannot carry a turn is refused too."""
    lib = runner()
    check_name(name)
    shipped = bundled_file(name)
    try:
        found = lib.resolve_among(name, active, project=project)
    except lib.ProfileNotFound:
        if not shipped.is_file():
            raise Refusal(3, "profile_not_found",
                          f"profile {name!r} is in none of this project's profiles, the "
                          "global ones or the ones telegram ships",
                          "`telegram profiles list` shows the profiles this project "
                          "reaches") from None
        origin = {"name": name, "layer": "bundled", "version": None,
                  "path": str(shipped), "shadows": []}
        try:
            knobs = lib.ProfileFile(name, "shipped", str(shipped), shipped.read_text()).knobs()
            lib.Profile.from_dict(knobs)
        except (OSError, lib.ProfileError, ValueError, TypeError, KeyError) as exc:
            raise Refusal(6, "profile_invalid",
                          f"profile {name!r} (layer bundled, {shipped}) is refused by "
                          f"the harness runner: {exc}") from None
    except (lib.ProfileError, ValueError, TypeError, KeyError) as exc:
        raise Refusal(6, "profile_invalid",
                      f"profile {name!r} is refused by the harness runner: {exc}") from None
    else:
        origin = {"name": name, "layer": found.layer, "version": found.id,
                  "shadows": list(found.shadows)
                  + (["bundled"] if shipped.is_file() else [])}
        knobs = dict(found.spec)
        try:
            lib.Profile.from_dict(knobs)
        except (lib.ProfileError, ValueError, TypeError, KeyError) as exc:
            raise Refusal(6, "profile_invalid",
                          f"profile {name!r} (layer {found.layer}, version {found.id}) "
                          f"is refused by the harness runner: {exc}") from None
    if fit:
        problems = fit_problems(knobs)
        if problems:
            raise Refusal(6, "profile_unfit",
                          f"profile {name!r} (layer {origin['layer']}"
                          + (f", version {origin['version']}" if origin["version"] else "")
                          + ") does not fit a telegram turn: " + "; ".join(problems))
    return {**origin, "knobs": knobs}


def describe(found: dict) -> dict:
    """One resolved profile as `list` shows it: its settings and rights, never
    an `env_set` value or a `settings` object."""
    knobs = found["knobs"]
    problems = fit_problems(knobs)
    row = {key: found[key] for key in ("name", "layer", "version", "path", "shadows")
           if key in found}
    row.update(harness=knobs.get("harness"), model=knobs.get("model"),
               effort=knobs.get("effort"), service_tier=knobs.get("service_tier"),
               rights=rights(knobs), timeout_seconds=knobs.get("timeout_seconds"),
               ok=not problems, problems=problems, warnings=fit_warnings(knobs))
    return row


def names(active: list, project: str | None) -> list[str]:
    """Every telegram profile name a lookup from the project reaches."""
    held = {one.name for one in active if one.active and one.name.startswith(PREFIX)
            and (one.scope == "global" or one.scope_id == project)}
    return sorted(held | set(BUNDLED))


def listing(active: list, project: str | None) -> list[dict]:
    """Every telegram profile the project reaches, as it resolves, and whether
    it fits; one that cannot be read is listed with why."""
    rows = []
    for name in names(active, project):
        try:
            rows.append(describe(resolve(name, active, project, fit=False)))
        except Refusal as refused:
            rows.append({"name": name, "ok": False, "code": refused.code,
                         "error": refused.message})
    return rows


# --- Saving ------------------------------------------------------------------

def substitution_in(value, where: str = "") -> str | None:
    """Where a profile's knobs carry a `${` sequence, or None when none does."""
    if isinstance(value, str):
        return where if "${" in value else None
    if isinstance(value, list):
        found = (substitution_in(v, f"{where}[{i}]") for i, v in enumerate(value))
    elif isinstance(value, dict):
        found = (f"{where}.{k}".lstrip(".") if "${" in str(k)
                 else substitution_in(v, f"{where}.{k}".lstrip("."))
                 for k, v in value.items())
    else:
        return None
    return next((one for one in found if one), None)


def checked_spec(name: str, text: str, where: str, active: list) -> dict:
    """The spec a save of `text` as `name` would store, or Refusal saying why
    it is refused."""
    lib = runner()
    check_name(name)
    if name in BUNDLED:
        raise Refusal(6, "profile_reserved",
                      f"profile {name!r} ships with telegram and is never changed from "
                      "here",
                      f"save the change as a new {PREFIX} name, then point the room at it")
    held = next((one for one in active if one.name == name and one.active
                 and one.scope == "global"), None)
    if held is not None:
        raise Refusal(6, "profile_reserved",
                      f"profile {name!r} is held at global scope (version {held.id}) and "
                      "is never changed from here",
                      f"save the change as a new {PREFIX} name")
    try:
        knobs = lib.ProfileFile(name, "file", where, text).knobs()
        spec = lib.check_spec(knobs, where)
    except lib.ProfileError as refused:
        raise Refusal(6, "profile_invalid", str(refused),
                      "`python -m callva.harness_runner knobs` lists every knob") from None
    knob = substitution_in(spec)
    if knob:
        raise Refusal(6, "profile_invalid",
                      f"profile {name!r} in {where}: its `{knob}` carries `${{`, and a "
                      "profile is read as the runner reads it, with nothing substituted",
                      "write the value itself into the profile")
    problems = fit_problems(spec)
    if problems:
        raise Refusal(6, "profile_unfit",
                      f"profile {name!r} in {where} does not fit a telegram turn: "
                      + "; ".join(problems))
    return spec


def save(conn, name: str, text: str, where: str, project: str, *,
         base: str | None = None, created_by: str | None = None,
         schema: str | None = None) -> dict:
    """Save `text` as the new active version of `name` at the project's scope,
    and commit. The runner's profile table is brought up to date first, under
    its own ledger."""
    lib = runner()
    from capabilities_contract.db import migrate
    steps = store_steps()
    migrate(conn, steps["owner"], [tuple(step) for step in steps["steps"]],
            major=steps["major"], minor=steps["minor"])
    try:
        spec = checked_spec(name, text, where, stored(conn, project, schema))
    except BaseException:
        conn.rollback()
        raise
    try:
        version, created = lib.save_profile(conn, name, spec, scope="project",
                                            scope_id=project, base=base,
                                            created_by=created_by, schema=schema)
        conn.commit()
    except lib.ProfileConflict as exc:
        conn.rollback()
        raise Refusal(6, "conflict", str(exc),
                      f"read it again with `telegram profiles show {name}` and save over "
                      "that version with --base") from None
    except lib.ProfileError as exc:
        conn.rollback()
        raise Refusal(6, "profile_invalid", str(exc)) from None
    except BaseException:
        conn.rollback()
        raise
    return {"profile": name, "scope": "project", "project_id": project,
            "version": version.id, "created": created, "created_by": version.created_by}


# --- Who may save ------------------------------------------------------------

def _daemon_control():
    """The daemon's role defaults and its rule for whether a command list
    admits a command, read out of `daemon.py` rather than restated here, so
    the gate on a profile is the gate on `/set`."""
    wanted = {"CONTROL_DEFAULTS", "_control_command_key", "_control_commands_allow"}
    tree = ast.parse((HERE / "daemon.py").read_text(encoding="utf-8"))
    nodes = [node for node in tree.body
             if (isinstance(node, ast.FunctionDef) and node.name in wanted)
             or (isinstance(node, ast.Assign)
                 and any(getattr(t, "id", None) in wanted for t in node.targets))]
    namespace: dict = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(HERE / "daemon.py"),
                 "exec"), namespace)
    return namespace["CONTROL_DEFAULTS"], namespace["_control_commands_allow"]


def role_admitted_to_set(role: str, settings: dict | None) -> bool:
    """Whether the control gate admits a sender of `role` to `/set`: the
    daemon's defaults for the role, with the settings' `control.roles` entry
    for it laid over them."""
    defaults, commands_allow = _daemon_control()
    policy = dict((defaults.get("roles") or {}).get(role) or {})
    control = (settings or {}).get("control")
    roles = control.get("roles") if isinstance(control, dict) else None
    override = roles.get(role) if isinstance(roles, dict) else None
    if isinstance(override, dict):
        policy.update(override)
    return commands_allow(policy.get("commands"), "set")


# --- Command face ------------------------------------------------------------

def _jobs_module():
    spec = importlib.util.spec_from_file_location("telegram_service_jobs_profiles",
                                                  HERE / "jobs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _connect(project_root: str | None):
    """A connection to the project's store, as the job register reaches it,
    and the schema it is bound to."""
    from capabilities_contract.db import DbError, connect
    jobs = _jobs_module()
    try:
        setting = jobs.store_setting(Path(project_root) if project_root else None)
    except jobs.JobError as exc:
        raise Refusal(6, exc.slug, exc.message, exc.hint) from None
    try:
        return connect(application_name="telegram", setting=setting), setting.schema
    except DbError as exc:
        raise Refusal(5, exc.slug, exc.message, exc.hint) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="profiles.py")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("store-steps")
    for verb in ("list", "show", "save"):
        one = commands.add_parser(verb)
        if verb != "list":
            one.add_argument("name")
        one.add_argument("--project-id", required=True)
        one.add_argument("--project-root")
        if verb == "save":
            one.add_argument("--where", default="stdin")
            one.add_argument("--base")
            one.add_argument("--created-by")
    args = parser.parse_args(argv)
    try:
        if args.command == "store-steps":
            answer = store_steps()
        else:
            conn, schema = _connect(args.project_root)
            try:
                if args.command == "save":
                    answer = save(conn, args.name, sys.stdin.read(), args.where,
                                  args.project_id, base=args.base,
                                  created_by=args.created_by, schema=schema)
                else:
                    active = stored(conn, args.project_id, schema)
                    if args.command == "list":
                        answer = {"project_id": args.project_id,
                                  "profiles": listing(active, args.project_id)}
                    else:
                        found = resolve(args.name, active, args.project_id, fit=False)
                        version = next((one for one in active if one.id == found["version"]),
                                       None)
                        answer = {**describe(found),
                                  "text": (version.text() if version is not None
                                           else bundled_file(args.name).read_text())}
            finally:
                conn.close()
    except Refusal as refused:
        error = {"code": refused.code, "message": refused.message}
        if refused.hint:
            error["hint"] = refused.hint
        sys.stderr.write(json.dumps({"error": error}, ensure_ascii=False) + "\n")
        return refused.exit_code
    sys.stdout.write(json.dumps(answer, ensure_ascii=False, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
