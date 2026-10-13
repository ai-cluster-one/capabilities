"""The reply gate: whether a group conversation needs the assistant at all.

A group message reaches a turn mechanically - every message, a mention of the
account's @username, one of the room's aliases, or a reply to the account - and
each of those three triggers is switched on or off per group (`triggers`). The
gate is one more trigger beside them, independent of the others: with it on,
the messages no enabled trigger admits are collected per channel, and once the
channel has been quiet for its debounce one light harness turn reads the batch
and decides to ignore it, react to one message with one emoji, or reply to one
message. A reply is then an ordinary turn on the channel's own profile; a
reaction is set by the daemon and no turn runs.

The gate is a decision, not a conversation. It reads a compact input built for
it here - who the assistant is, the batch, a short lead-in, two facts and the
emoji it may use - and never the turn's prompt, the project's context or a
channel's context file. It runs on the vendor of the channel's resolved
profile, on one of the two gate profiles telegram ships, from an empty
directory, with no tools and no session saved. Its answer is held to a JSON
schema, and an answer outside it is read as ignore.

`shadow`, the default once the gate is on, decides and logs and acts on
nothing; `live` acts. Every run writes one record.

Nothing here imports the runner, Telethon, the store or the network: the
daemon owns those and hands `run` the runner, and the suite reads this
module's decisions directly.
"""

from __future__ import annotations

import json
import re
import tempfile
import unicodedata
from pathlib import Path

# The mechanical triggers, each on unless a group's `triggers` leaves it out:
# `username` is Telegram's mention flag and the account's @username in the
# text, `alias` a configured alias, `reply` a reply to the account's message.
TRIGGERS = ("username", "alias", "reply")
GATE_MODES = ("shadow", "live")
ACTIONS = ("ignore", "react", "reply")

# Defaults of a group's `gate` block.
DEFAULT_CONTEXT_MESSAGES = 12
DEFAULT_CONTEXT_CHARS = 4000
DEFAULT_MAX_BATCH = 10
# The longest a batch message's text is carried in the gate's input; a decision
# about whether to answer is made on the opening of a long message.
MESSAGE_CHARS = 1000

# The gate's two profiles, by the harness of the channel's resolved profile.
# They are telegram's bundled profiles, the lowest layer of the same store
# resolution every telegram profile uses, and never a turn's: they set an
# output schema and carry no tools, so the turn's fit refuses them.
PROFILES = {"claude": "telegram-gate-claude", "codex": "telegram-gate-codex"}
STUB = "stub"
# What a channel on the stub gates with: no harness runs, and the decision is
# whatever this holds, so a case can drive every outcome.
STUB_DECISION = {"action": "ignore", "target_message_id": None, "emoji": None,
                 "reason": "the stub gate decides nothing"}

# The emoji a bot may react with: Telegram's standard reaction set, as the Bot
# API lists it for `ReactionTypeEmoji`. A chat narrows it with its own
# `available_reactions`; the gate is offered the intersection.
STANDARD_REACTIONS = (
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢", "🎉", "🤩",
    "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭",
    "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍", "🤗",
    "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘", "💊",
    "🙊", "😎", "👾", "🤷‍♂", "🤷", "🤷‍♀", "😡",
)


def _mapping(value):
    return value if isinstance(value, dict) else {}


# --- Settings ----------------------------------------------------------------

def triggers(policy) -> frozenset:
    """The mechanical triggers a group policy leaves on: every one where the
    policy says nothing, so a room without `triggers` is admitted as before."""
    named = _mapping(policy).get("triggers")
    if not isinstance(named, list):
        return frozenset(TRIGGERS)
    return frozenset(str(one) for one in named if one in TRIGGERS)


def settings(policy) -> dict | None:
    """A group's gate block with its defaults filled in, or None while the
    gate is off. Switching it on without naming a mode is `shadow`."""
    block = _mapping(_mapping(policy).get("gate"))
    if block.get("enabled") is not True:
        return None
    return {
        "mode": block.get("mode") if block.get("mode") in GATE_MODES else "shadow",
        "brief": str(block.get("brief") or "").strip(),
        "context_messages": int(block.get("context_messages", DEFAULT_CONTEXT_MESSAGES)),
        "context_chars": int(block.get("context_chars", DEFAULT_CONTEXT_CHARS)),
        "max_batch": int(block.get("max_batch", DEFAULT_MAX_BATCH)),
        "model": block.get("model"),
        "effort": block.get("effort"),
    }


def _for_harness(value, harness):
    """A `model` or `effort` setting for `harness`: a string applies to the
    harness the channel runs, an object names one per harness."""
    if isinstance(value, dict):
        value = value.get(harness)
    return str(value) if isinstance(value, str) and value.strip() else None


def overlay(gate, harness) -> dict:
    """What a group's `gate.model` and `gate.effort` lay over the gate profile
    of `harness`. Neither ever changes the harness: the gate's vendor is the
    channel's."""
    knobs = {}
    model = _for_harness((gate or {}).get("model"), harness)
    if model:
        knobs["model"] = model
    effort = _for_harness((gate or {}).get("effort"), harness)
    if effort:
        knobs["effort"] = effort
    return knobs


def profile_name(harness) -> str | None:
    """The gate profile for a channel whose profile runs `harness`; None for
    the stub and for a harness the gate has no profile for."""
    return PROFILES.get(harness)


# --- Fit ---------------------------------------------------------------------

def fit_problems(knobs: dict) -> list[str]:
    """Why a profile cannot carry a gate run, one sentence per knob; empty when
    it can. A gate run is one light turn: its own system prompt, an answer held
    to a schema, nothing saved, and on claude no tools, no settings files and
    one turn."""
    problems = []
    harness = knobs.get("harness")
    if harness not in ("claude", "codex"):
        problems.append(f"harness {harness!r} is not claude or codex")
    if not isinstance(knobs.get("output_schema"), dict):
        problems.append("output_schema is not set; the gate's decision is read as JSON")
    if not str(knobs.get("system_prompt") or "").strip():
        problems.append("system_prompt is not set; the gate runs on its own "
                        "instructions, not a coding agent's")
    if knobs.get("persist_session") is not False:
        problems.append("persist_session is not false; a gate run is saved in no "
                        "session list")
    if knobs.get("timeout_seconds") is None:
        problems.append("timeout_seconds is not set")
    if harness == "claude":
        if knobs.get("tools") != []:
            problems.append("tools is not empty; a gate run uses no tool")
        if knobs.get("setting_sources") != []:
            problems.append("setting_sources is not empty; a gate run loads no "
                            "settings, hooks or plugins")
        if knobs.get("strict_mcp") is not True:
            problems.append("strict_mcp is not true; a gate run reaches no MCP server")
        if knobs.get("max_turns") != 1:
            problems.append("max_turns is not 1; a gate run is a single turn")
    return problems


# --- The decision ------------------------------------------------------------

def _plain(emoji) -> str:
    """An emoji compared without its variation selector, which Telegram writes
    on some reactions and not on others."""
    return unicodedata.normalize("NFC", str(emoji)).replace("️", "")


def allowed_emoji(available) -> list[str]:
    """The standard set narrowed to what a chat allows: `available` is "all",
    a list of emoji, or None for a chat that allows no reaction."""
    if available == "all":
        return list(STANDARD_REACTIONS)
    if not isinstance(available, (list, tuple)):
        return []
    held = {_plain(one) for one in available}
    return [one for one in STANDARD_REACTIONS if _plain(one) in held]


def decision_schema(emoji) -> dict:
    """The schema one run's answer is held to: an action, a message id, an
    emoji from the allowed set (null unless reacting) and a reason. Every
    property is required and nullable where it may be empty, which both
    harnesses' structured output accept. A chat that allows no reaction is
    offered no `react`.

    It depends on the chat's allowed set alone and never on the batch. The
    harness carries the schema ahead of the gate's system prompt, at the head
    of the prompt prefix it caches, so a schema that named the batch's ids
    would make every run's prefix new and no run would read the cache. That
    the target is a message of the batch is held by `parse_decision`."""
    actions = [one for one in ACTIONS if one != "react" or emoji]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "target_message_id", "emoji", "reason"],
        "properties": {
            "action": {"type": "string", "enum": actions},
            "target_message_id": {"type": ["integer", "null"]},
            "emoji": {"type": ["string", "null"], "enum": [*emoji, None]},
            "reason": {"type": "string"},
        },
    }


def parse_decision(answer, batch_ids, emoji) -> dict:
    """The decision a run returned, checked against the batch and the allowed
    set. A decision that is malformed or outside them is ignore, with
    `invalid` saying why, so nothing is acted on that was not offered."""
    def refused(why):
        return {"action": "ignore", "target_message_id": None, "emoji": None,
                "reason": "", "invalid": why}

    if isinstance(answer, str):
        try:
            answer = json.loads(answer)
        except ValueError:
            return refused("the answer is not JSON")
    if not isinstance(answer, dict):
        return refused("the answer is not an object")
    action = answer.get("action")
    if action not in ACTIONS:
        return refused(f"action {action!r} is not one of {', '.join(ACTIONS)}")
    reason = answer.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    target = answer.get("target_message_id")
    ids = {int(one) for one in batch_ids}
    if action == "ignore":
        target = target if isinstance(target, int) and target in ids else None
        return {"action": "ignore", "target_message_id": target, "emoji": None,
                "reason": reason}
    if isinstance(target, bool) or not isinstance(target, int) or target not in ids:
        return refused(f"target_message_id {target!r} is not a message of the batch")
    chosen = answer.get("emoji")
    if action == "react":
        offered = {_plain(one): one for one in emoji}
        if not isinstance(chosen, str) or _plain(chosen) not in offered:
            return refused(f"emoji {chosen!r} is not one this chat allows")
        return {"action": "react", "target_message_id": target,
                "emoji": offered[_plain(chosen)], "reason": reason}
    return {"action": "reply", "target_message_id": target, "emoji": None,
            "reason": reason}


# --- The input ---------------------------------------------------------------

def _clip(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit].rstrip() + " [...]"


def lead_in_within(rows: list[dict], count: int, chars: int) -> list[dict]:
    """The last `count` rows that fit in `chars` characters of text together,
    newest kept first, returned oldest first."""
    kept, used = [], 0
    for row in reversed(rows[-count:] if count > 0 else []):
        size = len(str(row.get("text") or ""))
        if kept and used + size > chars:
            break
        if size > chars:
            row = {**row, "text": _clip(row.get("text"), chars)}
            size = chars
        kept.append(row)
        used += size
    return list(reversed(kept))


def build_input(identity: dict, batch: list[dict], lead_in: list[dict], facts: dict,
                emoji: list[str]) -> str:
    """The whole of what a gate run reads, as one JSON document.

    `identity` is who the assistant is in this room: its display name, its
    @username, its aliases and the room's brief. Each message, in the batch and
    in the lead-in, is its id, its sender's name and role, its time, the
    message it replies to and whether that is the assistant's, and its text;
    the lead-in marks the assistant's own messages. `facts` are the seconds
    since the assistant last spoke here and its share of the window."""
    def message(row, *, lead):
        out = {"id": row.get("id"), "sender": row.get("sender"), "role": row.get("role"),
               "time": row.get("time"), "reply_to": row.get("reply_to"),
               "reply_to_you": bool(row.get("reply_to_you")),
               "text": _clip(row.get("text"), MESSAGE_CHARS)}
        if lead:
            out["you"] = bool(row.get("you"))
        return out

    document = {
        "you": {"name": identity.get("name"), "username": identity.get("username"),
                "aliases": list(identity.get("aliases") or []),
                "brief": identity.get("brief")},
        "lead_in": [message(row, lead=True) for row in lead_in],
        "batch": [message(row, lead=False) for row in batch],
        "facts": {"seconds_since_you_spoke": facts.get("seconds_since_you_spoke"),
                  "your_messages_in_window": facts.get("your_messages_in_window"),
                  "messages_in_window": facts.get("messages_in_window")},
        "allowed_emoji": list(emoji),
    }
    return ("Decide what the assistant does about the new messages in `batch`. "
            "`lead_in` is what came before them. Answer with the JSON object your "
            "instructions describe and nothing else.\n\n"
            + json.dumps(document, ensure_ascii=False, indent=1))


def media_placeholder(kind: str, *, photo=False) -> str:
    """What a message without text stands for in the gate's input."""
    if kind == "voice":
        return "[voice]"
    if photo:
        return "[photo]"
    return "[file]" if kind == "media" else ""


# --- The record --------------------------------------------------------------

def record(*, channel, batch_ids, harness, model, decision, latency_ms, tokens, mode,
           acted, error=None, outcome=None) -> dict:
    """The one record a gate run writes: where it ran, what it read and on
    what, what it decided and why, what that cost, and whether it was acted
    on."""
    row = {"channel": channel, "batch": list(batch_ids), "harness": harness,
           "model": model, "action": decision.get("action"),
           "target": decision.get("target_message_id"), "emoji": decision.get("emoji"),
           "reason": decision.get("reason"), "latency_ms": latency_ms, "tokens": tokens,
           "mode": mode, "acted": bool(acted)}
    if decision.get("invalid"):
        row["invalid"] = decision["invalid"]
    if error:
        row["error"] = error
    if outcome:
        row["outcome"] = outcome
    return row


# --- The run -----------------------------------------------------------------

def run(lib, run_fn, found, prompt, schema, channel, *, run_root, codex_home, environ):
    """One gate run: the resolved gate profile with its schema narrowed to the
    chat's allowed emoji (`schema`), started from an empty directory made under
    `run_root` and removed after, as a fresh session. On codex the run reads `codex_home`
    rather than the user's codex home, unless the profile sets CODEX_HOME
    itself. `lib` is the harness runner and `run_fn` its `run`."""
    knobs = {**found["knobs"], "output_schema": schema}
    profile = lib.Profile.from_dict(knobs)
    extra_env = {}
    if profile.harness == "codex" and "CODEX_HOME" not in (knobs.get("env_set") or {}):
        home = Path(codex_home)
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        extra_env["CODEX_HOME"] = str(home)
    root = Path(run_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=run_dir_name(channel), dir=root) as cwd:
        return run_fn(prompt, profile, cwd, session=lib.Session.fresh(), environ=environ,
                      extra_env=extra_env)


_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def run_dir_name(channel: str) -> str:
    """The prefix of a gate run's empty working directory for a channel."""
    return "gate-" + (_SAFE.sub("_", str(channel)).strip("_") or "chat") + "-"
