"""How the worker binaries are invoked, and how what they answer is read.

The daemon runs a turn on a worker binary; `telegram service doctor` asks the
same binary for one minimal round-trip per configured (worker, model) pair.
Both go through this module, so the command a worker is launched with, the
way its success or failure is read off its output, and what counts as the
binary refusing a model each have one owner. Nothing here reads settings, the
store, or the network at import time, so the CLI loads it without standing up
a daemon.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

# The binary each worker runs on. The in-process stub has none, and its absence
# here is what tells a caller there is nothing to ask.
WORKER_BINARIES = {"claude": "claude", "codex": "codex"}

# How long a worker turn may take when the project says nothing, in seconds.
# The doctor's round-trip is held to the same window a turn is, read from the
# same setting.
DEFAULT_WORKER_TIMEOUT = 90

WORKER_ENV_DROP = ("TELEGRAM_SERVICE_LAUNCH_NONCE", "SSH_AUTH_SOCK")
WORKER_ENV_DROP_PREFIXES = ("CLAUDE_CODE_", "CLAUDECODE", "VSCODE_")


def scrub_worker_env(environ):
    """A copy of `environ` fit to hand a worker binary.

    The launch nonce is the daemon's proof of ownership and the agent-channel
    keys are a way into another editor session; neither has business in a
    child. The forwarded ssh agent is a credential the worker never asked for.
    """
    env = dict(environ)
    for name in WORKER_ENV_DROP:
        env.pop(name, None)
    for name in [key for key in env if key.startswith(WORKER_ENV_DROP_PREFIXES)]:
        env.pop(name, None)
    return env


def _kill_process_group(proc):
    """Kill the process group created for a worker, even if its leader already exited."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False


def _first_json_document(output, answer_keys, log=None):
    """Return the first JSON object carrying a harness answer key.

    Claude hooks may write their own JSON documents to stdout before or after
    the headless result. Those documents are part of the host protocol, not a
    reason to discard a completed worker answer. `log`, when given, is told
    about the foreign output; the daemon passes its own log, the doctor none.
    """
    decoder = json.JSONDecoder()
    text = output or ""
    index = 0
    skipped = []
    answer = None
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError as exc:
            if answer is not None:
                skipped.append("non-json trailing output")
                break
            raise RuntimeError(
                "worker stdout carried malformed output before its answer") from exc
        if answer is None and isinstance(value, dict) \
                and any(key in value for key in answer_keys):
            answer = value
        else:
            skipped.append(",".join(sorted(value)[:3])
                           if isinstance(value, dict) else type(value).__name__)
        index = end
    if answer is not None:
        if skipped and log is not None:
            log("worker stdout carried foreign output alongside the answer: "
                + "; ".join(skipped))
        return answer
    detail = f" (saw: {'; '.join(skipped)})" if skipped else ""
    raise RuntimeError("worker stdout carried no answer document" + detail)


def claude_failure_reason(stdout, stderr, rc):
    """Why a claude run failed, taken from where claude actually says so.

    A failed headless run answers with the same document a good one answers
    with: `subtype` stays "success" while `is_error` turns true, so `result` is
    the only field carrying a sentence a person can read. That document was
    read on the successful path alone, so a failure reported whichever host
    noise stderr happened to carry — and delivered the raw document to the chat
    when stderr carried nothing.

    Most non-zero exits carry no document at all. A rejected session id, an
    empty prompt, an unknown flag each leave stdout empty and put a single line
    on stderr, so stderr and then the exit code stay the answer for them.
    """
    try:
        document = _first_json_document(stdout, ("result", "is_error", "subtype"))
    except RuntimeError:
        document = None
    if isinstance(document, dict):
        reason = str(document.get("result") or "").strip()
        if reason:
            return reason
    return (stderr or "").strip() or (stdout or "").strip() or f"exit {rc}"


def _claude_turn_completed(document):
    """True only when claude's own result document confirms a successful turn end.

    The codex counterpart reads `turn.completed` off the event stream. Claude
    states the same thing in one object: `subtype` carries the verdict and
    `is_error` the flag, and "success" with `is_error` false is the only pair it
    writes for a turn that ran to its end. `error_max_turns`,
    `error_during_execution` and a run that never got far enough to write a
    verdict all leave the completion unstated, and an unstated completion is a
    failure here exactly as it is on codex. The answer comes from what the
    engine reports, never from the emptiness of the text.
    """
    if not isinstance(document, dict):
        return False
    return (not document.get("is_error")
            and str(document.get("subtype") or "") == "success")


# Lines codex prints on every run, fatal or not. The first of them is what the
# service used to report as the cause of a failure, so a refusal the model
# actually gave arrived as a note about stdin.
CODEX_ROUTINE_STDERR = (
    "Reading additional input from stdin...",
)


def _unwrap_engine_message(text):
    """The sentence inside an engine error, when the engine wrapped one.

    Codex forwards the provider's HTTP error verbatim, so `message` is often a
    JSON document whose own `error.message` is the only part a person can read.
    """
    value = (text or "").strip()
    for _ in range(3):
        if not (value.startswith("{") and value.endswith("}")):
            break
        try:
            obj = json.loads(value)
        except ValueError:
            break
        if not isinstance(obj, dict):
            break
        inner = obj.get("error")
        if isinstance(inner, dict) and inner.get("message"):
            value = str(inner["message"]).strip()
            continue
        if isinstance(inner, str) and inner.strip():
            value = inner.strip()
            continue
        if obj.get("message"):
            value = str(obj["message"]).strip()
            continue
        break
    return value


def codex_failure_reason(stdout, stderr, rc):
    """Why a codex run failed, taken from where codex actually says so.

    The protocol carries the refusal — `turn.failed`, then a top-level `error`
    event — while stderr carries routine chatter that happens to come first.
    Reporting stderr's first line therefore named the wrong cause every time,
    and a run stopped by an exhausted quota read as a note about stdin. The
    order below is the order of authority: the turn's own verdict, then any
    fatal event, then whatever stderr had left once the routine lines are gone.
    """
    fatal, events, items = None, [], []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            if message:
                fatal = str(message)
        elif kind == "error" and event.get("message"):
            events.append(str(event["message"]))
        elif kind in ("item.completed", "item.started"):
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "error" and item.get("message"):
                items.append(str(item["message"]))
    for candidate in (fatal, events[-1] if events else None, items[-1] if items else None):
        if candidate:
            return _unwrap_engine_message(candidate)
    residue = [line for line in (stderr or "").splitlines()
               if line.strip() and line.strip() not in CODEX_ROUTINE_STDERR]
    if residue:
        return residue[-1].strip()
    return f"exit {rc}"


# What an engine says when the subscription is spent rather than when the work
# is wrong. A paused queue and a failed job are different states, and the only
# thing that separates them is this sentence.
QUOTA_EXHAUSTED_RE = re.compile(
    r"usage limit|rate.?limit|quota|too many requests|\b429\b|insufficient_quota"
    r"|limit reached|limit will reset|resets? (at|in)\b",
    re.IGNORECASE,
)


def is_quota_exhausted(reason):
    return bool(QUOTA_EXHAUSTED_RE.search(str(reason or "")))


# What an engine says when it will not run the model it was handed, rather than
# when the work is wrong or the subscription is spent. The two answer in
# completely different registers — codex forwards the provider's invalid-request
# trace verbatim, claude writes a sentence — so both shapes are named here
# rather than derived from one of them.
MODEL_REFUSED_RE = re.compile(
    r"\bmodel_not_found\b"
    r"|\b(?:unknown|unsupported|unrecognized|unrecognised|invalid)[ _-]model\b"
    r"|\bmodel\b[^\n]{0,100}?"
    r"(?:does not exist|do not exist|may not exist|not have access"
    r"|requires a newer version|is not supported|is not available)",
    re.IGNORECASE,
)


def is_model_refused(reason):
    """True when the binary refused the model rather than the work.

    Read behind the quota sentence, never beside it. A subscription spent on one
    model says both things at once, and only one of them is a pause that can be
    waited out; taking the other reading would retire a job the queue would
    otherwise have resumed by itself.
    """
    text = str(reason or "")
    return bool(MODEL_REFUSED_RE.search(text)) and not is_quota_exhausted(text)


class WorkerModelRefused(RuntimeError):
    """The worker binary will not run the model it was configured with.

    This ending is named because nothing about it is worth another attempt: the
    same binary refuses the same model the same way every time, and no amount of
    starting over changes a setting. It is also the one failure that happens
    before any work does, so there is nothing half-done to protect.

    It carries two texts and keeps them apart, because they are read by
    different people. `str(...)` is the notice — the product's own words, and
    the whole of what a caller on a call is told, since that path speaks
    whatever it is handed to whoever is on the line. The engine's own sentence
    stays on `reason`, for the surfaces that answer to an operator: naming the
    ending is what was missing, and it is not worth having if the price is the
    only text that says what actually refused.
    """

    def __init__(self, notice, reason=""):
        super().__init__(notice)
        self.notice = notice
        self.reason = str(reason or "")


def model_refusal_notice(binary, model):
    """What a person is told when the binary will not run the model.

    The binary and the model are the whole of the fact, and the fact is that
    these two will not run together — both taken from what the run was
    configured with rather than from the refusal's own prose, which names them
    in whatever shape the provider chose and often in none a person can act on.

    Which of the two is the one to move is not something the daemon knows. The
    same refusal is returned for a model that does not exist and for a binary
    too old for one that does, so the notice states the pairing and stops
    rather than sending a person to the setting that may not be holding it.
    """
    named = f'the model "{model}"' if model else "its default model"
    return (f"{binary} will not run {named}. Nothing ran, and a retry is "
            "refused the same way.")


def worker_failure(binary, model, reason):
    """The exception a non-zero worker exit raises, named by what ended it."""
    if is_model_refused(reason):
        return WorkerModelRefused(model_refusal_notice(binary, model), reason)
    return RuntimeError(f"{binary} worker failed: {str(reason)[:500]}")


def worker_diagnosis(exc, fallback):
    """What an operator is told about a worker that ended badly.

    A named ending keeps the engine's own words *under* the name rather than in
    place of it. The name is the classification, which an operator has no other
    way to get; the engine's sentence is the only text that says what actually
    refused, and it is the one an operator acts on. Anything the daemon has not
    named reads exactly as it read before there was a name for anything.
    """
    if isinstance(exc, WorkerModelRefused) and exc.reason:
        # Flattened and bounded exactly as `_short_error` flattens and bounds
        # every other reason that reaches these same surfaces, so one line of
        # log stays one line of log and a row stays a row.
        said = exc.reason.replace("\n", " ")[:500]
        return f"{exc.notice} It said: {said}"
    return fallback


def _codex_turn_completed(stdout):
    """True only when Codex's JSONL protocol confirms a successful turn end."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            return True
    return False


# A project's session hook is what keeps generated context level with the body it
# is compiled from, and the engine runs one only against persisted hook trust.
# Granting that trust is an interactive act, so a daemon never has it, and the
# refusal is silent: the hook is skipped and the turn answers from whatever the
# last interactive run left on disk. The bypass gives away nothing here - the
# same command already runs with approvals and sandbox off, so a hook can do
# nothing this turn could not do anyway.
_CODEX_HOOK_TRUST = None


def codex_hook_trust():
    """The bypass flag, asked of the engine once rather than assumed.

    An engine that predates the flag exits on it before reaching the model, which
    would take every turn with it, so support is read from its own help and the
    answer is kept for the life of the process."""
    global _CODEX_HOOK_TRUST
    if _CODEX_HOOK_TRUST is None:
        flag = "--dangerously-bypass-hook-trust"
        try:
            offered = subprocess.run([WORKER_BINARIES["codex"], "exec", "--help"],
                                     capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            offered = ""
        _CODEX_HOOK_TRUST = [flag] if flag in offered else []
    return _CODEX_HOOK_TRUST


def claude_command(prompt, settings=None, *, resume_session=None):
    """Headless `claude -p`. --output-format json carries the reply (.result)
    plus usage / cost / model / session metadata in one object.
    --dangerously-skip-permissions gives full tool access (this is the isolated
    agent box, mirroring the codex worker); the behavioural boundary is the
    soft-gate in context.md, not a permission gate."""
    settings = settings or {}
    cmd = [WORKER_BINARIES["claude"], "-p", prompt, "--output-format", "json",
           "--dangerously-skip-permissions"]
    if resume_session:
        cmd += ["--resume", str(resume_session)]
    model = settings.get("model")
    if model:
        cmd += ["--model", model]
    effort = settings.get("effort")
    if effort:
        cmd += ["--effort", effort]
    return cmd


def codex_command(prompt, out, settings=None, *, resume_session=None):
    """Headless `codex exec`. Full access (bypass approvals+sandbox) mirrors the
    claude worker's; --skip-git-repo-check because /app is not a git repo. The
    final message comes from -o; --json carries usage metadata on stdout.

    With `resume_session` the run continues that thread instead of opening one.
    The `resume` subcommand takes the same flags with a single exception: it has
    no --color, and passing it exits before the model is ever reached. With --json
    on a pipe there is nothing to colourise anyway."""
    settings = settings or {}
    binary = WORKER_BINARIES["codex"]
    if resume_session:
        cmd = [binary, "exec", "resume", str(resume_session), prompt,
               "--dangerously-bypass-approvals-and-sandbox",
               *codex_hook_trust(),
               "--skip-git-repo-check", "--json", "-o", out]
    else:
        cmd = [binary, "exec", prompt,
               "--dangerously-bypass-approvals-and-sandbox",
               *codex_hook_trust(),
               "--skip-git-repo-check", "--json", "--color", "never", "-o", out]
    model = settings.get("model")
    if model:
        cmd += ["-m", model]
    reasoning = settings.get("reasoning_effort")
    if reasoning:
        cmd += ["-c", f'model_reasoning_effort="{reasoning}"']
    service_tier = settings.get("service_tier")
    if service_tier:
        cmd += ["-c", f'service_tier="{service_tier}"']
    return cmd


def worker_failure_reason(worker, stdout, stderr, rc):
    """Why a run on `worker` failed, read where that binary says so."""
    if worker == "claude":
        return claude_failure_reason(stdout, stderr, rc)
    return codex_failure_reason(stdout, stderr, rc)


def worker_turn_completed(worker, stdout):
    """True only when the binary's own output confirms a turn ran to its end."""
    if worker == "claude":
        try:
            document = _first_json_document(stdout, ("result", "is_error", "subtype"))
        except RuntimeError:
            return False
        return _claude_turn_completed(document)
    return _codex_turn_completed(stdout)


# What an engine says when it has no login to run under. Claude writes a
# sentence; codex forwards the provider's 401 on every reconnect and then on
# the failed turn. Neither is a refusal of the model, since the model was never
# reached, and neither is a spent subscription.
NOT_AUTHENTICATED_RE = re.compile(
    r"\bnot logged in\b|\b401\b|\bunauthori[sz]ed\b|\binvalid api key\b"
    r"|\brun /login\b|\bmissing bearer\b",
    re.IGNORECASE,
)


def is_not_authenticated(reason):
    return bool(NOT_AUTHENTICATED_RE.search(str(reason or "")))


# The whole of what the doctor asks a binary. Nothing about it needs a tool,
# a file or a project, so the answer is the binary's verdict on the model and
# nothing else.
PROBE_PROMPT = "Reply with the single word OK."


def probe_worker(worker, model, *, timeout, cwd, environ=None):
    """One minimal round-trip with the binary that runs `worker`, on `model`.

    The command is the one a turn runs — same binary, same flags, the model
    passed the same way — so what the binary refuses here is what it would
    refuse a turn. `model` None is the binary's own default. The run is held to
    `timeout` seconds and its whole process group is killed at the bound, so
    the answer arrives in bounded time whatever the binary does.

    The verdict is one of: `accepted`, `model_refused`, `quota_exhausted`,
    `not_authenticated`, `binary_missing`, `timed_out`, `failed`, or
    `in_process` for a worker that has no binary to ask. Only `accepted` and
    `in_process` are ok. `reason` is the binary's own text; `notice` is the
    product's sentence for a refused model.
    """
    binary = WORKER_BINARIES.get(worker)
    if binary is None:
        return {"ok": True, "verdict": "in_process", "binary": None,
                "reason": None, "notice": None, "seconds": 0.0}
    env = scrub_worker_env(environ if environ is not None else os.environ)
    if shutil.which(binary, path=env.get("PATH")) is None:
        return {"ok": False, "verdict": "binary_missing", "binary": binary,
                "reason": f"{binary} is not on PATH", "notice": None, "seconds": 0.0}
    started = time.monotonic()
    fd, out = tempfile.mkstemp(prefix="tg-doctor-", suffix=".txt", dir=cwd)
    os.close(fd)
    try:
        if worker == "claude":
            cmd = claude_command(PROBE_PROMPT, {"model": model})
        else:
            cmd = codex_command(PROBE_PROMPT, out, {"model": model})
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True,
                cwd=str(cwd), env=env)
        except OSError as exc:
            return {"ok": False, "verdict": "binary_missing", "binary": binary,
                    "reason": f"{binary} could not be started: {exc}",
                    "notice": None, "seconds": round(time.monotonic() - started, 3)}
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            proc.communicate()
            return {"ok": False, "verdict": "timed_out", "binary": binary,
                    "reason": f"{binary} gave no verdict within {timeout:g}s",
                    "notice": None, "seconds": round(time.monotonic() - started, 3)}
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
    seconds = round(time.monotonic() - started, 3)
    if proc.returncode == 0 and worker_turn_completed(worker, stdout):
        return {"ok": True, "verdict": "accepted", "binary": binary,
                "reason": None, "notice": None, "seconds": seconds}
    reason = worker_failure_reason(worker, stdout, stderr, proc.returncode)
    if is_model_refused(reason):
        verdict, notice = "model_refused", model_refusal_notice(binary, model)
    elif is_quota_exhausted(reason):
        verdict, notice = "quota_exhausted", None
    elif is_not_authenticated(reason):
        verdict, notice = "not_authenticated", None
    else:
        verdict, notice = "failed", None
    return {"ok": False, "verdict": verdict, "binary": binary,
            "reason": reason.replace("\n", " ")[:500], "notice": notice,
            "seconds": seconds}
