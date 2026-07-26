"""Validation for project-local Slack service settings.

The service intentionally mirrors Telegram's trusted-worker model: Claude and
Codex may use their full host tool surface, while capability CLIs enforce the
request-scoped authority envelope. Validation therefore checks configuration
shape and operational bounds, not a sandbox policy.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from authority import allowed_capability_names

WORKERS = {"claude", "codex", "stub"}
CONTROL_COMMANDS = {"help", "set", "status", "stop"}
WORKER_FIELDS = {
    "claude": {"model", "effort"},
    "codex": {"model", "reasoning_effort", "service_tier"},
    "stub": {"model"},
}
CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
CODEX_REASONING = {"low", "medium", "high", "xhigh"}
CODEX_TIERS = {"fast", "priority"}


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


def _validate_authority_row(row, path, problems):
    if not isinstance(row, dict):
        return
    caps = row.get("allowed_capabilities", row.get("capabilities"))
    if caps is not None:
        if not (isinstance(caps, (bool, list, dict)) or caps == "*"):
            problems.append(
                f"{path}.allowed_capabilities must be a boolean, '*', list, or object"
            )
        try:
            allowed_capability_names(caps)
        except ValueError as exc:
            problems.append(f"{path}.allowed_capabilities: {exc}")
    authority = row.get("authority")
    if authority is not None:
        authority = _mapping(authority, f"{path}.authority", problems)
        caps = authority.get("allowed_capabilities", authority.get("capabilities"))
        if caps is not None:
            try:
                allowed_capability_names(caps)
            except ValueError as exc:
                problems.append(f"{path}.authority.allowed_capabilities: {exc}")


def _validate_control_row(row, path, problems):
    if not isinstance(row, dict) or "control" not in row:
        return
    control = _mapping(row.get("control"), f"{path}.control", problems)
    _validate_commands(control.get("commands"), f"{path}.control.commands", problems)


def _validate_commands(value, path, problems):
    if value is None:
        return
    if value is True or value == "*":
        return
    commands = _string_list(value, path, problems)
    invalid = sorted(
        {
            command
            for command in commands
            if command.strip().lower().lstrip("/") not in CONTROL_COMMANDS
        }
    )
    if invalid:
        problems.append(f"{path} contains unknown commands: {', '.join(invalid)}")


def _validate_role(value, path, problems):
    if value is not None and (not isinstance(value, str) or not value.strip()):
        problems.append(f"{path} must be a non-empty string")


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
    assistant_name = settings.get("assistant_name", "Assistant")
    if not isinstance(assistant_name, str) or not assistant_name.strip():
        problems.append("assistant_name must be a non-empty string")

    direct = _mapping(settings.get("direct_messages", {}), "direct_messages", problems)
    if direct.get("mode", "allowed_users") not in {"allowed_users", "open"}:
        problems.append("direct_messages.mode must be allowed_users or open")
    _validate_role(direct.get("default_role"), "direct_messages.default_role", problems)

    users = _mapping(settings.get("allowed_users", {}), "allowed_users", problems)
    channels = _mapping(
        settings.get("allowed_channels", {}), "allowed_channels", problems
    )
    for user_id, row in users.items():
        _validate_authority_row(row, f"allowed_users.{user_id}", problems)
        if isinstance(row, dict):
            _validate_role(row.get("role"), f"allowed_users.{user_id}.role", problems)
            _validate_control_row(row, f"allowed_users.{user_id}", problems)
    for channel_id, row in channels.items():
        _validate_authority_row(row, f"allowed_channels.{channel_id}", problems)
        if isinstance(row, dict):
            _validate_role(
                row.get("default_role"),
                f"allowed_channels.{channel_id}.default_role",
                problems,
            )
            _validate_control_row(row, f"allowed_channels.{channel_id}", problems)
        members = row.get("members", {}) if isinstance(row, dict) else {}
        if members is not None:
            members = _mapping(
                members, f"allowed_channels.{channel_id}.members", problems
            )
            for user_id, member in members.items():
                _validate_authority_row(
                    member,
                    f"allowed_channels.{channel_id}.members.{user_id}",
                    problems,
                )
                if isinstance(member, dict):
                    _validate_role(
                        member.get("role"),
                        f"allowed_channels.{channel_id}.members.{user_id}.role",
                        problems,
                    )
                    _validate_control_row(
                        member,
                        f"allowed_channels.{channel_id}.members.{user_id}",
                        problems,
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
    control_roles = _mapping(control.get("roles", {}), "control.roles", problems)
    for role, policy in control_roles.items():
        policy = _mapping(policy, f"control.roles.{role}", problems)
        _validate_commands(
            policy.get("commands"), f"control.roles.{role}.commands", problems
        )
    observability = _mapping(
        settings.get("observability", {}), "observability", problems
    )
    if not isinstance(observability.get("log_message_snippets", False), bool):
        problems.append("observability.log_message_snippets must be boolean")

    authority = _mapping(settings.get("authority", {}), "authority", problems)
    _validate_authority_row(authority.get("default", {}), "authority.default", problems)
    roles = _mapping(authority.get("roles", {}), "authority.roles", problems)
    for role, policy in roles.items():
        policy = _mapping(policy, f"authority.roles.{role}", problems)
        _validate_authority_row(policy, f"authority.roles.{role}", problems)

    defaults = _mapping(settings.get("defaults", {}), "defaults", problems)
    worker = str(defaults.get("worker") or "stub").strip().lower()
    if worker not in WORKERS:
        problems.append(f"defaults.worker must be one of {sorted(WORKERS)}")
    elif check_worker and worker != "stub" and shutil.which(worker) is None:
        problems.append(f"defaults.worker executable is not on PATH: {worker}")

    workers = _mapping(defaults.get("workers", {}), "defaults.workers", problems)
    for name, profile in workers.items():
        if name not in WORKERS:
            problems.append(f"defaults.workers contains unknown worker: {name}")
            continue
        profile = _mapping(profile, f"defaults.workers.{name}", problems)
        for field, value in profile.items():
            if field not in WORKER_FIELDS[name]:
                problems.append(f"defaults.workers.{name} has unknown field: {field}")
                continue
            if value is not None and not isinstance(value, str):
                problems.append(
                    f"defaults.workers.{name}.{field} must be null or string"
                )
        if name == "claude" and profile.get("effort") not in {
            None,
            *CLAUDE_EFFORTS,
        }:
            problems.append(
                "defaults.workers.claude.effort must be null, low, medium, "
                "high, xhigh, or max"
            )
        if name == "codex" and profile.get("reasoning_effort") not in {
            None,
            *CODEX_REASONING,
        }:
            problems.append(
                "defaults.workers.codex.reasoning_effort must be null, low, "
                "medium, high, or xhigh"
            )
        if name == "codex" and profile.get("service_tier") not in {
            None,
            *CODEX_TIERS,
        }:
            problems.append(
                "defaults.workers.codex.service_tier must be null, fast, or priority"
            )

    configured_project = defaults.get("project")
    root = Path(project_root).resolve() if project_root else None
    project = Path(configured_project).expanduser() if configured_project else root
    if project is None or not project.resolve().is_dir():
        problems.append("defaults.project must resolve to an existing directory")

    _bounded_number(
        defaults.get("tail_size", 40), "defaults.tail_size", 1, 500, problems
    )
    _bounded_number(
        defaults.get("worker_timeout", 120),
        "defaults.worker_timeout",
        1,
        3600,
        problems,
    )
    _bounded_number(
        defaults.get("max_parallel_jobs", 2),
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
