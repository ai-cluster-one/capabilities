# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "callva-harness-runner==0.13.1",
# ]
# ///
"""The profiles a Slack dialogue turn or job may run on, and whether one fits.

A profile says how the model runs; it is a callva-harness-runner profile kept
in the store, in the `harness_profile_versions` table the runner owns. A name resolves
as agentrooms and tasks resolve it: the project's active version, then the
global one, then the runner's shipped profiles; then the four this bundle
ships in `service/profiles/` - `slacker-claude` and `slacker-codex` for
dialogue turns, `slacker-job-claude` and `slacker-job-codex` for jobs. The
version found is used whole.

Fit is what the service needs of a profile to work, checked after the runner
has validated it: free text it can cut at the reply marker, and the full access
a worker has today. Anything narrower is out of fit until a narrower profile is
proven to carry the turn.

The listener imports this module in-process. The CLI does not carry the runner
in its own dependency header; this file's header is where the runner is
pinned, and the CLI runs itself again under that pin wherever it needs it.
"""

from __future__ import annotations

from pathlib import Path

RUNNER_PIN = "callva-harness-runner==0.13.1"
DEFAULT_PROFILE = "slacker-claude"
DEFAULT_JOB_PROFILE = "slacker-job-claude"
BUNDLED = ("slacker-claude", "slacker-codex", "slacker-job-claude",
           "slacker-job-codex")
HERE = Path(__file__).resolve().parent


def runner():
    """The harness library, imported where a profile is read or a turn runs."""
    from callva import harness_runner
    return harness_runner


def stored(conn, project: str | None, schema: str | None = None) -> list:
    """Every active profile version a lookup from the project reaches, as the
    runner reads them. A store whose table does not exist yet holds none."""
    try:
        return runner().stored_profiles(conn, project=project, schema=schema)
    except Exception as exc:
        if type(exc).__name__ == "UndefinedTable":
            if not getattr(conn, "autocommit", True):
                conn.rollback()
            return []
        raise


def bundled_file(name: str, bundle_service: Path | str | None = None) -> Path:
    return Path(bundle_service or HERE) / "profiles" / f"{name}.toml"


def fit_problems(knobs: dict) -> list[str]:
    """Why a profile does not fit a dialogue turn, one sentence per knob; empty
    when it fits."""
    problems = []
    harness = knobs.get("harness")
    if knobs.get("output_schema") is not None:
        problems.append("output_schema is set; the service reads free text and cuts it "
                        "at the reply marker")
    if harness == "claude":
        tools = knobs.get("tools")
        if tools is not None and "Bash" not in tools:
            problems.append("tools leaves out Bash; a worker runs capability "
                            "commands through it")
        if "Bash" in (knobs.get("disallowed_tools") or []):
            problems.append("disallowed_tools names Bash; a worker runs capability "
                            "commands through it")
        if knobs.get("permission_mode") != "bypassPermissions":
            problems.append("permission_mode must be bypassPermissions; a turn has "
                            "nobody to answer a permission prompt")
        settings = knobs.get("settings")
        if isinstance(settings, dict) and (settings.get("sandbox") or {}).get("enabled"):
            problems.append("settings enables the sandbox; a worker needs the access "
                            "the service gives it today")
    elif harness == "codex":
        config = knobs.get("codex_config") or {}
        if config.get("sandbox_mode") != "danger-full-access":
            problems.append("codex_config.sandbox_mode must be danger-full-access; a "
                            "worker needs the access the service gives it today")
        if config.get("permissions") or config.get("default_permissions") \
                or config.get("permission_profile"):
            problems.append("codex_config names a permissions profile; a worker needs "
                            "the access the service gives it today")
        if knobs.get("approval_policy") != "never":
            problems.append("approval_policy must be never; a turn has nobody to "
                            "approve an escalation")
    else:
        problems.append(f"harness {harness!r} is not one the runner drives")
    return problems


def resolve(name: str, active: list, project: str | None,
            bundle_service: Path | str | None = None):
    """One name to (Profile, origin), or raise ValueError naming the name, where
    it resolved and why. `active` is what `stored` answered for the project.
    The origin names the layer it came from - `project`, `global`, `shipped`
    (the runner's) or `capability` (this bundle's) - and the stored version."""
    lib = runner()
    shipped = bundled_file(name, bundle_service)
    try:
        found = lib.resolve_among(name, active, project=project)
        knobs, where = dict(found.spec), found.where
        shadows = list(found.shadows) + (["capability"] if shipped.is_file() else [])
        origin = {"name": name, "layer": found.layer, "version": found.id,
                  "shadows": shadows}
    except lib.ProfileNotFound as exc:
        if not shipped.is_file():
            raise ValueError(f"profile {name!r} is not found: {exc}; nor does "
                             "slacker ship one") from None
        try:
            knobs = lib.ProfileFile(name, "shipped", str(shipped), shipped.read_text()).knobs()
        except Exception as exc:
            raise ValueError(f"profile {name!r} at {shipped} is refused by the harness "
                             f"runner: {exc}") from None
        where = str(shipped)
        origin = {"name": name, "layer": "capability", "version": None, "path": where,
                  "shadows": []}
    except (lib.ProfileError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"profile {name!r} is refused by the harness runner: {exc}") \
            from None
    try:
        profile = lib.Profile.from_dict(knobs)
    except (lib.ProfileError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"profile {name!r} ({where}) is refused by the harness "
                         f"runner: {exc}") from None
    origin.update(harness=knobs.get("harness"), model=knobs.get("model"))
    problems = fit_problems(knobs)
    if problems:
        raise ValueError(f"profile {name!r} ({where}) does not fit a Slack "
                         "dialogue turn: " + "; ".join(problems))
    return profile, origin


def check(names: list[str], active: list, project: str | None) -> list[dict]:
    """Every name's verdict, without stopping at the first refusal."""
    rows = []
    for name in names:
        try:
            _profile, origin = resolve(name, active, project)
            rows.append({**origin, "ok": True})
        except ValueError as exc:
            rows.append({"name": name, "ok": False, "error": str(exc)})
    return rows
