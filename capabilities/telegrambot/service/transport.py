"""The telegrambot transport: the HTTP Bot API, and history read back by number.

A bot has two doors to Telegram, and this module is both of them. New messages
arrive and every write leaves through the HTTP Bot API (`getUpdates`,
`sendMessage`, `setMessageReaction`, `sendChatAction`, ...). History has no Bot
API method at all, so it is read through a separate MTProto session the bot's
own token signs in, with updates switched off, by message number
(`messages.getMessages` and `channels.getMessages`, both open to bots): from an
anchor number backwards, keeping only the messages of the chat (and topic)
asked about, skipping the numbers that are empty, until the tail is full or the
request budget is spent. A read that fails says so and how far it got; it never
holds a turn up past its deadline.

Both doors produce one message shape (`Message`), so whatever reads a message
does not care which door it came through. Nothing here imports the CLI and
nothing here keeps message text: Telegram is the only home of the conversation.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx

API_BASE = "https://api.telegram.org"
DEEPGRAM_BASE = "https://api.deepgram.com"
DEEPGRAM_MODEL = "nova-3"
TIMEOUT = 60.0
MAX_RETRIES = 3
POLL_SECONDS = 30               # one getUpdates long poll
MESSAGE_CHARS = 4096            # one sendMessage, at most
CAPTION_CHARS = 1024            # one caption, at most
UPLOAD_HTTP_MAX = 50 * 1024 * 1024   # the Bot API's own ceiling for a sent file
HISTORY_BATCH = 100             # message numbers asked in one read request
HISTORY_DEADLINE = 8.0          # seconds one turn's whole history read may take
REPLY_CHAIN_STEPS = 5           # how far up a reply chain a read climbs
GENERAL_TOPIC = 1               # a forum's General topic, which the wire never marks
SUPERGROUP_SHIFT = 10 ** 12     # Bot API chat id of a channel: -(SHIFT + channel id)


# ── errors ───────────────────────────────────────────────────────────────────


class TelegramError(Exception):
    """A refusal with its exit category (SHEBANG exit codes) and a stable code."""

    def __init__(self, exit_code: int, code: str, message: str, hint: str | None = None,
                 status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.exit_code, self.code, self.message = exit_code, code, message
        self.hint, self.status, self.retry_after = hint, status, retry_after


def _description_code(description: str) -> str:
    """A Bot API description as a stable token: `Bad Request: chat not found`
    -> `chat_not_found`."""
    text = description.split(":", 1)[-1] if ":" in description else description
    token = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return token[:60] or "telegram_error"


# ── chat ids ─────────────────────────────────────────────────────────────────


def chat_kind(chat_id: int) -> str:
    """`private`, `group` (a basic group) or `supergroup`, from a Bot API chat id."""
    if chat_id > 0:
        return "private"
    if chat_id < -SUPERGROUP_SHIFT:
        return "supergroup"
    return "group"


def channel_of(chat_id: int) -> int:
    """The MTProto channel id of a supergroup's Bot API chat id."""
    return -chat_id - SUPERGROUP_SHIFT


def parse_chat_id(raw) -> int:
    """A chat id as the Bot API writes it: a whole number, negative for groups."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise TelegramError(6, "bad_chat", f"chat {raw!r} is not a Telegram chat id",
                            "a chat id is a whole number: a person's id, -<id> for a "
                            "basic group, -100<id> for a supergroup; `telegrambot seen` "
                            "lists the chats the bot has seen") from None
    if value == 0:
        raise TelegramError(6, "bad_chat", "chat 0 is not a Telegram chat id")
    return value


def bot_id_of(token: str) -> int | None:
    """The bot's own id, the part of its token before the colon."""
    head = (token or "").split(":", 1)[0]
    return int(head) if head.isdigit() else None


# ── the one message shape ────────────────────────────────────────────────────


@dataclasses.dataclass
class Message:
    """One message, whichever door it came through. `topic_id` is the forum topic
    (GENERAL_TOPIC for General), None outside a forum."""

    chat_id: int
    message_id: int
    date: int | None = None
    sender_id: int | None = None
    sender_name: str | None = None
    sender_username: str | None = None
    is_bot: bool = False
    own: bool = False
    text: str = ""
    topic_id: int | None = None
    reply_to: int | None = None
    reply_to_own: bool = False           # the message it answers is the bot's own
    attachment: dict | None = None      # {kind, name?, mime?, size?, duration?, file_id?}
    mentions: list = dataclasses.field(default_factory=list)   # @usernames and user ids
    service: bool = False                # a join, a title change and the like
    chat_type: str | None = None
    chat_title: str | None = None
    chat_username: str | None = None
    is_forum: bool = False

    def as_dict(self) -> dict:
        return {k: v for k, v in dataclasses.asdict(self).items()
                if v not in (None, [], "") or k in ("text",)}


def _full_name(first, last) -> str | None:
    name = " ".join(part for part in (first, last) if part)
    return name or None


_ATTACHMENT_KEYS = ("voice", "audio", "video_note", "video", "animation", "document",
                    "sticker", "photo")


def _update_attachment(msg: dict) -> dict | None:
    for kind in _ATTACHMENT_KEYS:
        found = msg.get(kind)
        if not found:
            continue
        if kind == "photo":
            biggest = found[-1] if isinstance(found, list) else found
            return {"kind": "photo", "file_id": biggest.get("file_id"),
                    "size": biggest.get("file_size")}
        if kind == "sticker":
            return {"kind": "sticker", "name": found.get("emoji"),
                    "file_id": found.get("file_id")}
        return {"kind": kind, "name": found.get("file_name"),
                "mime": found.get("mime_type"), "size": found.get("file_size"),
                "duration": found.get("duration"), "file_id": found.get("file_id")}
    return None


def _update_mentions(msg: dict, text: str) -> list:
    found = []
    for entity in (msg.get("entities") or msg.get("caption_entities") or []):
        kind = entity.get("type")
        if kind == "mention":
            start, length = entity.get("offset", 0), entity.get("length", 0)
            found.append(text[start:start + length].lstrip("@").lower())
        elif kind == "text_mention" and (entity.get("user") or {}).get("id"):
            found.append(int(entity["user"]["id"]))
        elif kind == "bot_command":
            start, length = entity.get("offset", 0), entity.get("length", 0)
            command = text[start:start + length]
            if "@" in command:
                found.append(command.split("@", 1)[1].lower())
    return found


def from_update(msg: dict, own_id: int | None = None) -> Message:
    """A Bot API `Message` object in the one shape."""
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    text = msg.get("text") or msg.get("caption") or ""
    is_forum = bool(chat.get("is_forum"))
    topic = None
    if is_forum:
        topic = (msg.get("message_thread_id") if msg.get("is_topic_message")
                 else None) or GENERAL_TOPIC
    reply = msg.get("reply_to_message") or {}
    reply_to = reply.get("message_id")
    # In a forum, a message posted straight into a topic arrives as a "reply" to
    # the topic's root; that is where it is, not what it answers.
    if is_forum and reply_to is not None and reply_to == msg.get("message_thread_id"):
        reply_to = None
    service = not text and not _update_attachment(msg) and any(
        key in msg for key in ("new_chat_members", "left_chat_member", "new_chat_title",
                               "forum_topic_created", "pinned_message", "group_chat_created"))
    sender_id = sender.get("id")
    return Message(
        chat_id=int(chat.get("id")), message_id=int(msg.get("message_id")),
        date=msg.get("date"), sender_id=sender_id,
        sender_name=_full_name(sender.get("first_name"), sender.get("last_name"))
        or chat.get("title"),
        sender_username=sender.get("username"), is_bot=bool(sender.get("is_bot")),
        own=own_id is not None and sender_id == own_id, text=text, topic_id=topic,
        reply_to=reply_to,
        reply_to_own=reply_to is not None and own_id is not None
        and (reply.get("from") or {}).get("id") == own_id,
        attachment=_update_attachment(msg),
        mentions=_update_mentions(msg, text), service=service,
        chat_type=chat.get("type"), chat_title=chat.get("title")
        or _full_name(chat.get("first_name"), chat.get("last_name")),
        chat_username=chat.get("username"), is_forum=is_forum)


# ── the HTTP Bot API ─────────────────────────────────────────────────────────

# Methods whose request reaches a chat: a timeout after the request left may
# mean it landed, so it is reported as unknown and never repeated.
_DELIVERING = {"sendMessage", "sendDocument", "sendPhoto", "sendVoice", "sendAudio",
               "sendVideo", "copyMessage", "forwardMessage"}


class BotAPI:
    """One bot's HTTP Bot API. Honours `retry_after` on 429 and backs off on 5xx;
    a delivering method is never repeated once it may have been delivered."""

    def __init__(self, token: str, *, base: str | None = None, timeout: float = TIMEOUT,
                 client: httpx.Client | None = None, sleep=time.sleep):
        self.token = token
        self.base = (base or os.environ.get("TELEGRAMBOT_API_BASE") or API_BASE).rstrip("/")
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0))
        self.sleep = sleep
        self.lock = threading.Lock()

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _url(self, method: str) -> str:
        return f"{self.base}/bot{self.token}/{method}"

    def call(self, method: str, params: dict | None = None, *, files: dict | None = None,
             timeout: float | None = None) -> Any:
        delivering = method in _DELIVERING
        attempt = 0
        while True:
            attempt += 1
            try:
                if files:
                    data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                            for k, v in (params or {}).items() if v is not None}
                    resp = self.client.post(self._url(method), data=data, files=files,
                                            timeout=timeout)
                else:
                    body = {k: v for k, v in (params or {}).items() if v is not None}
                    resp = self.client.post(self._url(method), json=body, timeout=timeout)
            except httpx.ConnectError as exc:
                # Nothing left the machine: always safe to try again.
                if attempt < MAX_RETRIES:
                    self.sleep(attempt * 1.5)
                    continue
                raise TelegramError(5, "network_error", f"{method}: cannot reach Telegram: "
                                    f"{exc}", "check connectivity") from None
            except httpx.TimeoutException:
                if delivering:
                    raise TelegramError(5, "delivery_unknown",
                                        f"{method} timed out after it was sent; delivery could "
                                        "not be confirmed", "do not retry automatically: read "
                                        "the chat first") from None
                if attempt < MAX_RETRIES:
                    self.sleep(attempt * 1.5)
                    continue
                raise TelegramError(5, "timeout", f"{method} timed out") from None
            except httpx.RequestError as exc:
                if delivering:
                    raise TelegramError(5, "delivery_unknown", f"{method}: {exc}",
                                        "do not retry automatically: read the chat "
                                        "first") from None
                raise TelegramError(5, "network_error", f"{method}: {exc}") from None
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            if resp.status_code == 200 and payload.get("ok"):
                return payload.get("result")
            description = str(payload.get("description") or resp.text[:200] or resp.status_code)
            retry_after = (payload.get("parameters") or {}).get("retry_after")
            if resp.status_code == 429:
                wait = float(retry_after or attempt * 1.5)
                if attempt < MAX_RETRIES and wait <= 60:
                    self.sleep(wait)
                    continue
                raise TelegramError(5, "rate_limited", f"{method}: {description}",
                                    f"Telegram asks to wait {wait:g}s", status=429,
                                    retry_after=wait)
            if resp.status_code >= 500:
                if attempt < MAX_RETRIES and not delivering:
                    self.sleep(attempt * 1.5)
                    continue
                code = "delivery_unknown" if delivering else "server_error"
                raise TelegramError(5, code, f"{method}: Telegram returned "
                                    f"{resp.status_code}: {description}",
                                    status=resp.status_code)
            if resp.status_code in (401, 404) and method == "getMe":
                raise TelegramError(2, "token_rejected", "Telegram rejects the bot token",
                                    "make a new token with BotFather (/token) and give it to "
                                    "the connection", status=resp.status_code)
            if resp.status_code == 401:
                raise TelegramError(2, "token_rejected", f"{method}: {description}",
                                    status=401)
            if resp.status_code == 409:
                raise TelegramError(5, "poll_conflict", f"{method}: {description}",
                                    "another process polls this bot, or a webhook is set; "
                                    "one token is polled by one process", status=409)
            if resp.status_code == 403:
                raise TelegramError(4, _description_code(description),
                                    f"{method}: {description}", status=403)
            if resp.status_code == 404 or "not found" in description.lower():
                raise TelegramError(3, _description_code(description),
                                    f"{method}: {description}", status=resp.status_code)
            raise TelegramError(6, _description_code(description), f"{method}: {description}",
                                status=resp.status_code)

    # -- the methods this capability uses -----------------------------------

    def get_me(self) -> dict:
        return self.call("getMe")

    def webhook_info(self) -> dict:
        return self.call("getWebhookInfo")

    def updates(self, offset: int | None, timeout: int = POLL_SECONDS) -> list[dict]:
        return self.call("getUpdates", {
            "offset": offset, "timeout": timeout,
            "allowed_updates": ["message", "my_chat_member", "chat_member"]},
            timeout=timeout + 15) or []

    def get_chat(self, chat_id: int) -> dict:
        return self.call("getChat", {"chat_id": chat_id})

    def member(self, chat_id: int, user_id: int) -> dict:
        return self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    def typing(self, chat_id: int, topic_id: int | None = None,
               action: str = "typing") -> None:
        self.call("sendChatAction", {"chat_id": chat_id, "action": action,
                                     "message_thread_id": _thread(topic_id)})

    def send_text(self, chat_id: int, text: str, *, topic_id: int | None = None,
                  reply_to: int | None = None) -> list[int]:
        """Send `text`, cut into parts of at most MESSAGE_CHARS; the ids sent."""
        sent = []
        for index, part in enumerate(split_text(text, MESSAGE_CHARS)):
            params = {"chat_id": chat_id, "text": part,
                      "message_thread_id": _thread(topic_id),
                      "link_preview_options": {"is_disabled": True}}
            if reply_to and index == 0:
                params["reply_parameters"] = {"message_id": reply_to,
                                              "allow_sending_without_reply": True}
            found = self.call("sendMessage", params)
            sent.append(int(found["message_id"]))
        return sent

    def send_file(self, chat_id: int, path: Path, *, caption: str | None = None,
                  topic_id: int | None = None, reply_to: int | None = None) -> int:
        size = path.stat().st_size
        if size > UPLOAD_HTTP_MAX:
            raise TelegramError(6, "file_too_large", f"{path} is {size} bytes; the Bot API "
                                f"sends at most {UPLOAD_HTTP_MAX} bytes",
                                "send a link to it instead")
        params = {"chat_id": chat_id, "caption": (caption or "")[:CAPTION_CHARS] or None,
                  "message_thread_id": _thread(topic_id)}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to,
                                          "allow_sending_without_reply": True}
        with path.open("rb") as handle:
            found = self.call("sendDocument", params,
                              files={"document": (path.name, handle)}, timeout=300)
        return int(found["message_id"])

    def react(self, chat_id: int, message_id: int, emoji: str | None) -> None:
        reaction = [{"type": "emoji", "emoji": emoji}] if emoji else []
        self.call("setMessageReaction", {"chat_id": chat_id, "message_id": message_id,
                                         "reaction": reaction})

    def set_commands(self, commands: list[dict]) -> None:
        self.call("setMyCommands", {"commands": commands})

    def download(self, file_id: str, dest: Path) -> Path:
        """Fetch a file the Bot API can serve (at most 20 MB) to `dest`."""
        found = self.call("getFile", {"file_id": file_id})
        path = found.get("file_path")
        if not path:
            raise TelegramError(6, "file_unavailable", "Telegram serves no path for this file",
                                "the Bot API serves files of at most 20 MB")
        url = f"{self.base}/file/bot{self.token}/{path}"
        try:
            with self.client.stream("GET", url, timeout=120) as resp:
                if resp.status_code != 200:
                    raise TelegramError(5, "download_failed",
                                        f"file download answered {resp.status_code}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open("wb") as out:
                    for chunk in resp.iter_bytes():
                        out.write(chunk)
        except httpx.RequestError as exc:
            raise TelegramError(5, "download_failed", f"file download: {exc}") from None
        return dest


def _thread(topic_id: int | None) -> int | None:
    """The message_thread_id a write carries: none for General, which takes none."""
    return topic_id if topic_id and topic_id != GENERAL_TOPIC else None


def split_text(text: str, limit: int = MESSAGE_CHARS) -> list[str]:
    """Cut a reply into messages of at most `limit` characters, at a paragraph,
    then a line, then a space where one is near the end."""
    text = (text or "").strip()
    parts = []
    while len(text) > limit:
        window = text[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


# ── Deepgram ─────────────────────────────────────────────────────────────────


def deepgram_check(key: str, client: httpx.Client | None = None) -> dict:
    """The cheapest call the key authenticates: the projects it can see."""
    own = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        resp = client.get(f"{DEEPGRAM_BASE}/v1/projects",
                          headers={"Authorization": f"Token {key}"})
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"Deepgram unreachable: {exc}"}
    finally:
        if own:
            client.close()
    if resp.status_code == 200:
        return {"ok": True}
    if resp.status_code in (401, 403):
        return {"ok": False, "error": "Deepgram rejects the key"}
    return {"ok": False, "error": f"Deepgram answered {resp.status_code}"}


def transcribe(audio: Path, key: str, *, mime: str | None = None,
               timeout: float = 120.0, client: httpx.Client | None = None) -> dict:
    """{text, language?, confidence?} for an audio file, or raise TelegramError."""
    own = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        resp = client.post(f"{DEEPGRAM_BASE}/v1/listen",
                           params={"model": DEEPGRAM_MODEL, "smart_format": "true",
                                   "detect_language": "true"},
                           headers={"Authorization": f"Token {key}",
                                    "Content-Type": mime or "audio/ogg"},
                           content=audio.read_bytes())
    except httpx.RequestError as exc:
        raise TelegramError(5, "transcription_failed", f"Deepgram: {exc}") from None
    finally:
        if own:
            client.close()
    if resp.status_code != 200:
        raise TelegramError(2 if resp.status_code in (401, 403) else 5,
                            "transcription_failed",
                            f"Deepgram answered {resp.status_code}: {resp.text[:200]}")
    channel = (((resp.json().get("results") or {}).get("channels") or [{}])[0])
    best = (channel.get("alternatives") or [{}])[0]
    return {"text": (best.get("transcript") or "").strip(),
            "language": channel.get("detected_language"),
            "confidence": best.get("confidence")}


# ── history, read back by number ─────────────────────────────────────────────


@dataclasses.dataclass
class Reading:
    """What one read found: the messages oldest first, and how it went."""

    messages: list
    wanted: int
    requests: int = 0
    reason: str | None = None           # why it stopped short of `wanted`

    @property
    def line(self) -> str:
        """The channel-state line a turn is told."""
        got = len(self.messages)
        if self.reason is None and got >= self.wanted:
            return f"history: read {got} of {self.wanted}"
        return f"history: read {got} of {self.wanted}, {self.reason or 'nothing older found'}"

    def as_dict(self) -> dict:
        return {"messages": [m.as_dict() for m in self.messages], "wanted": self.wanted,
                "read": len(self.messages), "requests": self.requests,
                "complete": self.reason is None and len(self.messages) >= self.wanted,
                "reason": self.reason}


def _mt_topic(message, is_forum: bool) -> int | None:
    """The forum topic of an MTProto message: the same question the Bot API answers
    with message_thread_id, from `reply_to`."""
    if not is_forum:
        return None
    if type(getattr(message, "action", None)).__name__ == "MessageActionTopicCreate":
        return int(message.id)   # a topic's root is the message that opened it
    reply = getattr(message, "reply_to", None)
    if not getattr(reply, "forum_topic", False):
        return GENERAL_TOPIC
    for value in (getattr(reply, "reply_to_top_id", None),
                  getattr(reply, "reply_to_msg_id", None)):
        if isinstance(value, int) and value > 0:
            return value
    return GENERAL_TOPIC


def _mt_attachment(message) -> dict | None:
    media = getattr(message, "media", None)
    if media is None:
        return None
    kind = type(media).__name__
    if kind == "MessageMediaPhoto":
        return {"kind": "photo"}
    if kind == "MessageMediaDocument":
        doc = getattr(media, "document", None)
        found = {"kind": "document", "mime": getattr(doc, "mime_type", None),
                 "size": getattr(doc, "size", None)}
        for attr in getattr(doc, "attributes", None) or []:
            name = type(attr).__name__
            if name == "DocumentAttributeFilename":
                found["name"] = attr.file_name
            elif name == "DocumentAttributeAudio":
                found["kind"] = "voice" if getattr(attr, "voice", False) else "audio"
                found["duration"] = getattr(attr, "duration", None)
            elif name == "DocumentAttributeVideo" and found["kind"] == "document":
                found["kind"] = "video_note" if getattr(attr, "round_message", False) else "video"
                found["duration"] = getattr(attr, "duration", None)
            elif name == "DocumentAttributeSticker":
                found["kind"] = "sticker"
                found["name"] = getattr(attr, "alt", None)
        return found
    if kind in ("MessageMediaEmpty", "MessageMediaWebPage"):
        return None
    return {"kind": kind.removeprefix("MessageMedia").lower()}


def _peer_matches(peer, chat_id: int) -> bool:
    kind = type(peer).__name__
    if kind == "PeerUser":
        return chat_id > 0 and peer.user_id == chat_id
    if kind == "PeerChat":
        return chat_kind(chat_id) == "group" and peer.chat_id == -chat_id
    if kind == "PeerChannel":
        return chat_kind(chat_id) == "supergroup" and peer.channel_id == channel_of(chat_id)
    return False


def from_mtproto(message, chat_id: int, users: dict, own_id: int | None,
                 is_forum: bool = False) -> Message | None:
    """An MTProto message in the one shape; None for an empty number."""
    kind = type(message).__name__
    if kind == "MessageEmpty":
        return None
    sender = getattr(message, "from_id", None)
    sender_id = getattr(sender, "user_id", None)
    if sender_id is None and chat_id > 0:
        # A private chat leaves from_id out: the sender is the bot or the person.
        sender_id = own_id if getattr(message, "out", False) else chat_id
    user = users.get(sender_id)
    reply = getattr(message, "reply_to", None)
    topic = _mt_topic(message, is_forum)
    reply_to = getattr(reply, "reply_to_msg_id", None)
    if is_forum and getattr(reply, "forum_topic", False) and \
            getattr(reply, "reply_to_top_id", None) is None:
        reply_to = None   # a post into the topic, not an answer to its root
    date = getattr(message, "date", None)
    entities = getattr(message, "entities", None) or []
    text = getattr(message, "message", None) or ""
    mentions = []
    for entity in entities:
        name = type(entity).__name__
        if name == "MessageEntityMention":
            mentions.append(text[entity.offset + 1:entity.offset + entity.length].lower())
        elif name == "MessageEntityMentionName":
            mentions.append(int(entity.user_id))
    return Message(
        chat_id=chat_id, message_id=int(message.id),
        date=int(date.timestamp()) if date else None, sender_id=sender_id,
        sender_name=_full_name(getattr(user, "first_name", None),
                               getattr(user, "last_name", None)),
        sender_username=getattr(user, "username", None),
        is_bot=bool(getattr(user, "bot", False)),
        own=own_id is not None and sender_id == own_id, text=text, topic_id=topic,
        reply_to=reply_to, attachment=_mt_attachment(message), mentions=mentions,
        service=kind == "MessageService", is_forum=is_forum)


def session_path(state_dir: Path, bot_id: int) -> Path:
    """Where a bot's MTProto session lives: its auth key, no message text."""
    return Path(state_dir) / str(bot_id) / "mtproto.session"


class HistoryReader:
    """One bot's MTProto session, used only to read messages by number.

    The session is a Telethon StringSession kept in one file (mode 0600), so the
    service and a CLI call can use it at once without sharing a database file.
    Telethon's own flood waiting is off: a read that Telegram asks to wait
    stops, reports it, and the turn goes on without it."""

    def __init__(self, token: str, api_id: int, api_hash: str, state_dir: Path):
        self.token = token
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.bot_id = bot_id_of(token)
        self.path = session_path(state_dir, self.bot_id or 0)
        self.client = None

    async def open(self):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        saved = None
        try:
            saved = self.path.read_text().strip() or None
        except OSError:
            pass
        client = TelegramClient(StringSession(saved), self.api_id, self.api_hash,
                                receive_updates=False, flood_sleep_threshold=0,
                                request_retries=1, connection_retries=2, timeout=10,
                                device_model="telegrambot", app_version="telegrambot")
        await client.connect()
        try:
            if not await client.is_user_authorized():
                await client.sign_in(bot_token=self.token)
            me = await client.get_me()
        except Exception:
            await client.disconnect()
            raise
        if me is None or (self.bot_id and me.id != self.bot_id):
            await client.disconnect()
            # A session another token signed in is not this bot's: start afresh.
            if saved:
                self.path.unlink(missing_ok=True)
                return await self.open()
            raise TelegramError(2, "session_mismatch", "the MTProto session is not this bot's")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = client.session.save()
        if fresh != saved:
            spare = self.path.with_name(f".{self.path.name}.{os.getpid()}")
            spare.write_text(fresh)
            os.chmod(spare, 0o600)
            os.replace(spare, self.path)
        self.client = client
        return self

    async def close(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    async def fetch(self, chat_id: int, ids: list[int]) -> tuple[list, dict]:
        """The raw messages for `ids` in this chat and the users they name."""
        from telethon.tl import functions, types
        wanted = [types.InputMessageID(i) for i in ids if i > 0]
        if not wanted:
            return [], {}
        if chat_kind(chat_id) == "supergroup":
            request = functions.channels.GetMessagesRequest(
                channel=types.InputChannel(channel_of(chat_id), 0), id=wanted)
        else:
            request = functions.messages.GetMessagesRequest(id=wanted)
        result = await self.client(request)
        users = {u.id: u for u in getattr(result, "users", None) or []}
        return list(getattr(result, "messages", None) or []), users

    async def read_back(self, chat_id: int, anchor: int, *, limit: int, topic_id=None,
                        is_forum: bool = False, max_requests: int = 5,
                        include_anchor: bool = False) -> Reading:
        """Up to `limit` messages of this chat (and topic) numbered below `anchor`
        (or up to it, with `include_anchor`), oldest first."""
        found: dict[int, Message] = {}
        reading = Reading([], limit)
        top = anchor if include_anchor else anchor - 1
        while top >= 1 and len(found) < limit:
            if reading.requests >= max_requests:
                reading.reason = f"the read budget of {max_requests} requests is spent"
                break
            ids = list(range(top, max(0, top - HISTORY_BATCH), -1))
            reading.requests += 1
            raw, users = await self.fetch(chat_id, ids)
            for message in raw:
                if type(message).__name__ == "MessageEmpty":
                    continue
                if not _peer_matches(getattr(message, "peer_id", None), chat_id):
                    continue   # another chat in the bot's shared numbering
                shaped = from_mtproto(message, chat_id, users, self.bot_id, is_forum)
                if shaped is None:
                    continue
                if topic_id is not None and shaped.topic_id != topic_id and \
                        shaped.message_id != topic_id:
                    continue
                found[shaped.message_id] = shaped
            top = ids[-1] - 1
        reading.messages = sorted(found.values(), key=lambda m: m.message_id)[-limit:]
        return reading

    async def one(self, chat_id: int, message_id: int, is_forum: bool = False):
        raw, users = await self.fetch(chat_id, [message_id])
        for message in raw:
            if _peer_matches(getattr(message, "peer_id", None), chat_id):
                return message, from_mtproto(message, chat_id, users, self.bot_id, is_forum)
        return None, None

    async def download(self, chat_id: int, message_id: int, dest: Path) -> Path:
        raw, _shaped = await self.one(chat_id, message_id)
        if raw is None or getattr(raw, "media", None) is None:
            raise TelegramError(3, "no_attachment",
                                f"message {message_id} in chat {chat_id} carries no file the "
                                "bot can read")
        dest.parent.mkdir(parents=True, exist_ok=True)
        path = await self.client.download_media(raw, file=str(dest))
        return Path(path)


def mtproto_failure(exc: Exception) -> TelegramError:
    """An MTProto exception as a refusal: a flood wait, a refused method, the network."""
    if isinstance(exc, TelegramError):
        return exc
    name = type(exc).__name__
    seconds = getattr(exc, "seconds", None)
    if seconds is not None or "Flood" in name:
        return TelegramError(5, "flood_wait", f"Telegram asks to wait {seconds}s before the "
                             "next read", retry_after=seconds)
    if name in ("ApiIdInvalidError", "ApiIdPublishedFloodError"):
        return TelegramError(2, "api_id_invalid", "Telegram rejects the connection's api_id "
                             "and api_hash", "copy both from my.telegram.org/apps")
    if name in ("AccessTokenInvalidError", "AccessTokenExpiredError", "AuthKeyUnregisteredError"):
        return TelegramError(2, "token_rejected", "Telegram rejects the bot token for MTProto")
    if name in ("ChannelPrivateError", "ChannelInvalidError", "ChatIdInvalidError",
                "PeerIdInvalidError"):
        return TelegramError(3, "chat_unreachable", f"the bot cannot read this chat ({name})")
    if isinstance(exc, (OSError, ConnectionError, asyncio.TimeoutError)):
        return TelegramError(5, "mtproto_unreachable", f"MTProto: {name}: {exc}")
    return TelegramError(5, "mtproto_failed", f"MTProto: {name}: {exc}")


def run_read(coro_factory, deadline: float = HISTORY_DEADLINE):
    """Run one read to completion in a fresh event loop, within `deadline`
    seconds; for a caller with no loop of its own (the CLI)."""
    async def bounded():
        return await asyncio.wait_for(coro_factory(), timeout=deadline)
    try:
        return asyncio.run(bounded())
    except asyncio.TimeoutError:
        raise TelegramError(5, "history_timeout",
                            f"the read did not finish within {deadline:g}s") from None


async def read_history(token: str, api_id: int, api_hash: str, state_dir: Path,
                       chat_id: int, anchor: int, *, limit: int, topic_id=None,
                       is_forum: bool = False, max_requests: int = 5,
                       include_anchor: bool = False, reply_chain_of: int | None = None
                       ) -> Reading:
    """One whole read: sign in (or reuse the session), read back, climb the
    reply chain of `reply_chain_of` past the tail within the same budget."""
    reader = HistoryReader(token, api_id, api_hash, state_dir)
    try:
        await reader.open()
        reading = await reader.read_back(chat_id, anchor, limit=limit, topic_id=topic_id,
                                         is_forum=is_forum, max_requests=max_requests,
                                         include_anchor=include_anchor)
        if reply_chain_of:
            seen = {m.message_id for m in reading.messages}
            chain = []
            target, steps = reply_chain_of, 0
            while target and target not in seen and steps < REPLY_CHAIN_STEPS and \
                    reading.requests < max_requests:
                steps += 1
                reading.requests += 1
                _raw, shaped = await reader.one(chat_id, target, is_forum)
                if shaped is None:
                    break
                chain.append(shaped)
                seen.add(shaped.message_id)
                target = shaped.reply_to
            if chain:
                reading.messages = sorted(chain + reading.messages, key=lambda m: m.message_id)
        return reading
    except TelegramError:
        raise
    except Exception as exc:
        raise mtproto_failure(exc) from None
    finally:
        await reader.close()
