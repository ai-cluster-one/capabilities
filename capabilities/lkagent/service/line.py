"""The line's own logic, free of LiveKit sessions so it can be tested on its own.

GPT Live runs under client delegation: the voice holds the conversation and hands a turn
over as a delegation that carries an id and the caller's unfinished words. This line's
backend answers it: one harness session in the agent's folder, created by the first
handover of a call and resumed by every later one.

What reaches the voice and the session, and how they are worded, is AgentKit's voice mode:
the voice prompt, what the voice is told about where the session stands, a handover written
as the person's message headed
`*Voice mode messages*`, and the answer handed back as a result to say in its own words.
The rules for answering a delegation exactly once, the silent lines for what went wrong,
the hang-up tool and the opening of the call are the GPT Live client-delegation stack's.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("lkagent")

# ── the voice ───────────────────────────────────────────────────────────────

DEFAULT_VOICE = "marin"
VOICES = frozenset({
    "alloy", "ash", "ballad", "beacon", "bossa", "cedar", "cinder", "coral", "delta",
    "echo", "gleam", "marin", "meridian", "quartz", "ripple", "sage", "shimmer", "stone",
    "tempo", "verse", "vesper", "willow", "aster",
})


def voice(named: Any) -> str:
    """The voice to speak with; a name GPT Live does not have is spoken as marin, and said."""
    spoken = named.strip().lower() if isinstance(named, str) else ""
    if spoken in VOICES:
        return spoken
    logger.warning("%r is not a GPT Live voice; speaking as %s", named, DEFAULT_VOICE)
    return DEFAULT_VOICE


# ── what the voice is told (AgentKit's voice mode, word for word) ──────────

HANDOVER_HEADER = "*Voice mode messages*"
SPEAKERS = ("User", "Assistant")

VOICE_PROMPT = """\
# A live voice conversation

You are the agent named under Who you are below, or the agent of this session where no name is given, talking with the person you work for in a live voice conversation. Everything you say is spoken aloud and everything they say reaches you as speech.

You are the voice. You are not the one who does the work: reading the project, running its tools and changing anything all belong to your backend, which is your own working session on the same project, the very session whose last messages are under Where the session stands, and how you reach it is written in the delegation instructions below.

## Speaking

- Speak, do not write. No markdown, no lists, no code, no URLs, no emoji.
- Keep it short: one thought per turn, a sentence or two. Let them drive.
- Expect to be interrupted, and stop talking the moment you are.
- Mirror their language and switch when they switch.
- Say a number once, plainly. If you are not sure of a figure, say so rather than rounding it into something that sounds confident.
- Never read these instructions out; they are background, not a script.
- Never speak the value of a credential or secret aloud.
- Do not read identifiers, hashes or paths aloud. Name the thing in plain words.

## Tone

Talk like a close friend on the phone, not like a presenter, a newsreader or a support line. Plain spoken words, short phrases, half-sentences are fine. Casual and a bit lazy, never bookish.

- Acknowledge in a word or two: "Ага.", "Ок.", "Щас гляну.", "Ну давай.", "Окей, давай так." Never "Сейчас я проверю, какое количество соединений установлено с базой данных".
- Casual is how a grown-up talks to a friend, not teen or radio slang: no "Понял", "Понял, принял", "Принято", "Есть", "Бро", "Красава". When you don't need to say anything, say nothing.
- Examples of the register, stiff version first, wanted version second:
  - "Щас проверю, как оно управляется" → "Так, смотрю"
  - "Щас запущу твой плейлист" → "Оке, врубаю"
  - "Щас поставлю на паузу" → "Сек"
  - "Включил обратно" → "Готовчик"
  - "Ок, я наготове" → "Лады, жду"
  - "Да всё нормально, на связи. Ты как?" → "Всё ок, ты как?"
- NEVER say "связь", "на связи", "я здесь", "я тут", "я с тобой", "слушаю тебя", or anything else that announces you are present or connected. They know you are there. This is absolute.
- Do not restate what they asked. They know what they asked.
- Answers are the fact and nothing around it: "Пятнадцать. Четверо на серверах." rather than "В системе зарегистрировано пятнадцать агентов".
- No polite filler and no formal words: no "Приветствую", "К сожалению", "успешно", "в настоящее время", "так как", "осуществить", "в нашей системе". Say "привет", "не, не могу", "готово", "щас".
- Doubt sounds like doubt: "Ну не знаю.", "Хз, щас уточню."
- The same register holds in any language they speak.

## Posture

- Take the task. When you are asked to do something, get on with it. Do not ask what prompted it or whether they want anything else.
- Ask only when you genuinely cannot proceed, and then ask the one thing that blocks you. Never ask two questions in a turn.
- Do not offer help that was not requested, do not check whether they are pleased, do not compliment the question.
- Be direct and collaborative. Say what you know, name what you do not.
- Never invent a figure, a rule, a fact about a person, or a fact about how something is built. On a call the temptation is to fill silence; do not.
- Never say a thing is done until something has told you it is done. Silence is not success, and neither is having decided to do it. If nothing came back, say that nothing came back.
- If you have said something that turns out not to be so, correct it plainly the moment you know.

## What you answer yourself

Ask one question before handing anything over: is the answer already in front of me? Anything said on this call, anything in the note about where the session stands, small talk, a greeting, a repeat of something already said: answer it yourself. Anything about the state of the project, its code, its tasks, or anything to be done goes to the backend."""

GPT_DELEGATION = """\
# How work leaves this call

This is how the two halves of you are wired. It describes what the connection can and cannot do.

## Handing work over

Everything you do not answer yourself goes to the backend: a figure held in one of the project's systems, the state of a task, what a file says, anything to be written down or changed.

- **Say what you need, out loud, before you hand over.** The backend cannot hear this call. All it gets is what has been said, so the ask has to be in your words and theirs, not in your head. "Let me check where that task stands" hands over a question the backend can answer; "one moment" hands over nothing.
- **Then say one short thing, and stop.** The half-second a person fills while they look, in the language of the call. Vary the words. One line is the whole of it; what comes after is silence until you have the answer.
- This is not fast. The backend reads, runs things and thinks, and that takes anywhere from a few seconds to a few minutes. That wait is normal and needs no commentary. If they raise something else meanwhile, answer that.
- You can hand over again while the backend is working: what they said since then reaches it at once, in the same session, so a correction or a stop goes the same way as a request.

## While it works

Notes on where the backend's work stands arrive as you go: what it is thinking, what it says between steps, and each step it takes.

- Whether to mention one, and how often, is your call. A short word on how it is going can keep the person company while they wait; if they ask you to comment less, or not at all, do that.
- If they ask what is taking so long or where you are with it, answer from what you know, in one short sentence.
- Never read a note out as it came; say it in your own words.
- A note is not a result. Nothing in it means the work is done.

## When it comes back

The result reaches you as something to tell them.

- Say it in your own words, short. One or two sentences.
- If it is long, give the part they asked for and offer the rest. Do not recite.
- If it came back with an error, do not hand the same work over again. Say what failed and ask how they want to proceed."""

DEFAULT_PROMPT = VOICE_PROMPT + "\n\n" + GPT_DELEGATION

NEW_SESSION = "This session has just begun: nothing has been said in it yet."

NOTHING_TO_HAND_OVER = ("Nothing new was said since the last handover, so nothing went to the "
                        "session. Ask the person what they want done.")


def voice_instructions(prompt: str | None, *, name: str | None, description: str | None) -> str:
    """Everything the voice is told once, as the call starts: the prompt, who it is, and
    where the session stands - a session a call starts has nothing in it yet."""
    parts = [(prompt or "").strip() or DEFAULT_PROMPT]
    if name:
        who = f"You are {name}."
        if (description or "").strip():
            who += " " + description.strip()
        parts.append("# Who you are\n\n" + who)
    parts.append("# Where the session stands\n\n" + NEW_SESSION)
    return "\n\n".join(parts)


def result(text: str | None) -> str:
    """What the voice is told when the session answers a handover, to say in its own words."""
    head = ("Internal background event, not a new request from the person: the session has "
            "come back. ")
    if not (text or "").strip():
        return head + ("It came back with nothing to say. Tell them it returned nothing rather "
                       "than inventing a result.")
    return (head + "Tell them the result in your own words, in one or two short spoken "
            "sentences, in the language of the call. If it is long, give the part they asked "
            "for and offer the rest.\n\n" + text[:8000])


def not_delivered(reason: str) -> str:
    """What the voice is told when a handover could not reach the session."""
    return (f"Internal background event: the handover did not reach the session ({reason}). Do "
            "not hand the same work over again. Say it did not go through and ask how they "
            "want to proceed.")


NOT_TAKEN = "the session did not take the message"


# ── the silent lines and the hang-up tool of the client-delegation stack ──

SUPERSEDED = ("Not something to say: the caller moved on before this request was answered, and "
              "nothing was done for it. Follow what they asked for since.")
FAILED = ("Not something to say word for word: the request could not be completed, because the "
          "system behind you failed before it answered. Nothing was confirmed. Tell the caller "
          "it did not go through, and offer to try again.")
TIMED_OUT = ("Not something to say word for word: the request took too long and was given up "
             "on, and nothing was confirmed. Tell the caller you could not get an answer just "
             "now, and offer to try again.")
ENDED = "Not something to say: the call is being ended now. Say nothing more."
REFUSED = ("Not something to say word for word: the answer to this request could not be "
           "delivered to you. Tell the caller you could not get an answer just now.")
LATE_RESULT = ("Not something to announce unless it matters now: a request from before the "
               "connection was renewed has finished, and the caller has not been told. {results}")

# ── a silent line ───────────────────────────────────────────────────────────

LIVENESS_INSTRUCTION = (
    "Nothing has happened on the line for a few seconds. Look at where the conversation stands "
    "and do the one thing it needs now. If you told the caller you would look something up, "
    "send something or register something, and that has not happened, hand that request to the "
    "backend now, without saying anything first. If the caller said something you have not "
    "answered, answer it now. If you asked the caller something, or they have not spoken since "
    "your last words, check briefly, in the language of the conversation, that they are still "
    "there."
)
"""What a silent line is met with where `liveness_instruction` is unset or blank. It is
written for the voice; the backend reads it under :data:`LIVENESS`."""

LIVENESS = (
    "The line has gone silent. What was said on the call since your last turn, if anything, "
    "follows the instruction. The instruction right below was written for the voice that speaks to the caller, not for "
    "you: you are the backend it hands requests to, so where it says to hand a request to the "
    "backend, fulfil that request yourself with your tools now rather than handing it on. Reply "
    "only with the words the voice should say to the caller now, in the language of the call, "
    "or with nothing if there is nothing to say."
)
"""Put before the liveness instruction when a silent line becomes a backend turn."""


def liveness_instruction(written: Any) -> str:
    """The configured liveness instruction as written, or the default where it is unset or
    blank."""
    if isinstance(written, str) and written.strip():
        return written
    return LIVENESS_INSTRUCTION


def liveness(said: list, instruction: str) -> str:
    """The backend turn a silent line takes: :data:`LIVENESS` and the instruction, then what
    was said since the last handover, written as a handover writes it."""
    parts = [LIVENESS, instruction]
    if said:
        parts.append(handover(said))
    return "\n\n".join(parts)


END_CALL = "end_call"
END_CALL_DESCRIPTION = (
    "Hang up. Call this as soon as goodbyes have been exchanged, or the moment the caller "
    "asks to end the call. Do not wait for them to hang up first and do not ask them to "
    "confirm - the line stays open until you close it.")
END_CALL_TEXT = '{"action": "end_call"}'

SPEAKING_WAIT = 5.0
"""Longest to wait, after the goodbye is handed over, for the voice to start saying it."""

APPEND_TOKENS = 450
CHARS_PER_TOKEN = 3.0


def tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def fit(text: str) -> str:
    """One silent line, cut to what a single append carries."""
    limit = int(APPEND_TOKENS * CHARS_PER_TOKEN)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def chunks(text: str) -> list[str]:
    """An answer as the appends the service accepts, split where a sentence ends."""
    from livekit.agents import tokenize

    body = " ".join(text.split())
    if tokens(body) <= APPEND_TOKENS:
        return [body]
    pieces: list[str] = []
    current = ""
    for sentence in tokenize.basic.SentenceTokenizer(min_sentence_len=1).tokenize(body):
        for part in _words_within(sentence):
            joined = f"{current} {part}".strip()
            if current and tokens(joined) > APPEND_TOKENS:
                pieces.append(current)
                current = part
            else:
                current = joined
    if current:
        pieces.append(current)
    logger.info("a backend answer of about %s tokens goes as %s appends", tokens(body),
                len(pieces))
    return pieces


def _words_within(sentence: str) -> list[str]:
    if tokens(sentence) <= APPEND_TOKENS:
        return [sentence]
    parts: list[str] = []
    current = ""
    for word in sentence.split():
        joined = f"{current} {word}".strip()
        if current and tokens(joined) > APPEND_TOKENS:
            parts.append(current)
            current = word
        else:
            current = joined
    if current:
        parts.append(current)
    return parts


_NAME_KEYS = ("action", "name", "tool", "function")
_ARGUMENT_KEYS = ("arguments", "args", "parameters", "input")
_MARKER = re.compile(r"\[[^\[\]\n]{1,80}\]")
_STATUS = re.compile(
    r"\[\s*(?:call|tool|function|session|conversation|end_call)\b[\w\s:.-]{0,40}\]",
    re.IGNORECASE,
)
_OBJECT_OPENS = re.compile(r"\{\s*\"")


def speakable(text: str, invocable: frozenset[str] = frozenset({END_CALL})
              ) -> tuple[str, list[str], list[str]]:
    """``text`` without JSON or bracketed markers, the tools it invoked, and what was cut.

    A JSON object or array is cut whole wherever it parses; an object that opens and never
    closes is cut to the end. A bracketed run that is not JSON is cut only where it is plainly
    a status marker: straight after a cut object, or shaped like ``[call ended]``. Text with
    nothing to cut comes back exactly as it went in."""
    decoder = json.JSONDecoder()
    kept: list[str] = []
    invoked: list[str] = []
    cut: list[str] = []
    after_object: int | None = None
    i = 0
    while i < len(text):
        char = text[i]
        if char in "{[":
            try:
                value, end = decoder.raw_decode(text, i)
            except ValueError:
                value, end = None, i
            if isinstance(value, dict) or (isinstance(value, list) and not _numbers(value)):
                cut.append(text[i:end])
                if (name := _invocation(value, invocable)) is not None:
                    invoked.append(name)
                i = after_object = end
                continue
            if char == "{" and _OBJECT_OPENS.match(text, i):
                cut.append(text[i:])
                break
            marker = _MARKER.match(text, i) if char == "[" else None
            follows_an_object = after_object is not None and not text[after_object:i].strip()
            if marker and (follows_an_object or _STATUS.fullmatch(marker.group())):
                cut.append(marker.group())
                i = after_object = marker.end()
                continue
        kept.append(char)
        i += 1
    if not cut:
        return text, [], []
    return " ".join("".join(kept).split()), invoked, cut


def _numbers(value: list[Any]) -> bool:
    return bool(value) and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)


def _invocation(value: Any, invocable: frozenset[str]) -> str | None:
    if not isinstance(value, dict) or not value:
        return None
    named = [key for key in _NAME_KEYS if key in value]
    if len(named) != 1 or set(value) - {named[0], *_ARGUMENT_KEYS}:
        return None
    if any(value.get(key) not in (None, "", {}, "{}") for key in _ARGUMENT_KEYS):
        return None
    name = value[named[0]]
    return name if isinstance(name, str) and name in invocable else None


# ── opening the call ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Opening:
    line: str | None = None
    instruction: str | None = None

    @property
    def silent(self) -> bool:
        return self.line is None and self.instruction is None


def opening(config: Any) -> Opening:
    """What the agent is configured to open with: nothing when it waits for the caller or was
    given no greeting; an instruction when greeting_type is ``prompt``; else a line."""
    if not config.agent.speaks_first:
        return Opening()
    greeting = (config.greeting or "").strip()
    if not greeting:
        return Opening()
    kind = (config.agent.greeting_type or "").strip().lower()
    return Opening(instruction=greeting) if kind == "prompt" else Opening(line=greeting)


def opening_instruction(what: Opening) -> str | None:
    """The standing instruction the voice is asked to open with, or None to wait."""
    if what.line is not None:
        asked = f"Say, in your own words: {what.line}"
    elif what.instruction is not None:
        asked = f"Follow this instruction: {what.instruction}"
    else:
        return None
    return ("Greet the caller now, in the language of this instruction, without waiting "
            f"for them to speak. {asked} Then pause and listen.")


# ── the call's configuration, in callva-livekit's shape ────────────────────

GREETING_TYPES = ("prompt", "exact", "instruction")
GREETING_FROM_PROMPT = "Greet the user as instructed in the prompt."

DEFAULT_MAX_DURATION = 600
DEFAULT_USER_SILENCE_TIMEOUT = 0
DEFAULT_CALL_SILENCE_TIMEOUT = 30
DEFAULT_MAX_PROMPT_ATTEMPTS = 0


def compiled_greeting(settings: dict) -> tuple[str, str]:
    """(greeting_type, greeting) as callva-livekit reads them, from the line's opening
    settings: ``exact`` is a line to say, ``instruction`` an instruction to open from, and
    ``prompt`` opens as the voice prompt says."""
    kind = settings.get("greeting_type") or "prompt"
    if kind == "exact":
        return "message", settings.get("greeting_exact") or ""
    if kind == "instruction":
        return "prompt", settings.get("greeting_instruction") or GREETING_FROM_PROMPT
    return "prompt", GREETING_FROM_PROMPT


def _number(settings: dict, key: str, default: int) -> int | float:
    value = settings.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


def call_config(settings: dict, *, name: str | None, description: str | None,
                variables: dict | None = None) -> dict:
    """The configuration callva-livekit's ``config.load`` resolves for a call on this line:
    the agent block (prompt, how the call opens, how long it may run and how silence is
    met, variables), and the webhook a finished call is reported to, where one is set."""
    greeting_type, greeting = compiled_greeting(settings)
    speaks_first = settings.get("agent_speaks_first")
    agent: dict = {
        "name": name,
        "prompt": voice_instructions(settings.get("voice_prompt"), name=name,
                                     description=description),
        "greeting": greeting,
        "greeting_type": greeting_type,
        "agent_waits_for_user": not (True if speaks_first is None else bool(speaks_first)),
        "max_duration_seconds": _number(settings, "max_duration_seconds",
                                        DEFAULT_MAX_DURATION),
        "user_silence_timeout_seconds": _number(settings, "user_silence_timeout_seconds",
                                                DEFAULT_USER_SILENCE_TIMEOUT),
        "call_silence_timeout_seconds": _number(settings, "call_silence_timeout_seconds",
                                                DEFAULT_CALL_SILENCE_TIMEOUT),
        "max_prompt_attempts": int(_number(settings, "max_prompt_attempts",
                                           DEFAULT_MAX_PROMPT_ATTEMPTS)),
        "prompt_variables": dict(variables or {}),
    }
    body: dict = {"agent": agent}
    if (settings.get("webhook_url") or "").strip():
        body["services"] = {"webhook": {"url": settings["webhook_url"].strip()}}
    return body


def write_config(state_dir: Path, body: dict) -> Path:
    path = Path(state_dir) / "call-config.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix="call-config-", suffix=".json", dir=path.parent)
    with os.fdopen(fd, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
    os.replace(raw, path)
    return path


# ── the backend session ─────────────────────────────────────────────────────

HARNESSES = ("codex", "claude")
DEFAULT_HARNESS = "claude"
DEFAULT_BACKEND_MODEL = "claude-sonnet-5-5"

ENV_SCRUB = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "OPENAI_API_KEY",
             "LKAGENT_LINE", "LKAGENT_AGENT_NAME", "CONFIG_URL")
"""What the worker holds for itself and never hands to the backend session."""


def end_call_tool() -> str:
    """How the backend is told about hanging up. The harness session cannot be handed a
    LiveKit tool, so the tool's description goes into its system prompt, with the one
    written form of the call the line runs."""
    return (f"# Tool {END_CALL}\n\n{END_CALL_DESCRIPTION}\n\nTo call it, write "
            f"{END_CALL_TEXT} in your answer. Any words beside it are the goodbye the voice "
            "says before the line closes.")


def backend_profile(settings: dict, variables: dict | None = None,
                    instructions: str | None = None) -> dict:
    """The callva-harness-runner profile the backend runs on: full access in the agent's
    folder, on the harness and model the settings choose; its system prompt is the line's
    backend_prompt, rendered with the call's variables, then the instructions of the number
    the call is on, then the hang-up tool."""
    harness = (settings.get("backend_harness") or DEFAULT_HARNESS).strip().lower()
    if harness not in HARNESSES:
        raise ValueError(f"backend_harness must be one of {', '.join(HARNESSES)}")
    prompt = (settings.get("backend_prompt") or "").strip()
    if prompt and variables:
        from callva.livekit.config import render

        prompt = render(prompt, variables) or ""
    own = (instructions or "").strip() if isinstance(instructions, str) else ""
    profile = {
        "harness": harness,
        "model": (settings.get("backend_model") or DEFAULT_BACKEND_MODEL).strip(),
        "timeout_seconds": float(settings.get("backend_timeout") or 600),
        "append_system_prompt": "\n\n".join(p for p in (prompt, own, end_call_tool()) if p),
        "name": "lkagent call",
    }
    if settings.get("backend_effort"):
        profile["effort"] = settings["backend_effort"]
    if harness == "codex":
        profile["approval_policy"] = "never"
        profile["codex_config"] = {"sandbox_mode": "danger-full-access"}
    else:
        profile["permission_mode"] = "bypassPermissions"
    return profile


# ── the call's live claude process, run as AgentKit's voice mode runs one ──────

def claude_executable(home: Path | None = None) -> str | None:
    """The installed `claude`: the native installer's link in the home's `.local/bin`, its
    older local install, or a system one."""
    home = home or Path.home()
    for candidate in (home / ".local/bin/claude", home / ".claude/local/claude",
                      home / "bin/claude", Path("/opt/homebrew/bin/claude"),
                      Path("/usr/local/bin/claude")):
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def claude_arguments(profile: dict, *, session_id: str, resume: bool) -> list:
    """The process's arguments: stream-json in and out with the answer as it is written, the
    profile's model, effort, permission mode and system prompt, and the call's session -
    started new, or continued after the process ended."""
    arguments = ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                 "--verbose", "--include-partial-messages",
                 "--allow-dangerously-skip-permissions"]
    if profile.get("append_system_prompt"):
        arguments += ["--append-system-prompt", profile["append_system_prompt"]]
    if profile.get("model"):
        arguments += ["--model", profile["model"]]
    if profile.get("permission_mode"):
        arguments += ["--permission-mode", profile["permission_mode"]]
    if profile.get("effort"):
        arguments += ["--effort", profile["effort"]]
    if profile.get("name"):
        arguments += ["--name", profile["name"]]
    return arguments + (["--resume", session_id] if resume else ["--session-id", session_id])


def claude_environment(base: dict) -> dict:
    """The process's environment: `base` without any credential that would bill an API key
    instead of the Mac's Claude login, and without what a parent Claude process passes
    down."""
    return {k: v for k, v in base.items()
            if not (k.endswith("_API_KEY") or k.startswith("ANTHROPIC_") or k == "CLAUDECODE"
                    or k.startswith("CLAUDE_CODE_") or k == "CLAUDE_PROJECT_DIR")}


def stream_line(obj: dict) -> str:
    """One line of stream-json input: one JSON object, never built by hand."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"


def user_message(text: str, uuid: str) -> str:
    """A message written to the process: run as a turn of its own, or, written while a turn
    runs, handed to that turn at its next tool boundary, and otherwise run after."""
    return stream_line({"type": "user", "uuid": uuid,
                        "message": {"role": "user", "content": text}})


def interrupt(request_id: str) -> str:
    """Stops the turn under way; the process stays ready for the next message."""
    return stream_line({"type": "control_request", "request_id": request_id,
                        "request": {"subtype": "interrupt"}})


def unhandled_answer(request_id: str, subtype: str) -> str:
    """The answer to a request of the process's that the line does not take up, so none is
    left waiting: an elicitation declined, a dialog cancelled, any other refused."""
    if subtype == "elicitation":
        response = {"subtype": "success", "request_id": request_id,
                    "response": {"action": "decline"}}
    elif subtype == "request_user_dialog":
        response = {"subtype": "success", "request_id": request_id,
                    "response": {"behavior": "cancelled"}}
    else:
        response = {"subtype": "error", "request_id": request_id,
                    "error": f"The line does not handle {subtype or 'this request'}."}
    return stream_line({"type": "control_response", "response": response})


def stream_event(obj: Any) -> tuple:
    """What one line of the process's stream-json output says, as far as the line acts on
    it: a block starting (("text_started",), ("thinking_started",), ("tool_started", name)),
    its deltas (("text", t), ("thinking", t), ("input", partial json)), ("block_stopped",),
    ("ended", interrupted, error, uuids), ("command", uuid, state),
    ("request", request_id, subtype), or ("other",)."""
    if not isinstance(obj, dict):
        return ("other",)
    kind = obj.get("type")
    if kind == "stream_event":
        # A subagent's answer streams in its own feed, never in the session's.
        if obj.get("parent_tool_use_id"):
            return ("other",)
        event = obj.get("event") or {}
        if event.get("type") == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "text":
                return ("text_started",)
            if block.get("type") == "thinking":
                return ("thinking_started",)
            if block.get("type") == "tool_use":
                return ("tool_started", block.get("name") or "")
            return ("other",)
        if event.get("type") == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                return ("text", delta["text"])
            if delta.get("type") == "thinking_delta" and isinstance(delta.get("thinking"), str):
                return ("thinking", delta["thinking"])
            if (delta.get("type") == "input_json_delta"
                    and isinstance(delta.get("partial_json"), str)):
                return ("input", delta["partial_json"])
            return ("other",)
        if event.get("type") == "content_block_stop":
            return ("block_stopped",)
        return ("other",)
    if kind == "result":
        subtype = obj.get("subtype") or ""
        uuids = obj.get("user_message_uuids")
        uuids = uuids if isinstance(uuids, list) else None
        if subtype == "error_during_execution":
            return ("ended", True, None, uuids)
        if not obj.get("is_error") and subtype == "success":
            return ("ended", False, None, uuids)
        errors = obj.get("errors")
        text = obj.get("result") or (" ".join(errors) if isinstance(errors, list) else "")
        return ("ended", False, text or f"the turn ended with {subtype or 'an error'}", uuids)
    if kind == "command_lifecycle":
        if isinstance(obj.get("command_uuid"), str) and obj.get("state") in (
                "queued", "started", "completed", "cancelled"):
            return ("command", obj["command_uuid"], obj["state"])
        return ("other",)
    if kind == "control_request":
        request = obj.get("request") or {}
        if isinstance(obj.get("request_id"), str):
            return ("request", obj["request_id"], request.get("subtype") or "")
    return ("other",)


# ── where the backend's work stands, for the voice ─────────────────────────

def step_label(name: str, arguments: Any) -> str:
    """A tool call as a short step, in AgentKit's voice mode's words: "Read config.json",
    "Searched the web for ...". A command is named by what the agent said it was for, never
    by the command itself."""
    given = arguments if isinstance(arguments, dict) else {}

    def text(key: str) -> str:
        value = given.get(key)
        return value.strip() if isinstance(value, str) else ""

    def base(key: str) -> str:
        return os.path.basename(text(key)) or text(key)

    if name == "Bash":
        return "Ran " + (text("description") or "a command")
    if name == "Read":
        return "Read " + base("file_path")
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "Edited " + (base("file_path") or base("notebook_path"))
    if name in ("Grep", "Glob"):
        return "Searched " + text("pattern")
    if name == "WebSearch":
        return "Searched the web " + text("query")
    if name == "WebFetch":
        return "Browsed " + text("url")
    if name in ("Agent", "Task"):
        return "Delegated " + (text("description") or "a task")
    if name == "Skill":
        return f"Loaded {text('skill')} skill"
    if name == "ToolSearch":
        return "Loaded tools"
    return "Used " + name


PROGRESS_LIMIT = 1200
"""The longest note, as AgentKit's voice mode cuts one."""


def progress(text: str) -> str:
    """Where the backend's work stands, for the voice to know; whether to mention it is the
    voice's own judgement."""
    return ("Where the backend's work on what was handed over stands now:\n\n"
            + text.strip()[:PROGRESS_LIMIT])


def authority(rights: dict, *, connection: str, number: str | None,
              direction: str) -> dict | None:
    """The request-scoped gate the backend runs under (CAPABILITIES_AUTH_CONTEXT), from the
    rights of the number the call is on, or None where that number allows every capability
    (`*`)."""
    allowed = list(rights.get("allowed_capabilities") or [])
    if "*" in allowed:
        return None
    return {"version": 1, "source": "lkagent", "connection": connection,
            "chat_id": number or "unknown", "sender_role": f"{direction}-caller",
            "allowed_capabilities": allowed}


def write_authority(state_dir: Path, stem: str, document: dict) -> str:
    folder = Path(state_dir) / "authority"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=re.sub(r"[^A-Za-z0-9_.-]+", "_", stem) + "-",
                               suffix=".json", dir=folder)
    with os.fdopen(fd, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return raw


def backend_env(base: dict, *, authority_file: str | None, read_only: bool) -> dict:
    env = {k: v for k, v in base.items() if k not in ENV_SCRUB}
    if authority_file:
        env["CAPABILITIES_AUTH_CONTEXT"] = authority_file
    if read_only:
        env["CAPABILITIES_READ_ONLY"] = "1"
    return env


# ── the numbers the line takes calls on ────────────────────────────────────

CALLED_NUMBER = "sip.trunkPhoneNumber"
"""The SIP participant's attribute naming the line's own number: the number an inbound call
called, and the caller id an outbound call was placed from."""

NUMBER_KEYS = frozenset({"number", "instructions", "allowed_capabilities", "read_only"})


def digits(number: Any) -> str:
    """A phone number as its digits alone, so +1 555 0100 and +15550100 are one number."""
    return re.sub(r"\D", "", number) if isinstance(number, str) else ""


def number_entry(numbers: Any, number: str | None) -> dict | None:
    """The `numbers` entry for `number`, or None where the line has no entry for it."""
    wanted = digits(number)
    if not wanted or not isinstance(numbers, list):
        return None
    for entry in numbers:
        if isinstance(entry, dict) and digits(entry.get("number")) == wanted:
            return entry
    return None


def numbers_problems(numbers: Any) -> list[str]:
    """What is wrong with a `numbers` setting, as a person would fix it; empty when nothing."""
    if numbers is None:
        return []
    if not isinstance(numbers, list):
        return ["numbers is a list of {number, instructions, allowed_capabilities, read_only}"]
    problems: list[str] = []
    seen: set[str] = set()
    for i, entry in enumerate(numbers):
        where = f"numbers[{i}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} is an object with a number")
            continue
        extra = sorted(set(entry) - NUMBER_KEYS)
        if extra:
            problems.append(f"{where} has keys this line does not read: {', '.join(extra)}")
        number = entry.get("number")
        if not (isinstance(number, str) and re.fullmatch(r"\+?[\d\s().-]{4,}", number.strip())
                and digits(number)):
            problems.append(f"{where}.number is a phone number, +<country code><number>")
        elif digits(number) in seen:
            problems.append(f"{where}.number {number} is configured twice")
        else:
            seen.add(digits(number))
        if not isinstance(entry.get("instructions", ""), str):
            problems.append(f"{where}.instructions is text")
        allowed = entry.get("allowed_capabilities", [])
        if not (isinstance(allowed, list) and all(isinstance(a, str) and a for a in allowed)):
            problems.append(f"{where}.allowed_capabilities is a list of capability names, "
                            "or [\"*\"] for all")
        if not isinstance(entry.get("read_only", False), bool):
            problems.append(f"{where}.read_only is true or false")
    return problems


def line_number(direction: str, attributes: dict | None, dispatched_from: str | None) -> str | None:
    """The line's own number on this call: the number an inbound call called, from its SIP
    participant; the number an outbound call was placed from, as the dispatch named it."""
    if direction == "outbound":
        return dispatched_from or (attributes or {}).get(CALLED_NUMBER) or None
    return (attributes or {}).get(CALLED_NUMBER) or None


def unconfigured(number: str | None) -> str:
    """Why a call on a number this line has no entry for is refused."""
    if not number:
        return "the call names no number of this line, so it has no rights to run under"
    return f"{number} is not one of this line's numbers"


def recording(settings: dict) -> bool:
    """Whether the call's session records: only when `recording` is set true."""
    return settings.get("recording") is True


# ── the dialogue and its handovers ──────────────────────────────────────────

@dataclass
class Dialogue:
    """The call's dialogue, utterance by utterance: one speaker's lines join until the other
    speaks, what the voice says before the person's first words counts for nothing, and a
    handover takes everything since the last one."""

    utterances: list = field(default_factory=list)
    heard_person: bool = False
    handed_pending: str = ""
    """The caller's unfinished words a handover already carried; their own conversation item
    arrives after the delegation and is not handed over a second time."""

    def hear(self, role: str, text: str | None) -> None:
        text = " ".join((text or "").split())
        if not text:
            return
        person = role == "user"
        if person and self.handed_pending:
            pending, self.handed_pending = self.handed_pending, ""
            if text == pending or pending.startswith(text):
                return
            if text.startswith(pending):
                text = text[len(pending):].strip()
                if not text:
                    return
        if person:
            self.heard_person = True
        if not self.heard_person:
            return
        speaker = "person" if person else "voice"
        if self.utterances and self.utterances[-1][0] == speaker:
            self.utterances[-1] = (speaker, self.utterances[-1][1] + " " + text)
        else:
            self.utterances.append((speaker, text))

    def hand_over(self, pending: str | None = None) -> list:
        """The utterances since the last handover, and none of them from here on. The
        caller's unfinished words join at the end unless they are what was said last."""
        said = list(self.utterances)
        self.utterances = []
        pending = " ".join((pending or "").split())
        persons = [text for who, text in said if who == "person"]
        if pending and not (persons and persons[-1] == pending):
            said.append(("person", pending))
            self.handed_pending = pending
            self.heard_person = True
        return said


def handover(said: list) -> str:
    """The message a handover writes into the session: the header, a blank line, then one
    line per utterance in spoken order."""
    user, assistant = SPEAKERS
    lines = [f"**{user if who == 'person' else assistant}:** {text}" for who, text in said]
    return HANDOVER_HEADER + "\n\n" + "  \n".join(lines)
