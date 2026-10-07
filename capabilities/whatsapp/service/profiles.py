#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "callva-harness-runner==0.8.0",
# ]
# ///
"""The profiles a WhatsApp dialogue turn or job may run on, and whether one fits.

A profile says how the model runs; it is a callva-harness-runner profile file,
found by the runner's own discovery with this service's folders first: the
project's `capabilities/whatsapp/service/profiles/`, then the bundle's
`service/profiles/`, then the machine folder, then the runner's shipped set.
The first file found is used whole. The bundle ships `whatsapp-claude` and
`whatsapp-codex` for dialogue turns and `whatsapp-job-claude` and
`whatsapp-job-codex` for jobs; a project file of the same name shadows any.

Fit is what the service needs of a profile to work, checked after the runner
has read and validated the file: free text it can cut at the reply marker, and
the full access a worker has today. Anything narrower is out of fit until a
narrower profile is proven to carry the turn.

The listener imports this module in-process. The CLI does not carry the runner
in its own dependency header, so `service start` and `service doctor` run this
file as a script (`check NAME... --folder DIR...`) and read its JSON answer.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RUNNER_PIN = "callva-harness-runner==0.8.0"
DEFAULT_PROFILE = "whatsapp-claude"
DEFAULT_JOB_PROFILE = "whatsapp-job-claude"
BUNDLED = ("whatsapp-claude", "whatsapp-codex", "whatsapp-job-claude",
           "whatsapp-job-codex")
HERE = Path(__file__).resolve().parent


def runner():
    """The harness library, imported where a profile is read or a turn runs."""
    from callva import harness_runner
    return harness_runner


def folders(project_root: Path | str | None, bundle_service: Path | str | None = None) -> list[str]:
    """The caller folders the runner searches first, in order."""
    out = []
    if project_root:
        out.append(str(Path(project_root) / "capabilities" / "whatsapp" / "service"
                       / "profiles"))
    out.append(str(Path(bundle_service or HERE) / "profiles"))
    return out


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


def resolve(name: str, search: list[str]):
    """One name to (Profile, origin), or raise ValueError naming the name or the
    file and why. The runner reads and validates the file; fit is checked here."""
    lib = runner()
    try:
        found = lib.find_profile_file(name, search)
        knobs = found.knobs()
        profile = found.profile()
    except lib.ProfileNotFound as exc:
        raise ValueError(f"profile {name!r} is not found: {exc}") from None
    except (lib.ProfileError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"profile {name!r} is refused by the harness runner: {exc}") \
            from None
    origin = {"name": name, "source": found.source, "path": found.path,
              "harness": knobs.get("harness"), "model": knobs.get("model"),
              "shadows": list(found.shadows)}
    problems = fit_problems(knobs)
    if problems:
        raise ValueError(f"profile {name!r} at {found.path} does not fit a WhatsApp "
                         "dialogue turn: " + "; ".join(problems))
    return profile, origin


def check(names: list[str], search: list[str]) -> list[dict]:
    """Every name's verdict, without stopping at the first refusal."""
    rows = []
    for name in names:
        try:
            _profile, origin = resolve(name, search)
            rows.append({"name": name, "ok": True, **origin})
        except ValueError as exc:
            rows.append({"name": name, "ok": False, "error": str(exc)})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="profiles.py")
    sub = parser.add_subparsers(dest="verb", required=True)
    one = sub.add_parser("check", help="resolve and fit-check profiles by name")
    one.add_argument("names", nargs="+")
    one.add_argument("--folder", action="append", default=[])
    listing = sub.add_parser("list", help="every profile visible from the folders")
    listing.add_argument("--folder", action="append", default=[])
    args = parser.parse_args(argv)
    if args.verb == "check":
        rows = check(args.names, args.folder)
        print(json.dumps({"ok": all(r["ok"] for r in rows), "profiles": rows},
                         ensure_ascii=False))
        return 0 if all(r["ok"] for r in rows) else 6
    rows = []
    for item in runner().list_profiles(args.folder):
        rows.append({"name": item.name, "source": item.source, "path": item.path,
                     "harness": item.harness, "error": item.error})
    print(json.dumps({"profiles": rows}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
