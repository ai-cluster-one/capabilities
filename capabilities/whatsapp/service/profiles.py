# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "callva-harness-runner==0.10.1",
# ]
# ///
"""The profiles a WhatsApp dialogue turn or job may run on, and whether one fits.

A profile says how the model runs; it is a callva-harness-runner profile kept
in the store, in the `harness_profiles` table the runner owns. A name resolves
as agentrooms and tasks resolve it: the project's active version, then the
global one, then the runner's shipped profiles; then the four this bundle
ships in `service/profiles/` - `whatsapp-claude` and `whatsapp-codex` for
dialogue turns, `whatsapp-job-claude` and `whatsapp-job-codex` for jobs. The
version found is used whole.

A project's profile files in `capabilities/whatsapp/service/profiles/` are
not read. While any is there the service refuses to start; `import_files`
carries them into the store at the project's scope, all or none, and removes
them.

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

RUNNER_PIN = "callva-harness-runner==0.10.1"
DEFAULT_PROFILE = "whatsapp-claude"
DEFAULT_JOB_PROFILE = "whatsapp-job-claude"
BUNDLED = ("whatsapp-claude", "whatsapp-codex", "whatsapp-job-claude",
           "whatsapp-job-codex")
HERE = Path(__file__).resolve().parent


def runner():
    """The harness library, imported where a profile is read or a turn runs."""
    from callva import harness_runner
    return harness_runner


def project_folder(project_root: Path | str) -> Path:
    """Where a project kept its profile files before they moved into the store."""
    return Path(project_root) / "capabilities" / "whatsapp" / "service" / "profiles"


def unimported(project_root: Path | str | None) -> list[Path]:
    """The project's profile files not yet carried into the store."""
    if not project_root:
        return []
    folder = project_folder(project_root)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix == ".toml" and p.is_file())


def unimported_problem(project_root: Path | str | None) -> str | None:
    """Why the project's profiles cannot be resolved yet, or None."""
    files = unimported(project_root)
    if not files:
        return None
    return (f"profile files are no longer read: {', '.join(str(p) for p in files)}; "
            "`whatsapp profiles import --apply` saves them in the store at this "
            "project's scope and removes them")


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
                             "whatsapp ship one") from None
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
        raise ValueError(f"profile {name!r} ({where}) does not fit a WhatsApp "
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


def import_plan(project_root: Path | str, active: list, project: str) -> list[dict]:
    """What importing the project's files would do, one row per file: `save`, as
    the profile's first version at the project's scope; `held`, the store
    already holds exactly it; or `refused`, with the reason."""
    lib = runner()
    held = {one.name: one for one in active
            if one.scope == "project" and one.scope_id == project and one.active}
    rows = []
    for path in unimported(project_root):
        name = path.stem
        row = {"name": name, "file": str(path)}
        try:
            lib.check_profile_name(name)
            knobs = lib.ProfileFile(name, "folder", str(path), path.read_text()).knobs()
            spec = lib.check_spec(knobs, f"profile {name!r} at {path}")
        except (OSError, UnicodeDecodeError, lib.ProfileError, ValueError, TypeError,
                KeyError) as exc:
            rows.append({**row, "action": "refused", "error": str(exc)})
            continue
        row["spec"] = spec
        version = held.get(name)
        if version is None:
            rows.append({**row, "action": "save"})
        elif version.spec == spec:
            rows.append({**row, "action": "held", "version": version.id})
        else:
            rows.append({**row, "action": "refused", "version": version.id,
                         "error": f"the project already holds version {version.id} of "
                                  f"{name!r}, which differs from {path}"})
    return rows


def import_apply(conn, plan: list[dict], project: str, *, schema: str | None = None,
                 created_by: str | None = None) -> list[dict]:
    """Save every planned file inside the caller's transaction; the caller
    commits, then removes the files. Raises ValueError when another writer got
    there first, so nothing is saved."""
    lib = runner()
    out = []
    for row in plan:
        try:
            current = lib.active_profile(conn, row["name"], scope="project",
                                         scope_id=project, schema=schema, lock=True)
            if current is not None and current.spec != row["spec"]:
                raise ValueError(f"the project already holds version {current.id} of "
                                 f"{row['name']!r}, which differs from {row['file']}")
            version, created = lib.save_profile(
                conn, row["name"], row["spec"], scope="project", scope_id=project,
                created_by=created_by, schema=schema)
        except lib.ProfileError as exc:
            raise ValueError(str(exc)) from None
        out.append({"name": row["name"], "file": row["file"], "version": version.id,
                    "created": created})
    return out
