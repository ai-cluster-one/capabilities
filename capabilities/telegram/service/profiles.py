#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "callva-harness-runner==0.10.0",
#     "capabilities-contract==0.5.1",
# ]
# ///
"""The harness profiles a telegram worker may run on, where each comes from,
and whether one fits.

A profile says how the model runs; it is a callva-harness-runner profile kept
in the store, in the `harness_profiles` table the runner owns. Every name
telegram reaches starts with `telegram-`: the prefix is the capability's scope,
so a room never runs another capability's profile and the runner's own shipped
profiles are out of reach. A name resolves to the project's active version,
then the global one, then the ones this bundle ships in `profiles/` -
`telegram-claude` and `telegram-codex` for a turn, `telegram-gate-claude` and
`telegram-gate-codex` for the reply gate. The version found is used whole, and a
version or file the runner refuses is an error, never a step to the next layer.

Fit is what a turn needs of a profile, checked after the runner has accepted
it: free text the daemon cuts at its reply marker, nobody asked to approve
anything, and a deadline of its own, since a delegated job has no other one.
The narrower settings whatsapp refuses are warnings here, because a telegram
room may run a narrower worker today.

A profile is saved only at the project's scope, under a name neither bundled
nor held globally, so a bundled or global profile is never changed from a chat.

A room names the profile its turns run on: the channel's `/set profile`, a
forum topic's, the group's or the direct user's `profile`, then
`defaults.profile` (the environment's TELEGRAM_SERVICE_PROFILE in its place),
else `stub`, which runs in process on no profile. The daemon resolves the name
per turn; `check` resolves and fits every name the settings reach for a start,
a reload or the doctor, and `probe` runs one minimal live turn on each for the
doctor. `materialize` writes the profiles the migration from the old worker
settings needs, named from the values they carry.

The CLI does not carry the runner in its own dependency header: every worker
`telegram send` resolves that header, and the runner is large. This file's
header is where the runner is pinned; the CLI runs this file for every answer
that needs it, and imports it in-process only for what needs no runner.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

RUNNER_PIN = "callva-harness-runner==0.10.0"
PREFIX = "telegram-"
BUNDLED = ("telegram-claude", "telegram-codex")
# The reply gate's profiles, bundled beside the turn's at the same lowest layer.
# They are no turn's: they set an output schema, so the turn's fit refuses them,
# and the gate checks its own fit for them (`gate.py`).
GATE_BUNDLED = ("telegram-gate-claude", "telegram-gate-codex")
# The reserved name that runs in process, on no profile: what a room runs when
# nothing names a profile, and the seed's own choice.
STUB = "stub"
# The environment's default profile, over `defaults.profile`, so a deployed node
# chooses without editing its settings.
DEFAULT_ENV = "TELEGRAM_SERVICE_PROFILE"
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


# --- The old worker settings ------------------------------------------------
#
# Before a room named a profile it named a worker - `worker`, and per worker a
# model, an effort, a codex service tier and claude's `context` block - and a
# turn on worker W ran on `telegram-W` with those settings laid over its knobs.
# `implicit_knobs` is the one place those settings become knobs: a job row
# registered before rooms named profiles runs through it, and the migration
# writes the profiles it derives with it.

WORKERS = ("claude", "codex")


def _setting(value):
    """A setting's value, with "default" and empty read as unset, as the
    daemon reads them."""
    return None if value in (None, "", "default") else value


def implicit_knobs(worker: str, cfg: dict | None) -> dict:
    """The knobs one worker's settings lay over `telegram-<worker>`: `model`;
    claude's `effort` and its `context` block (`tools`; `mcp: false` as a strict,
    empty MCP config); codex's `reasoning_effort` as `effort` and its
    `service_tier`, `fast` meaning `priority`. A setting left unset leaves the
    profile's knob as it is."""
    cfg = cfg or {}
    knobs = {}
    model = _setting(cfg.get("model"))
    if model:
        knobs["model"] = str(model)
    if worker == "claude":
        effort = _setting(cfg.get("effort"))
        if effort:
            knobs["effort"] = str(effort)
        context = cfg.get("context") if isinstance(cfg.get("context"), dict) else {}
        if context.get("tools") is not None:
            knobs["tools"] = [str(tool) for tool in context["tools"]]
        if context.get("mcp") is False:
            knobs["strict_mcp"] = True
            knobs["mcp_config"] = {"mcpServers": {}}
    elif worker == "codex":
        effort = _setting(cfg.get("reasoning_effort"))
        if effort:
            knobs["effort"] = str(effort)
        tier = _setting(cfg.get("service_tier"))
        if tier:
            knobs["service_tier"] = "priority" if tier == "fast" else str(tier)
    return knobs


def _where(found: dict) -> str:
    return (f"profile {found['name']!r} (layer {found['layer']}"
            + (f", version {found['version']}" if found.get("version") else "") + ")")


def overlaid(found: dict, overlay: dict) -> dict:
    """`found` with `overlay` laid over its knobs, or Refusal naming the
    profile and what the runner or fit refuses in the result."""
    knobs = {**found["knobs"], **overlay}
    if overlay:
        lib = runner()
        try:
            lib.Profile.from_dict(knobs)
        except (lib.ProfileError, ValueError, TypeError, KeyError) as exc:
            raise Refusal(6, "profile_invalid",
                          f"{_where(found)} with the worker settings {sorted(overlay)} laid "
                          f"over it is refused by the harness runner: {exc}") from None
        problems = fit_problems(knobs)
        if problems:
            raise Refusal(6, "profile_unfit",
                          f"{_where(found)} with the worker settings {sorted(overlay)} laid "
                          "over it does not fit a telegram turn: " + "; ".join(problems))
    return {**found, "knobs": knobs, "overlay": overlay}


def implicit(worker: str, cfg: dict | None, active: list, project: str | None) -> dict:
    """What a turn on `worker` with these old settings runs on: `telegram-<worker>`
    resolved and fitted as any name is, with `implicit_knobs` laid over it."""
    if worker not in WORKERS:
        raise Refusal(6, "profile_name_refused",
                      f"worker {worker!r} runs on no profile; a harness turn runs on "
                      "claude or codex")
    return overlaid(resolve(PREFIX + worker, active, project), implicit_knobs(worker, cfg))


# --- The names a room's settings reach -----------------------------------------

def default_profile(settings: dict | None, environ=None) -> tuple[str | None, str]:
    """The default profile name and where it was named: the environment's
    TELEGRAM_SERVICE_PROFILE, then `defaults.profile`; None when neither does."""
    environ = environ if environ is not None else {}
    named = str(environ.get(DEFAULT_ENV) or "").strip()
    if named:
        return named, DEFAULT_ENV
    defaults = (settings or {}).get("defaults")
    defaults = defaults if isinstance(defaults, dict) else {}
    if defaults.get("profile"):
        return str(defaults["profile"]), "settings.defaults.profile"
    return None, "default"


def reached(settings: dict | None, environ=None) -> list[dict]:
    """Every profile name the settings name, each with the position naming it:
    the default (TELEGRAM_SERVICE_PROFILE or `defaults.profile`), the voice
    default, and every direct user, group and forum topic that names one. The
    stub is listed too; it runs in process and resolves to nothing."""
    settings = settings if isinstance(settings, dict) else {}

    def mapping(value):
        return value if isinstance(value, dict) else {}

    rows = []
    name, position = default_profile(settings, environ)
    if name:
        rows.append({"position": position, "profile": name})
    defaults = mapping(settings.get("defaults"))
    voice = mapping(defaults.get("voice_agent"))
    if voice.get("profile"):
        rows.append({"position": "settings.defaults.voice_agent.profile",
                     "profile": str(voice["profile"])})
    for user_id, policy in mapping(settings.get("allowed_users")).items():
        if mapping(policy).get("profile"):
            rows.append({"position": f"settings.allowed_users.{user_id}.profile",
                         "profile": str(policy["profile"])})
    for chat_id, policy in mapping(settings.get("allowed_groups")).items():
        policy = mapping(policy)
        if policy.get("profile"):
            rows.append({"position": f"settings.allowed_groups.{chat_id}.profile",
                         "profile": str(policy["profile"])})
        for topic_id, topic in mapping(policy.get("topics")).items():
            if mapping(topic).get("profile"):
                rows.append({"position":
                             f"settings.allowed_groups.{chat_id}.topics.{topic_id}.profile",
                             "profile": str(topic["profile"])})
    return rows


def turn(name: str, active: list, project: str | None) -> dict | None:
    """What a turn named `name` runs on: None for the stub, else the profile
    resolved and fitted, or Refusal naming it and the problem."""
    if name == STUB:
        return None
    return resolve(name, active, project)


def check(settings: dict | None, environ, active: list, project: str | None) -> list[dict]:
    """Every name the settings reach that does not resolve or fit, each with its
    position and why; empty when every one does."""
    problems = []
    for row in reached(settings, environ):
        try:
            turn(row["profile"], active, project)
        except Refusal as refused:
            problems.append({**row, "code": refused.code, "message": refused.message})
    return problems


def problems_sentence(problems: list[dict]) -> str:
    return "; ".join(f"{one['position']} names {one['profile']}: {one['message']}"
                     for one in problems)


# --- The doctor's probe ------------------------------------------------------

# The whole of what the doctor asks a harness. Nothing about it needs a tool,
# a file or a project, so the answer is the harness's verdict on the model and
# nothing else.
PROBE_PROMPT = "Reply with the single word OK."

# A failure kind the runner names, as the doctor's verdict reads it.
VERDICTS = {"model_refused": "model_refused", "quota": "quota_exhausted",
            "not_authenticated": "not_authenticated", "binary_missing": "binary_missing",
            "timeout": "timed_out", "incompatible_harness": "incompatible_harness"}


def _workers_module():
    spec = importlib.util.spec_from_file_location("telegram_service_workers_profiles",
                                                  HERE / "workers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe(entries: list[dict], timeout: float, active: list, project: str | None, cwd: str,
          *, run=None, environ=None, live: bool = True) -> list[dict]:
    """One minimal live turn per distinct profile the settings name, held to
    `timeout` seconds; with `live` false, only whether each resolves and fits,
    and no turn is run.

    Each entry is a name and the positions naming it (`declared_at`); its row
    adds the harness and model it resolved to (`worker`, `model`), the layer
    and the version, and the verdict - `accepted`, `resolved` when no turn was
    run, `in_process` for the stub, `model_refused`, `quota_exhausted`,
    `not_authenticated`, `binary_missing`, `timed_out`, `incompatible_harness`
    or `failed`. Only `accepted`, `resolved` and `in_process` are ok. `reason`
    is the harness's own sentence, or why the name does not resolve or fit;
    `notice` the product's for a refused model."""
    workers = _workers_module()
    lib = runner() if run is None else None
    run = run or lib.run
    env = workers.scrub_worker_env(environ if environ is not None else os.environ)
    rows = []
    for entry in entries:
        name = entry.get("profile")
        row = {"profile": name, "declared_at": list(entry.get("declared_at") or []),
               "worker": None, "model": None, "layer": None, "version": None}
        if name == STUB:
            rows.append({**row, "worker": STUB, "ok": True, "verdict": "in_process",
                         "binary": None, "reason": None, "notice": None, "seconds": 0.0})
            continue
        try:
            found = resolve(name, active, project)
        except Refusal as refused:
            rows.append({**row, "ok": False, "verdict": "failed", "binary": None,
                         "reason": refused.message, "notice": None, "seconds": 0.0})
            continue
        knobs = {**found["knobs"], "timeout_seconds": float(timeout)}
        worker = knobs.get("harness")
        row.update(worker=worker, model=knobs.get("model"), layer=found["layer"],
                   version=found["version"])
        if not live:
            rows.append({**row, "ok": True, "verdict": "resolved", "binary": worker,
                         "reason": None, "notice": None, "seconds": 0.0})
            continue
        started = time.monotonic()
        lib = lib or runner()
        result = run(PROBE_PROMPT, lib.Profile.from_dict(knobs), cwd, environ=env)
        seconds = round(time.monotonic() - started, 3)
        if result.ok:
            rows.append({**row, "ok": True, "verdict": "accepted", "binary": worker,
                         "reason": None, "notice": None, "seconds": seconds})
            continue
        kind = str(getattr(result.failure, "kind", "error"))
        message = str(getattr(result.failure, "message", "") or kind)
        verdict = VERDICTS.get(kind, "failed")
        notice = (workers.model_refusal_notice(worker, knobs.get("model"))
                  if verdict == "model_refused" else None)
        rows.append({**row, "ok": False, "verdict": verdict, "binary": worker,
                     "reason": message.replace("\n", " ")[:500], "notice": notice,
                     "seconds": seconds})
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
    if name in BUNDLED + GATE_BUNDLED:
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


# --- The migration's profiles -------------------------------------------------
#
# `telegram service migrate-profiles` moves the old worker settings to profile
# names. Each distinct combination of a worker and its settings becomes one
# project profile: `telegram-<worker>` as it resolves, with `implicit_knobs`
# laid over it, which is exactly what a turn on that combination ran on. A
# combination that changes nothing is `telegram-<worker>` itself and writes
# nothing. The name is read off the values, so a rerun finds the identical
# spec under the same name and creates nothing.

_SEGMENT = re.compile(r"[^A-Za-z0-9._]+")


def _segment(value) -> str:
    return _SEGMENT.sub("-", str(value)).strip("-.")


def _digest(worker: str, overlay: dict) -> str:
    return hashlib.sha256(json.dumps({"worker": worker, **overlay}, sort_keys=True)
                          .encode("utf-8")).hexdigest()[:10]


def derived_name(worker: str, overlay: dict) -> str:
    """The profile name a worker and the knobs its settings lay over it are
    saved under: `telegram-<worker>` then the model, the effort, the service
    tier, the tools and `no-mcp`, each as written (`telegram-codex-gpt-5.5-high-
    priority`); a name too long for the runner is the worker and a digest of
    the values instead."""
    if not overlay:
        return PREFIX + worker
    parts = [PREFIX + worker]
    for knob in ("model", "effort", "service_tier"):
        if overlay.get(knob):
            parts.append(_segment(overlay[knob]))
    if "tools" in overlay:
        parts.append("tools-" + ("-".join(_segment(tool).lower() for tool in overlay["tools"])
                                 or "none"))
    if overlay.get("strict_mcp"):
        parts.append("no-mcp")
    name = "-".join(part for part in parts if part)
    if len(name) > 64:
        name = f"{PREFIX}{worker}-{_digest(worker, overlay)}"
    return name


def materialize(conn, combos: list[dict], project: str, *, apply: bool,
                created_by: str | None = None, schema: str | None = None) -> list[dict]:
    """The profile each old combination of a worker and its settings moves to,
    in the order given, written at the project's scope when `apply`.

    Each answer names the combination's `worker`, the `overlay` its settings
    lay, the `profile` it moves to and whether that profile is `created` (or,
    in a dry run, would be); `error` instead when the combination does not make
    a profile a turn can run on, in which case nothing names it. A derived name
    already held by a different spec, a bundled or a global profile takes the
    digest form instead, so a migration never changes a profile it did not
    write."""
    lib = runner()
    if apply:
        from capabilities_contract.db import migrate
        steps = store_steps()
        migrate(conn, steps["owner"], [tuple(step) for step in steps["steps"]],
                major=steps["major"], minor=steps["minor"])
        conn.commit()
    active = stored(conn, project, schema)
    taken: dict[str, dict] = {}
    out = []
    try:
        for combo in combos:
            worker = combo.get("worker")
            if worker == STUB:
                out.append({"worker": STUB, "overlay": {}, "profile": STUB, "created": False})
                continue
            overlay = implicit_knobs(worker, combo.get("settings"))
            answer = {"worker": worker, "overlay": overlay}
            try:
                found = implicit(worker, combo.get("settings"), active, project)
            except Refusal as refused:
                out.append({**answer, "profile": None, "error": refused.message})
                continue
            if not overlay:
                out.append({**answer, "profile": PREFIX + worker, "created": False})
                continue
            spec = lib.check_spec({key: value for key, value in found["knobs"].items()},
                                  f"profile for {worker} {sorted(overlay)}")
            name = derived_name(worker, overlay)
            for candidate in (name, f"{PREFIX}{worker}-{_digest(worker, overlay)}"):
                held = next((one for one in active if one.name == candidate and one.active
                             and (one.scope == "global" or one.scope_id == project)), None)
                if candidate in BUNDLED or (held is not None and (
                        held.scope == "global" or held.spec != spec)):
                    continue
                if candidate in taken and taken[candidate] != spec:
                    continue
                name = candidate
                break
            else:
                out.append({**answer, "profile": None,
                            "error": f"every name for {worker} {sorted(overlay)} is held by "
                                     "another profile"})
                continue
            exists = name in taken or any(one.name == name and one.active and one.scope_id
                                          == project for one in active)
            taken[name] = spec
            created = not exists
            if apply and created:
                _, created = lib.save_profile(conn, name, spec, scope="project",
                                              scope_id=project, created_by=created_by,
                                              schema=schema)
            out.append({**answer, "profile": name, "created": created})
        if apply:
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return out


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
        return connect(application_name="telegram", setting=setting,
                       transaction=True), setting.schema
    except DbError as exc:
        raise Refusal(5, exc.slug, exc.message, exc.hint) from None


def _reachable(project_id: str | None, project_root: str | None) -> tuple[list, str | None]:
    """What `stored` answers for the project, or none and why when the store
    cannot be read: a check or a probe still answers from the bundled layer,
    which is all a turn reaches while the store is down."""
    try:
        conn, schema = _connect(project_root)
    except Refusal as refused:
        return [], refused.message
    try:
        return stored(conn, project_id, schema), None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="profiles.py")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("store-steps")
    # Both read their input as JSON on stdin: `check` the settings document and
    # the environment, `probe` the names with their positions, the timeout and
    # whether to run the live turn.
    for verb in ("check", "probe"):
        one = commands.add_parser(verb)
        one.add_argument("--project-id")
        one.add_argument("--project-root")
    for verb in ("list", "show", "resolve", "save", "materialize"):
        one = commands.add_parser(verb)
        if verb not in ("list", "materialize"):
            one.add_argument("name")
        one.add_argument("--project-id", required=True)
        one.add_argument("--project-root")
        if verb in ("save", "materialize"):
            one.add_argument("--created-by")
        if verb == "save":
            one.add_argument("--where", default="stdin")
            one.add_argument("--base")
        if verb == "materialize":
            # The combinations, as JSON on stdin.
            one.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "store-steps":
            answer = store_steps()
        elif args.command in ("check", "probe"):
            given = json.loads(sys.stdin.read() or "{}")
            active, store_error = _reachable(args.project_id, args.project_root)
            if args.command == "check":
                problems = check(given.get("settings"), given.get("environ") or {},
                                 active, args.project_id)
                answer = {"ok": not problems, "problems": problems,
                          "store_error": store_error}
            else:
                with tempfile.TemporaryDirectory(prefix="telegram-doctor-") as scratch:
                    rows = probe(given.get("profiles") or [],
                                 float(given.get("timeout") or 90),
                                 active, args.project_id, scratch,
                                 live=bool(given.get("live")))
                answer = {"rows": rows, "store_error": store_error}
        else:
            conn, schema = _connect(args.project_root)
            try:
                if args.command == "save":
                    answer = save(conn, args.name, sys.stdin.read(), args.where,
                                  args.project_id, base=args.base,
                                  created_by=args.created_by, schema=schema)
                elif args.command == "materialize":
                    given = json.loads(sys.stdin.read() or "{}")
                    answer = {"profiles": materialize(
                        conn, given.get("combos") or [], args.project_id,
                        apply=args.apply, created_by=args.created_by, schema=schema)}
                elif args.command == "resolve":
                    # What a job registered on `name` runs on: the profile
                    # resolved and fitted, or the refusal saying why not.
                    answer = describe(resolve(args.name, stored(conn, args.project_id, schema),
                                              args.project_id))
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
