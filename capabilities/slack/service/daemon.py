#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["slack_sdk>=3.27"]
# ///
"""slack service daemon — Socket Mode listener (push, not polling).

An admitted message is either answered by a real worker (headless claude/codex,
or a stub for tests) running off the listener thread, or relayed to a local inbox.
Pure decision logic lives in policy.py / register.py / watermark.py / authority.py /
prompt.py / dispatcher.py / workers.py; this module wires them to Slack: reaction ack,
per-conversation dispatch, worker orchestration with an outbox drain, control keywords,
and a bounded catch-up pass on (re)connect. slack_sdk is imported lazily inside
connect_and_listen() so process_event and the pure helpers stay unit-testable without it."""

import json
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # policy/register siblings

from policy import (parse_event, accept, route, conversation_key,
                    resolve_role, control_command, control_allowed,
                    select_catchup)  # noqa: E402
from register import Register  # noqa: E402
from watermark import Watermark  # noqa: E402
from dispatcher import Dispatcher  # noqa: E402
from workers import WORKERS, WorkerTimeout  # noqa: E402
from authority import build_auth_context, summarize  # noqa: E402
from prompt import build_prompt  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


def event_key(evt: dict) -> str:
    return f"{evt['channel']}:{evt['ts']}"


def _append_inbox(inbox_path: Path, evt: dict) -> None:
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    with inbox_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(evt, ensure_ascii=False) + "\n")


def process_event(payload_event, *, settings, register, watermark, inbox_path,
                  post_message, submit_job, owner_dm=None) -> dict:
    """Handle one Slack event up to the queue boundary (transport-agnostic).

    Answer jobs are enqueued via submit_job(conv, job); the worker thread posts
    the reply and marks the register done/error. Relay and control are terminal
    here. post_message(channel, text, thread_ts) sends a reply."""
    evt = parse_event(payload_event)
    if evt is None:
        return {"action": "ignore"}
    if not accept(evt, settings):
        # Parsed a real DM/mention but the accept-gate refused it. Surface it
        # (with the channel id) so an operator can see what to allow; genuine
        # noise (bot/self messages, non-message events) returned above stays silent.
        return {"action": "ignore", "reason": "not_allowed", "kind": evt["kind"],
                "channel": evt["channel"], "user": evt["user"], "ts": evt["ts"],
                "text": evt["text"]}
    key = event_key(evt)
    if not register.reserve(key):
        return {"action": "duplicate", "key": key}
    conv = conversation_key(evt)
    watermark.advance(evt["channel"], evt["ts"])

    cmd = control_command(evt["text"])
    if cmd:
        role = resolve_role(evt, settings)
        register.mark_done(key)
        if control_allowed(cmd, role, settings):
            return {"action": "control", "command": cmd, "conversation": conv,
                    "role": role, "evt": evt, "key": key}
        return {"action": "control_denied", "command": cmd, "conversation": conv,
                "role": role, "evt": evt, "key": key}

    if route(evt, settings) == "relay":
        _append_inbox(Path(inbox_path), evt)
        if owner_dm:
            post_message(owner_dm,
                         f"inbox: message from {evt['user']} in {evt['channel']}", None)
        register.mark_done(key)
        return {"action": "relay", "key": key, "text": evt["text"]}

    role = resolve_role(evt, settings)
    job = {"evt": evt, "key": key, "conversation": conv, "role": role}
    submit_job(conv, job)
    return {"action": "answer", "key": key, "conversation": conv, "text": evt["text"]}


def _load_settings(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _log(log_path, msg: str) -> None:
    line = msg if msg.endswith("\n") else msg + "\n"
    try:
        with Path(log_path).open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        sys.stderr.write(line)


def map_tail(messages, bot_user_id):
    """History/replies (newest-first) → chronological [{sender, text}], skipping
    empty text and non-message subtypes. The bot's own messages are labeled
    'assistant' so the worker sees its side of the dialogue."""
    out = []
    for m in messages or []:
        if m.get("subtype") and not m.get("bot_id"):
            continue
        text = (m.get("text") or "").strip()
        if not text:
            continue
        is_bot = bool(m.get("bot_id")) or (bot_user_id and m.get("user") == bot_user_id)
        out.append({"sender": "assistant" if is_bot else (m.get("user") or "user"),
                    "text": text})
    out.reverse()
    return out


def read_outbox(path, offset):
    """Return (new text lines, new byte offset) appended past `offset`."""
    p = Path(path)
    if not p.exists():
        return [], offset
    texts = []
    try:
        with p.open("r", encoding="utf-8") as fh:
            fh.seek(offset)
            for line in fh:
                offset += len(line.encode("utf-8"))
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                text = str(item.get("text") or "").strip()
                if text:
                    texts.append(text)
    except OSError:
        return texts, offset
    return texts, offset


def synth_payload(channel, message, bot_user_id):
    """Build a Slack-event-shaped payload from a history message for catch-up.
    Channel messages only qualify if they @mention the bot."""
    if message.get("bot_id") or message.get("subtype"):
        return None
    user, ts = message.get("user"), message.get("ts")
    text = message.get("text", "")
    if not (user and ts):
        return None
    if str(channel).startswith("D"):
        return {"type": "message", "channel_type": "im", "user": user,
                "channel": channel, "ts": ts, "text": text,
                "thread_ts": message.get("thread_ts")}
    if bot_user_id and f"<@{bot_user_id}>" in text:
        return {"type": "app_mention", "user": user, "channel": channel, "ts": ts,
                "text": text, "thread_ts": message.get("thread_ts")}
    return None


def worker_env(base_env, *, outbox, conversation, authority_path, real_slack, worker_bin):
    env = dict(base_env)
    env["PATH"] = f"{worker_bin}{os.pathsep}{env.get('PATH', '')}"
    env["SLACK_WORKER_OUTBOX"] = str(outbox)
    env["SLACK_WORKER_CONVERSATION"] = str(conversation)
    if real_slack:
        env["SLACK_REAL_SLACK"] = str(real_slack)
    if authority_path:
        env["CAPABILITIES_AUTH_CONTEXT"] = str(authority_path)
    return env


def _safe(part):
    import re
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(part)).strip("_") or "conv"


def connect_and_listen(*, bot_token, app_token, settings, register, watermark,
                       inbox_path, owner_dm, state_dir, log) -> None:
    # Lazy import: keeps every other module testable without slack_sdk installed.
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    web = WebClient(token=bot_token)
    sm = SocketModeClient(app_token=app_token, web_client=web)

    defaults = settings.get("defaults") or {}
    worker_name = (defaults.get("worker") or "claude").strip().lower()
    if worker_name not in WORKERS:
        worker_name = "claude"
    project = defaults.get("project") or os.environ.get("SLACK_SERVICE_PROJECT_ROOT") or os.getcwd()
    tail_size = int(defaults.get("tail_size") or 30)
    worker_timeout = float(defaults.get("worker_timeout") or 180)
    max_parallel = int(defaults.get("max_parallel_jobs") or 3)
    catch = defaults.get("catch_up") or {}
    max_age = catch.get("max_age_seconds", 3600)
    max_msgs = catch.get("max_messages", 50)
    model = defaults.get("model")
    effort = defaults.get("effort")
    connection = os.environ.get("SLACK_SERVICE_CONNECTION") or settings.get("connection") or "default"

    import shutil
    real_slack = os.environ.get("SLACK_REAL_SLACK") or shutil.which("slack") or ""
    worker_bin = str(Path(__file__).resolve().parent / "worker-bin")
    context_path = os.environ.get("SLACK_SERVICE_CONTEXT") or ""
    context_md = ""
    if context_path and Path(context_path).is_file():
        context_md = Path(context_path).read_text()

    try:
        bot_user_id = web.auth_test().get("user_id")
    except Exception as exc:
        bot_user_id = None
        _log(log, f"auth_test failed: {exc!r}")

    running_procs = {}      # conv -> Popen (for /stop + shutdown)
    stopping = set()        # conversations whose worker /stop just killed
    procs_lock = threading.Lock()

    def post(channel, text, thread_ts):
        for chunk in _chunks(text):
            web.chat_postMessage(channel=channel, text=chunk, thread_ts=thread_ts)

    def _react(channel, ts, name, add=True):
        try:
            (web.reactions_add if add else web.reactions_remove)(
                channel=channel, timestamp=ts, name=name)
        except Exception:
            pass

    def _fetch_tail(evt):
        try:
            if evt["kind"] == "im":
                resp = web.conversations_history(channel=evt["channel"], limit=tail_size)
            else:
                root = evt.get("thread_ts") or evt["ts"]
                resp = web.conversations_replies(channel=evt["channel"], ts=root, limit=tail_size)
            return map_tail(resp.get("messages") or [], bot_user_id)
        except Exception as exc:
            _log(log, f"tail fetch failed for {evt['channel']}: {exc!r}")
            return []

    def run_job(job):
        evt = job["evt"]; conv = job["conversation"]; key = job["key"]; role = job["role"]
        channel, ts = evt["channel"], evt["ts"]
        # Reply as a normal channel message; only stay in-thread if the incoming
        # mention was itself already inside a thread (don't thread a top-level mention).
        thread = None if evt["kind"] == "im" else evt.get("thread_ts")
        outbox = state_dir / "outbox" / f"{_safe(conv)}-{ts}.jsonl"
        authpath = state_dir / "authority" / f"{_safe(conv)}-{ts}.json"
        outbox.parent.mkdir(parents=True, exist_ok=True)
        _react(channel, ts, "eyes", add=True)
        stop_drain = threading.Event()
        drainer = None
        try:
            tail = _fetch_tail(evt)
            sender_entry = (settings.get("allowed_users") or {}).get(evt["user"])
            sender_name = (sender_entry.get("name") if isinstance(sender_entry, dict)
                           else sender_entry) or evt["user"]
            auth = build_auth_context(settings, role=role, connection=connection,
                                      conversation=conv, sender_id=evt["user"],
                                      sender_name=sender_name)
            authority_path = None
            if auth is not None:
                authpath.parent.mkdir(parents=True, exist_ok=True)
                authpath.write_text(json.dumps(auth, ensure_ascii=False, indent=2) + "\n")
                authority_path = str(authpath)
            state = {"now": _now(), "conversation": conv, "kind": evt["kind"],
                     "connection": connection, "worker": worker_name,
                     "sender_name": sender_name, "sender_role": role,
                     "authority_summary": summarize((auth or {}).get("allowed_capabilities")),
                     "request_text": evt.get("text") or ""}
            prompt = build_prompt(context_md, state, tail)
            env = worker_env(os.environ, outbox=str(outbox), conversation=conv,
                             authority_path=authority_path, real_slack=real_slack,
                             worker_bin=worker_bin)

            def _drain_loop():
                off = 0
                while not stop_drain.is_set():
                    lines, off = read_outbox(outbox, off)
                    for text in lines:
                        post(channel, text, thread)
                    stop_drain.wait(1.0)
                lines, off = read_outbox(outbox, off)
                for text in lines:
                    post(channel, text, thread)

            drainer = threading.Thread(target=_drain_loop, daemon=True)
            drainer.start()

            def _on_spawn(proc):
                with procs_lock:
                    running_procs[conv] = proc

            _log(log, f"dispatch conv={conv} worker={worker_name} tail={len(tail)} role={role}")
            result = WORKERS[worker_name](prompt, cwd=project, env=env,
                                          timeout=worker_timeout, model=model,
                                          effort=effort, on_spawn=_on_spawn)
            reply = result["reply"]
            post(channel, reply, thread)
            _react(channel, ts, "eyes", add=False)
            _react(channel, ts, "white_check_mark", add=True)
            register.mark_done(key)
            _log(log, f"answered conv={conv} «{reply[:80]}»")
        except WorkerTimeout as exc:
            _finish_error(channel, ts, thread, key, conv, role, exc, stopping, register, post, _react, _log, log)
        except Exception as exc:
            with procs_lock:
                was_stopped = conv in stopping
            if was_stopped:
                register.mark_done(key)
                _log(log, f"stopped conv={conv}")
            else:
                _finish_error(channel, ts, thread, key, conv, role, exc, stopping, register, post, _react, _log, log)
        finally:
            stop_drain.set()
            if drainer is not None:
                drainer.join(timeout=5)
            with procs_lock:
                running_procs.pop(conv, None)
                stopping.discard(conv)   # per-job flag: clear on every terminal path
            with contextlib_suppress():
                outbox.unlink()
            with contextlib_suppress():
                authpath.unlink()

    dispatcher = Dispatcher(run_job, max_parallel=max_parallel)

    def _status_text():
        with procs_lock:
            active = list(running_procs.keys())
        return (f"running: {len(active)} job(s)"
                + (f" in {', '.join(active)}" if active else "")
                + f"\nworker={worker_name}, max_parallel={max_parallel}")

    def _stop_conversation(conv):
        with procs_lock:
            proc = running_procs.get(conv)
            if proc is not None:
                stopping.add(conv)
        if proc is None:
            return "Nothing is running for this conversation."
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return "Stopped."

    def submit_job(conv, job):
        dispatcher.submit(conv, job)

    def _handle_control(out):
        evt = out["evt"]; conv = out["conversation"]
        # Reply as a normal channel message; only stay in-thread if the incoming
        # mention was itself already inside a thread (don't thread a top-level mention).
        thread = None if evt["kind"] == "im" else evt.get("thread_ts")
        if out["action"] == "control_denied":
            post(evt["channel"], f"nope: {out['command']} is not allowed for role {out['role']}",
                 thread)
            return
        if out["command"] == "status":
            post(evt["channel"], _status_text(), thread)
        elif out["command"] == "stop":
            post(evt["channel"], _stop_conversation(conv), thread)

    def catch_up(reason):
        now_ts = _now_ts()
        channels = set(watermark.keys()) | set((settings.get("allowed_channels") or {}).keys())
        for ch in channels:
            wm = watermark.get(ch)
            if wm is None:
                watermark.advance(ch, now_ts)   # first sighting: no ancient backlog
                continue
            try:
                resp = web.conversations_history(channel=ch, limit=int(max_msgs), oldest=wm)
            except Exception as exc:
                _log(log, f"catch-up skipped {ch}: {exc!r}")
                continue
            selected = select_catchup(resp.get("messages") or [], watermark=wm,
                                      now_ts=now_ts, max_age_seconds=max_age,
                                      max_messages=max_msgs)
            n = 0
            for m in selected:
                payload = synth_payload(ch, m, bot_user_id)
                if payload is None:
                    continue
                out = process_event(payload, settings=settings, register=register,
                                    watermark=watermark, inbox_path=inbox_path,
                                    post_message=post, submit_job=submit_job,
                                    owner_dm=owner_dm)
                if out["action"] in ("control", "control_denied"):
                    _handle_control(out)
                n += 1
            if n:
                _log(log, f"catch-up/{reason} {ch}: processed {n} since {wm}")

    catchup_lock = threading.Lock()

    def _run_catch_up(reason):
        if catchup_lock.acquire(blocking=False):
            try:
                catch_up(reason)
            finally:
                catchup_lock.release()

    def on_request(client, req):
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event", {})
        try:
            out = process_event(event, settings=settings, register=register,
                                watermark=watermark, inbox_path=inbox_path,
                                post_message=post, submit_job=submit_job, owner_dm=owner_dm)
            if out["action"] in ("control", "control_denied"):
                _handle_control(out)
            snip = (out.get("text") or "").replace("\n", " ")[:80]
            quoted = f" «{snip}»" if snip else ""
            if out["action"] != "ignore":
                _log(log, f"{out['action']} {out.get('key', '')}{quoted}")
            elif out.get("reason") == "not_allowed":
                _log(log, f"not_allowed {out['kind']} from {out['user']} "
                          f"in {out['channel']} ts={out['ts']}{quoted}")
        except Exception as exc:
            _log(log, f"error handling event: {exc!r}")

    def on_raw(client, message):
        if isinstance(message, str) and '"type":"hello"' in message:
            threading.Thread(target=_run_catch_up, args=("connect",), daemon=True).start()

    def _graceful_stop(signum, frame):
        _log(log, f"received signal {signum}; killing in-flight workers and exiting")
        with procs_lock:
            procs = list(running_procs.values())
        for proc in procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        os._exit(0)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _graceful_stop)
        except (ValueError, OSError):
            pass

    sm.socket_mode_request_listeners.append(on_request)
    sm.on_message_listeners.append(on_raw)
    _log(log, f"connecting to Slack Socket Mode (worker={worker_name}, project={project})")
    sm.connect()
    threading.Event().wait()  # block forever; SIGTERM from `service stop` ends the process


def _finish_error(channel, ts, thread, key, conv, role, exc, stopping, register,
                  post, react, _log, log):
    react(channel, ts, "eyes", add=False)
    react(channel, ts, "x", add=True)
    detail = f"{type(exc).__name__}: {str(exc)[:300]}"
    notice = (f"Worker error: {detail}" if role == "supervisor"
              else "Something went wrong handling this. Please tell an administrator.")
    try:
        post(channel, notice, thread)
    except Exception as se:
        _log(log, f"failed to post error notice: {se!r}")
    register.mark_error(key)
    _log(log, f"error conv={conv} {detail}")


def _chunks(text, limit=3900):
    text = str(text)
    if len(text) <= limit:
        return [text]
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_ts():
    import time as _t
    return str(_t.time())


import contextlib


def contextlib_suppress():
    return contextlib.suppress(OSError)


def main() -> None:
    settings_path = os.environ.get("SLACK_SERVICE_SETTINGS", "")
    state_dir = Path(os.environ.get("SLACK_SERVICE_STATE_DIR", "."))
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    log_path = state_dir / "daemon.log"

    if not bot_token or not app_token:
        _log(log_path, "missing SLACK_BOT_TOKEN or SLACK_APP_TOKEN; exiting")
        sys.exit(2)

    settings = _load_settings(settings_path)
    register = Register(state_dir / "register.json")
    watermark = Watermark(state_dir / "watermarks.json")
    inbox_path = state_dir / "inbox.jsonl"
    owner_dm = (settings.get("inbox") or {}).get("notify_owner")

    connect_and_listen(bot_token=bot_token, app_token=app_token, settings=settings,
                       register=register, watermark=watermark, inbox_path=inbox_path,
                       owner_dm=owner_dm, state_dir=state_dir, log=log_path)


if __name__ == "__main__":
    main()
