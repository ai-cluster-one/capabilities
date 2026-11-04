"""The profiles a telegrambot turn, job or reply-gate run may run on.

A profile is a callva-harness-runner profile kept in the store, in the
`harness_profiles` table the runner owns. Every telegrambot profile is named
`telegrambot-<something>`; a name resolves to the project's active version, then
the global one, then the ones this bundle ships in `service/profiles/`:
`telegrambot-claude` and `telegrambot-codex` for dialogue turns,
`telegrambot-job-claude` and `telegrambot-job-codex` for jobs, and the reply
gate's `telegrambot-gate-claude` and `telegrambot-gate-codex`.

Out of the box a turn runs on the harness the machine is signed in to - claude
where its CLI is on PATH, else codex - so a bot answers at once rather than
from a stub.

Fit is what a turn needs of a profile: free text it can cut at the reply
marker, and nobody to ask for permission. The gate's fit is the gate module's.
"""

from __future__ import annotations

import shutil
from pathlib import Path

PREFIX = "telegrambot-"
HERE = Path(__file__).resolve().parent
BUNDLED = ("telegrambot-claude", "telegrambot-codex", "telegrambot-job-claude",
           "telegrambot-job-codex", "telegrambot-gate-claude", "telegrambot-gate-codex")
GATE_PROFILES = {"claude": "telegrambot-gate-claude", "codex": "telegrambot-gate-codex"}


def runner():
    from callva import harness_runner
    return harness_runner


def signed_in_harness(which=shutil.which) -> str:
    """The harness a turn runs on when the settings name no profile."""
    if which("claude"):
        return "claude"
    if which("codex"):
        return "codex"
    return "claude"


def default_profile(which=shutil.which) -> str:
    return f"{PREFIX}{signed_in_harness(which)}"


def default_job_profile(which=shutil.which) -> str:
    return f"{PREFIX}job-{signed_in_harness(which)}"


def stored(conn, project: str | None, schema: str | None = None) -> list:
    """Every active profile version a lookup from the project reaches. A store
    whose profile table does not exist yet holds none."""
    try:
        return runner().stored_profiles(conn, project=project, schema=schema)
    except Exception as exc:
        if type(exc).__name__ == "UndefinedTable":
            return []
        raise


def bundled_file(name: str) -> Path:
    return HERE / "profiles" / f"{name}.toml"


def fit_problems(knobs: dict) -> list[str]:
    """Why a profile does not fit a turn or a job, one sentence per knob."""
    problems = []
    harness = knobs.get("harness")
    if knobs.get("output_schema") is not None:
        problems.append("output_schema is set; the service reads free text and cuts it at "
                        "the reply marker")
    if harness == "claude":
        if knobs.get("permission_mode") != "bypassPermissions":
            problems.append("permission_mode must be bypassPermissions; a turn has nobody to "
                            "answer a permission prompt")
        tools = knobs.get("tools")
        if tools is not None and "Bash" not in tools:
            problems.append("tools leaves out Bash; a turn runs capability commands through it")
    elif harness == "codex":
        if knobs.get("approval_policy") != "never":
            problems.append("approval_policy must be never; a turn has nobody to approve")
    else:
        problems.append(f"harness {harness!r} is not one the runner drives")
    return problems


def knobs_of(name: str, active: list, project: str | None) -> tuple[dict, dict]:
    """(knobs, origin) of one name, unfitted; ValueError naming why it does not
    resolve."""
    if not name.startswith(PREFIX):
        raise ValueError(f"profile {name!r} is not a telegrambot profile; its name starts "
                         f"with {PREFIX}")
    lib = runner()
    shipped = bundled_file(name)
    try:
        found = lib.resolve_among(name, active, project=project)
        return dict(found.spec), {"name": name, "layer": found.layer, "version": found.id,
                                  "where": found.where if hasattr(found, "where") else None}
    except lib.ProfileNotFound as exc:
        if not shipped.is_file():
            raise ValueError(f"profile {name!r} is not found: {exc}; nor does telegrambot "
                             "ship one") from None
        try:
            knobs = lib.ProfileFile(name, "shipped", str(shipped), shipped.read_text()).knobs()
        except Exception as exc:
            raise ValueError(f"profile {name!r} at {shipped} is refused by the harness "
                             f"runner: {exc}") from None
        return knobs, {"name": name, "layer": "capability", "version": None,
                       "where": str(shipped)}
    except (lib.ProfileError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"profile {name!r} is refused by the harness runner: {exc}") from None


def resolve(name: str, active: list, project: str | None, *, fit=fit_problems):
    """(Profile, origin) for a name that resolves and fits, else ValueError."""
    lib = runner()
    knobs, origin = knobs_of(name, active, project)
    try:
        profile = lib.Profile.from_dict(knobs)
    except Exception as exc:
        raise ValueError(f"profile {name!r} ({origin['layer']}) is refused by the harness "
                         f"runner: {exc}") from None
    problems = fit(knobs)
    if problems:
        raise ValueError(f"profile {name!r} ({origin['layer']}) does not fit: "
                         + "; ".join(problems))
    origin.update(harness=knobs.get("harness"), model=knobs.get("model"),
                  effort=knobs.get("effort"), timeout_seconds=knobs.get("timeout_seconds"))
    return profile, origin, knobs


def listing(active: list, project: str | None) -> list[dict]:
    """Every telegrambot profile this project reaches, with whether it fits a turn.
    No env_set value and no settings object: a group may read this."""
    names = set(BUNDLED) | {p.name for p in active if p.name.startswith(PREFIX)}
    rows = []
    for name in sorted(names):
        try:
            knobs, origin = knobs_of(name, active, project)
        except ValueError as exc:
            rows.append({"name": name, "ok": False, "error": str(exc)})
            continue
        problems = fit_problems(knobs)
        rows.append({"name": name, "layer": origin["layer"], "version": origin["version"],
                     "harness": knobs.get("harness"), "model": knobs.get("model"),
                     "effort": knobs.get("effort"),
                     "timeout_seconds": knobs.get("timeout_seconds"),
                     "gate": name in GATE_PROFILES.values(),
                     "fits_turn": not problems, **({"why_not": problems} if problems else {})})
    return rows
