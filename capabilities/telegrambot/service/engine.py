"""The telegrambot service engine: one process serving the bots of a set of projects.

The CLI owns everything a project is - its identity, its policy, its
connections, its settings - and hands this module a host that answers those
questions inside the project's own resolution. This module owns what the
process does with the answers: one getUpdates long poll per bot, each bot's
dialogue and job runner, and the files on this machine that say how it runs.

Project mode is the case of a set of one: `telegrambot service run` serves the
project it is started in, `telegrambot service run --machine` every project on
the machine's opt-in list, through the same code. One lock per project, the
same in both modes, decides which process serves it.

An update's offset is confirmed to Telegram (by the next poll) only once the
dialogue has recorded what it does with it, so a restart is handed again what
it had not settled; the dialogue's reservation makes that harmless.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import fcntl
import hashlib
import json
import os
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

LOG_FILE = "daemon.log"
PID_FILE = "daemon.pid"
LOCK_FILE = "daemon.lock"
STATUS_FILE = "daemon.json"
SERVE_LOCK = "serve.lock"
MACHINE_TAG = "machine"
ADMIT_SECONDS = 10.0
CHECK_SECONDS = 30.0
PUBLISH_SECONDS = 10.0
TICK_SECONDS = 0.5


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def fingerprint_of(*parts: bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
        digest.update(b"\0")
    return digest.hexdigest()[:16]


class Reader:
    """One bot's history door: an event loop of its own on a thread, the MTProto
    session opened on first use and kept, reopened after a failure. A read never
    holds a turn past its deadline."""

    def __init__(self, transport, token: str, api_id: int, api_hash: str, state_dir: Path):
        self.t = transport
        self.args = (token, api_id, api_hash, state_dir)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True,
                                       name="history")
        self.thread.start()
        self.reader = None
        self.lock = threading.Lock()

    def _call(self, factory, deadline: float):
        future = asyncio.run_coroutine_threadsafe(asyncio.wait_for(factory(), deadline),
                                                  self.loop)
        try:
            return future.result(deadline + 2)
        except (asyncio.TimeoutError, TimeoutError):
            future.cancel()
            raise self.t.TelegramError(5, "history_timeout",
                                       f"the read did not finish within {deadline:g}s") from None

    def __call__(self, chat_id, anchor, *, limit, topic_id=None, is_forum=False,
                 reply_chain_of=None, max_requests=5):
        t = self.t

        async def read():
            if self.reader is None:
                fresh = t.HistoryReader(*self.args)
                await fresh.open()
                self.reader = fresh
            reading = await self.reader.read_back(chat_id, anchor, limit=limit,
                                                  topic_id=topic_id, is_forum=is_forum,
                                                  max_requests=max_requests)
            if reply_chain_of:
                seen = {m.message_id for m in reading.messages}
                target, steps, chain = reply_chain_of, 0, []
                while target and target not in seen and steps < t.REPLY_CHAIN_STEPS and \
                        reading.requests < max_requests:
                    steps += 1
                    reading.requests += 1
                    _raw, shaped = await self.reader.one(chat_id, target, is_forum)
                    if shaped is None:
                        break
                    chain.append(shaped)
                    seen.add(shaped.message_id)
                    target = shaped.reply_to
                reading.messages = sorted(chain + reading.messages, key=lambda m: m.message_id)
            return reading

        with self.lock:
            try:
                return self._call(read, t.HISTORY_DEADLINE)
            except t.TelegramError:
                self._drop()
                raise
            except Exception as exc:
                self._drop()
                raise t.mtproto_failure(exc) from None

    def _drop(self) -> None:
        reader, self.reader = self.reader, None
        if reader is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(reader.close(), self.loop).result(5)

    def close(self) -> None:
        with self.lock:
            self._drop()
        self.loop.call_soon_threadsafe(self.loop.stop)


class Bot:
    """One bot of one project: its poll, its dialogue and its job runner."""

    def __init__(self, slot: "Slot", plan: dict):
        self.slot = slot
        self.plan = plan
        self.cid = plan["connection"]
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.state, self.reason = "starting", None
        self.dialogue = None
        self.jobs = None
        self.reader = None
        self.api = None
        self.last_poll: str | None = None
        self.offset: int | None = None

    @property
    def engine(self) -> "Engine":
        return self.slot.engine

    def identity(self) -> tuple:
        return self.plan["identity"]

    def log(self, message: str) -> None:
        self.slot.log(f"{self.cid}: {message}")

    def row(self) -> dict:
        out = {"connection": self.cid, "bot": self.plan.get("username"), "state": self.state,
               "reason": self.reason, "last_poll_at": self.last_poll,
               "history": "on" if self.plan.get("api_id") else
               f"off: {self.plan.get('history_off')}",
               "voice": "on" if self.plan.get("deepgram") else f"off: {self.plan.get('voice_off')}"}
        if self.dialogue is not None:
            out["dialogue"] = self.dialogue.summary()
        return out

    def start(self) -> None:
        self.thread = threading.Thread(target=self.loop, daemon=True,
                                       name=f"poll-{self.slot.slug}-{self.cid}")
        self.thread.start()

    def halt(self) -> None:
        self.stop.set()

    def build(self) -> None:
        host, t = self.engine.host, self.engine.transport
        plan = self.plan
        self.api = host.bot_api(plan["token"])
        me = self.api.get_me()
        plan["username"] = me.get("username")
        hook = self.api.webhook_info()
        if hook.get("url"):
            raise t.TelegramError(5, "webhook_set", "a webhook is set on this bot, so it "
                                  "cannot be polled here",
                                  "remove it (deleteWebhook) where it was set, or use another bot")
        if plan.get("api_id"):
            self.reader = Reader(t, plan["token"], plan["api_id"], plan["api_hash"],
                                 plan["state_dir"])
        modules = self.engine.modules
        store = self.slot.store
        self.dialogue = modules["dialogue"].Dialogue(
            agent_id=self.slot.project_id, connection=self.cid, root=Path(self.slot.entry["root"]),
            api=self.api, me=me, store=store, settings=self.slot.plan["settings"],
            state_dir=plan["state_dir"], profiles=modules["profiles"], gate=modules["gate"],
            read_history=self.reader, history_off=plan.get("history_off"),
            deepgram=plan.get("deepgram"), voice_off=plan.get("voice_off"),
            transcribe=t.transcribe, cli_path=host.cli_path(),
            turn_env=host.turn_env(self.slot.entry), log=self.log,
            on_reload=lambda: self.engine.request_reload(self.slot.project_id),
            from_update=t.from_update, run=host.run_override())
        jobs = modules["jobs"]
        self.jobs = jobs.JobRunner(dialogue=self.dialogue, db=jobs.Db(store),
                                   owner_id=f"{self.engine.holder}:{time.time_ns()}",
                                   node=self.engine.node, run=host.run_override())
        self.dialogue.jobs = self.jobs
        self.jobs.start()
        with contextlib.suppress(Exception):
            self.api.set_commands(modules["dialogue"].BOT_MENU)
        self.log(f"serving @{me.get('username')}; history "
                 f"{'on' if self.reader else 'off: ' + str(plan.get('history_off'))}; voice "
                 f"{'on' if plan.get('deepgram') else 'off: ' + str(plan.get('voice_off'))}")

    def loop(self) -> None:
        t = self.engine.transport
        backoff = 1.0
        last_check = time.monotonic()
        while not self.stop.is_set():
            try:
                if self.slot.paused:
                    self.state, self.reason = "paused", "the project is not served now"
                    self.stop.wait(2.0)
                    continue
                if time.monotonic() - last_check > CHECK_SECONDS:
                    last_check = time.monotonic()
                    refusal = self.engine.host.check(self.slot.entry, self.cid)
                    if refusal:
                        self.state, self.reason = refusal
                        self.stop.wait(CHECK_SECONDS)
                        continue
                if self.dialogue is None:
                    self.build()
                updates = self.api.updates(self.offset, timeout=t.POLL_SECONDS
                                           if not self.engine.fast else 0)
                self.last_poll = now_iso()
                self.state, self.reason = "served", None
                for update in updates:
                    if self.stop.is_set():
                        break
                    self.handle(update)
                    self.offset = int(update["update_id"]) + 1
                backoff = 1.0
                if self.engine.fast:
                    self.stop.wait(0.05)
            except Exception as exc:
                code = getattr(exc, "code", type(exc).__name__)
                self.state, self.reason = "error", f"{code}: {getattr(exc, 'message', exc)}"
                self.log(f"poll failed: {self.reason}")
                wait = 60.0 if getattr(exc, "exit_code", None) == 2 else min(backoff, 60.0)
                self.stop.wait(wait)
                backoff = min(backoff * 2, 60.0)
        self.shutdown()
        self.state = "stopped"

    def handle(self, update: dict) -> None:
        """Offer one update; a store outage keeps it unconfirmed and is raised."""
        store_errors = self.engine.modules["store"].StoreUnavailable
        try:
            self.dialogue.offer_update(update)
        except store_errors:
            raise
        except Exception as exc:
            if type(exc).__module__.startswith("psycopg"):
                raise
            self.log(f"update {update.get('update_id')} not handled: "
                     f"{type(exc).__name__}: {exc}")

    def tick(self) -> None:
        if self.dialogue is not None:
            try:
                self.dialogue.tick()
            except Exception as exc:
                self.log(f"tick failed: {type(exc).__name__}: {exc}")

    def shutdown(self) -> None:
        if self.dialogue is not None:
            with contextlib.suppress(Exception):
                self.dialogue.shutdown()
        if self.jobs is not None:
            with contextlib.suppress(Exception):
                self.jobs.shutdown()
        if self.reader is not None:
            with contextlib.suppress(Exception):
                self.reader.close()
        if self.api is not None:
            with contextlib.suppress(Exception):
                self.api.close()


class Slot:
    """One project as the process sees it."""

    def __init__(self, engine: "Engine", project_id: str, entry: dict):
        self.engine = engine
        self.project_id = project_id
        self.entry = entry
        self.slug = entry.get("slug") or project_id
        self.plan: dict | None = None
        self.store = None
        self.bots: dict[str, Bot] = {}
        self.state, self.reason = "pending", None
        self.reload_error: str | None = None
        self.paused = False
        self.next_admit = 0.0
        self.lock_handle = None

    def log(self, message: str) -> None:
        self.engine.log(self.slug, message)

    def row(self) -> dict:
        return {"project": self.slug, "project_id": self.project_id,
                "root": self.entry.get("root"), "state": self.state, "reason": self.reason,
                "fingerprint": (self.plan or {}).get("fingerprint"),
                "reload_error": self.reload_error,
                "bots": [bot.row() for bot in self.bots.values()]}

    def take_lock(self) -> bool:
        if self.lock_handle is not None:
            return True
        path = self.engine.host.serve_lock(self.entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self.lock_handle = handle
        return True

    def admit(self) -> None:
        if not self.take_lock():
            self.state, self.reason = "refused", "another process serves this project"
            self.next_admit = time.monotonic() + ADMIT_SECONDS
            return
        plan, refusal = self.engine.host.admit(self.project_id, self.entry)
        if refusal:
            if (self.state, self.reason) != refusal:
                self.log(f"not served: {refusal[0]}: {refusal[1]}")
            self.state, self.reason = refusal
            self.next_admit = time.monotonic() + ADMIT_SECONDS
            return
        try:
            self.store = self.engine.store_for(plan["setting"])
        except Exception as exc:
            self.state, self.reason = "refused", f"{getattr(exc, 'slug', 'store')}: {exc}"
            self.log(f"not served: {self.reason}")
            self.next_admit = time.monotonic() + ADMIT_SECONDS
            return
        self.clear_reservations(plan)
        self.adopt(plan)
        self.state, self.reason = "served", None
        self.log(f"served: {len(self.bots)} bot(s)")

    def clear_reservations(self, plan: dict) -> None:
        """What a previous process took and never finished is not acted on again:
        its reservations are moved past, and the person can ask once more."""
        for cid in plan["bots"]:
            with contextlib.suppress(Exception):
                for row in self.store.stale_reservations(self.project_id, cid):
                    self.store.finish(self.project_id, cid, row["chat_id"], row["topic_id"],
                                      list(row["reserved"]))
                    self.log(f"{cid}: {row['chat_id']} messages {list(row['reserved'])} were "
                             "taken by a process that stopped; not acted on again")

    def adopt(self, plan: dict) -> None:
        self.plan = plan
        wanted = plan["bots"]
        for cid in list(self.bots):
            bot = self.bots[cid]
            if cid not in wanted or wanted[cid]["identity"] != bot.identity():
                bot.halt()
                self.bots.pop(cid)
                self.log(f"{cid}: stopped ({'dropped' if cid not in wanted else 'changed'})")
        for cid, bot_plan in wanted.items():
            if cid in self.bots:
                self.bots[cid].plan.update({k: v for k, v in bot_plan.items()
                                            if k != "username"})
                if self.bots[cid].dialogue is not None:
                    self.bots[cid].dialogue.update_settings(plan["settings"])
                continue
            bot = Bot(self, bot_plan)
            self.bots[cid] = bot
            bot.start()

    def reload(self) -> str | None:
        if self.plan is None:
            self.next_admit = 0.0
            return None
        plan, refusal = self.engine.host.admit(self.project_id, self.entry)
        if refusal:
            self.reload_error = f"{refusal[0]}: {refusal[1]}"
            self.log(f"reload rejected, the previous declaration stays: {self.reload_error}")
        elif self.engine.store_key(plan["setting"]) != self.engine.store_key(self.plan["setting"]):
            self.reload_error = "the project now resolves another store; that takes a restart"
            self.log(f"reload rejected: {self.reload_error}")
        else:
            self.reload_error = None
            self.adopt(plan)
            self.log(f"reloaded: fingerprint {plan['fingerprint']}")
        for bot in self.bots.values():
            if bot.dialogue is not None:
                bot.dialogue.after_reload(self.reload_error)
        return self.reload_error

    def tick(self) -> None:
        for bot in list(self.bots.values()):
            bot.tick()

    def release(self, said: str) -> None:
        for bot in self.bots.values():
            bot.halt()
        for bot in self.bots.values():
            if bot.thread is not None:
                bot.thread.join(timeout=10.0)
        self.bots.clear()
        if self.lock_handle is not None:
            with contextlib.suppress(Exception):
                self.lock_handle.close()
            self.lock_handle = None
        self.log(said)


class Engine:
    """The process: its projects, its status, its signals."""

    def __init__(self, host, *, mode: str, state_dir: Path, modules: dict, transport,
                 echo: bool = True, fast: bool = False):
        self.host = host
        self.mode = mode
        self.state_dir = Path(state_dir)
        self.modules = modules
        self.transport = transport
        self.echo = echo
        self.fast = fast
        self.node = host.node()
        self.holder = f"{self.node}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.slots: dict[str, Slot] = {}
        self.stores: dict[tuple, object] = {}
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.reload_requested = threading.Event()
        self.reload_projects: set = set()
        self.list_witness = None
        self.started_at = now_iso()
        self.last_publish = 0.0
        self.fingerprint: str | None = None
        self.log_lock = threading.Lock()

    def log(self, project: str, message: str) -> None:
        text = " ".join(part.strip() for part in str(message).splitlines() if part.strip())
        line = f"{now_iso()} telegrambot service [{project}]: {text}\n"
        with self.log_lock:
            with contextlib.suppress(OSError):
                with (self.state_dir / LOG_FILE).open("a", encoding="utf-8") as handle:
                    handle.write(line)
            if self.echo:
                with contextlib.suppress(Exception):
                    sys.stderr.write(line)
                    sys.stderr.flush()

    def store_key(self, setting) -> tuple:
        kwargs = setting.connect_kwargs()
        return (tuple(sorted((k, str(v)) for k, v in kwargs.items())), setting.schema)

    def store_for(self, setting):
        key = self.store_key(setting)
        if key not in self.stores:
            self.stores[key] = self.host.open_store(setting)
        return self.stores[key]

    def request_reload(self, project_id: str | None = None) -> None:
        if project_id:
            self.reload_projects.add(project_id)
        self.reload_requested.set()
        self.wake.set()

    def status(self) -> dict:
        return {"mode": self.mode, "pid": os.getpid(), "node": self.node, "holder": self.holder,
                "started_at": self.started_at, "published_at": now_iso(),
                "fingerprint": self.fingerprint,
                "projects": [slot.row() for slot in sorted(self.slots.values(),
                                                           key=lambda s: s.slug)]}

    def publish(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.last_publish < PUBLISH_SECONDS:
            return
        self.last_publish = time.monotonic()
        path = self.state_dir / STATUS_FILE
        spare = path.with_name(f".{path.name}.{os.getpid()}")
        with contextlib.suppress(OSError):
            spare.write_text(json.dumps(self.status(), indent=2, default=str))
            os.replace(spare, path)

    def run(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        handle = (self.state_dir / LOCK_FILE).open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("busy")
        try:
            (self.state_dir / PID_FILE).write_text(f"{os.getpid()}\n")
            self.fingerprint = self.host.fingerprint()
            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGHUP, lambda *_: self.request_reload())
                signal.signal(signal.SIGTERM, lambda *_: (self.stopping.set(), self.wake.set()))
                signal.signal(signal.SIGINT, lambda *_: (self.stopping.set(), self.wake.set()))
            self.log(MACHINE_TAG if self.mode == "machine" else self.host.tag(),
                     f"started ({self.mode} mode, pid {os.getpid()}, node {self.node})")
            while not self.stopping.is_set():
                self.step()
                self.wake.wait(TICK_SECONDS)
                self.wake.clear()
        finally:
            self.shutdown()
            with contextlib.suppress(OSError):
                (self.state_dir / PID_FILE).unlink()
            handle.close()

    def step(self) -> None:
        if self.reload_requested.is_set():
            self.reload_requested.clear()
            self.reload()
        self.take_list()
        for slot in list(self.slots.values()):
            if slot.plan is None and time.monotonic() >= slot.next_admit:
                try:
                    slot.admit()
                except Exception as exc:
                    slot.state, slot.reason = "error", f"{type(exc).__name__}: {exc}"
                    slot.log(f"not served: {slot.reason}")
                    slot.next_admit = time.monotonic() + ADMIT_SECONDS
            slot.tick()
        self.publish()

    def take_list(self) -> None:
        witness = self.host.list_witness()
        if witness == self.list_witness and self.slots:
            return
        self.list_witness = witness
        try:
            entries = self.host.entries()
        except Exception as exc:
            self.log(MACHINE_TAG, f"the opt-in list does not load: {exc}")
            return
        for project_id in list(self.slots):
            if project_id not in entries:
                self.slots.pop(project_id).release("left the service")
        for project_id, entry in entries.items():
            if project_id not in self.slots:
                self.slots[project_id] = Slot(self, project_id, entry)
        self.publish(force=True)

    def reload(self) -> None:
        only, self.reload_projects = self.reload_projects, set()
        for slot in self.slots.values():
            if only and slot.project_id not in only:
                continue
            try:
                slot.reload()
            except Exception as exc:
                slot.reload_error = f"{type(exc).__name__}: {exc}"
                slot.log(f"reload failed: {slot.reload_error}")
        self.list_witness = None
        self.fingerprint = self.host.fingerprint()
        self.publish(force=True)

    def shutdown(self) -> None:
        for slot in list(self.slots.values()):
            with contextlib.suppress(Exception):
                slot.release("the service stopped")
        for store in self.stores.values():
            with contextlib.suppress(Exception):
                store.close()
        self.log(MACHINE_TAG if self.mode == "machine" else self.host.tag(), "stopped")
        self.publish(force=True)
