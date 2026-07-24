"""Validation for project-local Slack service settings.

The daemon and the CLI both use this module so operational policy has one
parser and one fail-closed verdict.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

from authority import allowed_capability_names

WORKERS = {"claude", "codex", "stub"}
WORKSPACE_MODES = {"read_only", "workspace_write"}
MIN_CLAUDE_VERSION = (2, 1, 216)
_DOMAIN = re.compile(
    r"^(?:\*\.)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)


def _mapping(value, path, problems):
    if not isinstance(value, dict):
        problems.append(f"{path} must be an object")
        return {}
    return value


def _string_list(value, path, problems):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        problems.append(f"{path} must be a list of strings")
        return []
    return value


def _bounded_number(value, path, minimum, maximum, problems):
    try:
        number = float(value)
    except (TypeError, ValueError):
        problems.append(f"{path} must be a number")
        return
    if number < minimum or number > maximum:
        problems.append(f"{path} must be between {minimum} and {maximum}")


def _worker_version(name):
    try:
        result = subprocess.run(
            [name, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout + result.stderr)
    return tuple(map(int, match.groups())) if match else None


def validate_settings(settings, *, project_root=None, check_worker=False) -> list[str]:
    """Return every settings problem; an empty list means safe to start."""
    problems: list[str] = []
    if not isinstance(settings, dict):
        return ["settings must be a JSON object"]

    connection = settings.get("connection")
    if connection is not None and (
        not isinstance(connection, str) or not connection.strip()
    ):
        problems.append("connection must be null or a non-empty string")

    direct = _mapping(settings.get("direct_messages", {}), "direct_messages", problems)
    if direct.get("mode", "allowed_users") not in {"allowed_users", "open"}:
        problems.append("direct_messages.mode must be allowed_users or open")

    users = _mapping(settings.get("allowed_users", {}), "allowed_users", problems)
    channels = _mapping(
        settings.get("allowed_channels", {}), "allowed_channels", problems
    )
    channel_policy = settings.get("default_channel_policy", "allowed_only")
    if channel_policy not in {"allowed_only", "open"}:
        problems.append("default_channel_policy must be allowed_only or open")

    auto = _mapping(settings.get("auto_answer", {}), "auto_answer", problems)
    auto_users = _string_list(auto.get("users", []), "auto_answer.users", problems)
    auto_channels = _string_list(
        auto.get("channels", []), "auto_answer.channels", problems
    )
    if direct.get("mode", "allowed_users") != "open":
        unknown = sorted(set(auto_users) - set(users))
        if unknown:
            problems.append(
                "auto_answer.users are not admitted by allowed_users: "
                + ", ".join(unknown)
            )
    if channel_policy != "open":
        unknown = sorted(set(auto_channels) - set(channels))
        if unknown:
            problems.append(
                "auto_answer.channels are not admitted by allowed_channels: "
                + ", ".join(unknown)
            )

    control = _mapping(settings.get("control", {}), "control", problems)
    _mapping(control.get("roles", {}), "control.roles", problems)
    observability = _mapping(
        settings.get("observability", {}), "observability", problems
    )
    if not isinstance(observability.get("log_message_snippets", False), bool):
        problems.append("observability.log_message_snippets must be boolean")

    authority = _mapping(settings.get("authority", {}), "authority", problems)
    roles = _mapping(authority.get("roles", {}), "authority.roles", problems)
    granted_capabilities: set[str] = set()
    for role, policy in roles.items():
        policy = _mapping(policy, f"authority.roles.{role}", problems)
        domains = _string_list(
            policy.get("network_domains", []),
            f"authority.roles.{role}.network_domains",
            problems,
        )
        invalid_domains = sorted(
            {
                domain
                for domain in domains
                if domain == "*" or not _DOMAIN.fullmatch(domain)
            }
        )
        if invalid_domains:
            problems.append(
                f"authority.roles.{role}.network_domains contains invalid or "
                f"overbroad domains: {', '.join(invalid_domains)}"
            )
        try:
            granted_capabilities.update(
                allowed_capability_names(policy.get("allowed_capabilities", {}))
            )
        except ValueError as exc:
            problems.append(f"authority.roles.{role}.allowed_capabilities: {exc}")

    defaults = _mapping(settings.get("defaults", {}), "defaults", problems)
    worker = str(defaults.get("worker") or "stub").strip().lower()
    if worker not in WORKERS:
        problems.append(f"defaults.worker must be one of {sorted(WORKERS)}")
    elif check_worker and worker != "stub" and shutil.which(worker) is None:
        problems.append(f"defaults.worker executable is not on PATH: {worker}")
    elif check_worker and worker == "claude":
        version = _worker_version("claude")
        if version is None or version < MIN_CLAUDE_VERSION:
            required = ".".join(map(str, MIN_CLAUDE_VERSION))
            actual = ".".join(map(str, version)) if version else "unknown"
            problems.append(
                f"claude worker requires Claude Code >= {required} for strict "
                f"sandbox controls (found {actual})"
            )
    if worker == "codex" and granted_capabilities:
        problems.append(
            "authority roles cannot grant capabilities to the codex worker: "
            "its fail-closed sandbox cannot safely provide networked capability CLIs; "
            "use claude or keep capability authority empty"
        )
    if check_worker and worker == "claude":
        missing_capabilities = sorted(
            name for name in granted_capabilities if shutil.which(name) is None
        )
        if missing_capabilities:
            problems.append(
                "authorized capability executables are not on PATH: "
                + ", ".join(missing_capabilities)
            )
    if worker != "stub" and defaults.get("trusted_ingress") is not True:
        problems.append(
            "defaults.trusted_ingress must be true for claude/codex workers; "
            "admitted Slack senders can direct the worker inside the configured workspace"
        )
    worker_home = defaults.get("worker_home")
    if worker != "stub":
        if not isinstance(worker_home, str) or not worker_home.strip():
            problems.append(
                "defaults.worker_home must name a dedicated existing directory "
                "for claude/codex authentication and state"
            )
        elif not Path(worker_home).expanduser().resolve().is_dir():
            problems.append("defaults.worker_home does not exist")
        else:
            resolved_worker_home = Path(worker_home).expanduser().resolve()
            if resolved_worker_home == Path.home().resolve():
                problems.append(
                    "defaults.worker_home must not be the operator's general home directory"
                )
            try:
                worker_home_stat = resolved_worker_home.stat()
                if worker_home_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                    problems.append(
                        "defaults.worker_home must not be accessible by group or other users"
                    )
                if hasattr(os, "getuid") and worker_home_stat.st_uid != os.getuid():
                    problems.append(
                        "defaults.worker_home must be owned by the service user"
                    )
            except OSError as exc:
                problems.append(f"defaults.worker_home cannot be inspected: {exc}")

    workspace_mode = str(defaults.get("workspace_mode") or "read_only").strip().lower()
    if workspace_mode not in WORKSPACE_MODES:
        problems.append(
            f"defaults.workspace_mode must be one of {sorted(WORKSPACE_MODES)}"
        )

    configured_project = defaults.get("project")
    root = Path(project_root).resolve() if project_root else None
    project = Path(configured_project).expanduser() if configured_project else root
    if project is None or not project.resolve().is_dir():
        problems.append("defaults.project must resolve to an existing directory")

    _bounded_number(
        defaults.get("tail_size", 30), "defaults.tail_size", 1, 200, problems
    )
    _bounded_number(
        defaults.get("worker_timeout", 180),
        "defaults.worker_timeout",
        1,
        3600,
        problems,
    )
    _bounded_number(
        defaults.get("max_parallel_jobs", 3),
        "defaults.max_parallel_jobs",
        1,
        16,
        problems,
    )
    catch_up = _mapping(defaults.get("catch_up", {}), "defaults.catch_up", problems)
    _bounded_number(
        catch_up.get("max_age_seconds", 3600),
        "defaults.catch_up.max_age_seconds",
        0,
        604800,
        problems,
    )
    _bounded_number(
        catch_up.get("max_messages", 50),
        "defaults.catch_up.max_messages",
        1,
        1000,
        problems,
    )
    return problems


def require_valid_settings(settings, *, project_root=None, check_worker=False) -> None:
    problems = validate_settings(
        settings, project_root=project_root, check_worker=check_worker
    )
    if problems:
        raise ValueError("; ".join(problems))
