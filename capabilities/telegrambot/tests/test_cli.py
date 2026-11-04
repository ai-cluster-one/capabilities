"""The CLI against a fake Bot API: a connection that holds everything its bot
needs, the resolution report, doctor, and the gates in front of history and the
writes. No network."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from _cli import load_cli

TOKEN = "123456:fake-token-for-tests"


class FakeBotAPI:
    """Just enough of the Bot API (and Deepgram's key check) to drive the verbs."""

    def __init__(self):
        self.requests: list[tuple[str, dict | None]] = []
        self.privacy = True
        self.webhook = ""
        self.status = "member"
        self.forum = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.deepgram.com":
            good = request.headers.get("authorization") == "Token dg-key"
            return httpx.Response(200 if good else 401, json={})
        path = request.url.path
        if not path.startswith(f"/bot{TOKEN}/"):
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        method = path.rsplit("/", 1)[1]
        body = json.loads(request.content) if request.content and \
            request.headers.get("content-type", "").startswith("application/json") else None
        self.requests.append((method, body))
        if method == "getMe":
            return self.ok({"id": 123456, "is_bot": True, "first_name": "Helper",
                            "username": "helper_bot", "can_join_groups": True,
                            "can_read_all_group_messages": not self.privacy})
        if method == "getWebhookInfo":
            return self.ok({"url": self.webhook, "pending_update_count": 0})
        if method == "getChat":
            return self.ok({"id": body["chat_id"], "type": "supergroup", "title": "G",
                            "is_forum": self.forum})
        if method == "getChatMember":
            return self.ok({"status": self.status})
        if method == "sendMessage":
            return self.ok({"message_id": 500 + len(self.requests)})
        return httpx.Response(404, json={"ok": False, "description": "Not Found"})

    @staticmethod
    def ok(result):
        return httpx.Response(200, json={"ok": True, "result": result})


@pytest.fixture()
def telegram(monkeypatch):
    fake = FakeBotAPI()
    real = httpx.Client

    class Client(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", Client)
    return fake


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    caps = root / "capabilities"
    caps.mkdir()
    (caps / "settings.json").write_text(json.dumps({"capabilities": {"telegrambot": {"enabled": True}}}))
    (caps / "project.json").write_text(json.dumps({"slug": "project"}))
    home = tmp_path / "home"
    for key, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
                     ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(key, str(home / sub))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    for key in list(os.environ):
        if key.startswith(("AGENTKIT_DB_", "TELEGRAMBOT_", "DEEPGRAM_", "CAPABILITIES_PROJECT_",
                           "CAPABILITIES_READ_ONLY", "CAPABILITIES_AUTH_CONTEXT",
                           "AGENT_ENCRYPTION_KEY")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CAPABILITIES_PROJECT_ID", "prj_test")
    monkeypatch.setenv("CAPABILITIES_PROJECT_ID_ROOT", str(root))
    monkeypatch.chdir(root)
    return root


@pytest.fixture()
def cli(project):
    module = load_cli()
    module._RECORDS = None
    return module


def run(cli, capsys, *argv, stdin: str | None = None) -> tuple[int, dict | None, dict | None]:
    sys_argv, sys_stdin = sys.argv, sys.stdin
    sys.argv = ["telegrambot", *argv]
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    cli._RECORDS = None
    code = 0
    try:
        cli.main()
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else 1
    finally:
        sys.argv, sys.stdin = sys_argv, sys_stdin
    out, err = capsys.readouterr()
    parse = lambda text: json.loads(text) if text.strip().startswith(("{", "[")) else None  # noqa: E731
    return code, parse(out), parse(err.strip().splitlines()[-1]) if err.strip() else None


def add(cli, capsys, cid="helper", **secrets):
    return run(cli, capsys, "connection", "add", cid, "--file", "--stdin",
               stdin=json.dumps(secrets))


def entry(project: Path, cid="helper") -> dict:
    return json.loads((project / "capabilities" / "telegrambot" / "connections.json")
                      .read_text())["connections"][cid]


def test_one_connection_holds_every_value_its_bot_needs(cli, project, capsys):
    code, out, err = add(cli, capsys, secret_env=TOKEN, api_id_env="1234",
                         api_hash_env="hash-hash-hash", deepgram_env="dg-key")
    assert code == 0, err
    assert entry(project) == {"secret_env": "TELEGRAMBOT_HELPER_BOT_TOKEN",
                              "api_id_env": "TELEGRAMBOT_HELPER_API_ID",
                              "api_hash_env": "TELEGRAMBOT_HELPER_API_HASH",
                              "deepgram_env": "TELEGRAMBOT_HELPER_DEEPGRAM_KEY"}
    env = (project / ".env.local").read_text()
    for key in ("BOT_TOKEN", "API_ID", "API_HASH", "DEEPGRAM_KEY"):
        assert f"TELEGRAMBOT_HELPER_{key}=" in env
    assert TOKEN not in json.dumps(out)
    code, out, _ = run(cli, capsys, "connections")
    row = out["connections"]["helper"]
    assert [k["field"] for k in row["keys"]] == ["secret_env", "api_id_env", "api_hash_env",
                                                 "deepgram_env"]
    assert all(k["set"] for k in row["keys"]) and row["history"] == "ready"
    assert row["voice"] == "ready" and TOKEN not in json.dumps(out)


def test_a_connection_may_refer_to_keys_held_elsewhere(cli, project, capsys, monkeypatch):
    monkeypatch.setenv("SHARED_TOKEN", TOKEN)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    code, _out, err = run(cli, capsys, "connection", "add", "helper", "--file",
                          "--env", "secret_env=SHARED_TOKEN",
                          "--env", "deepgram_env=DEEPGRAM_API_KEY")
    assert code == 0, err
    assert entry(project) == {"secret_env": "SHARED_TOKEN", "deepgram_env": "DEEPGRAM_API_KEY"}
    code, out, _ = run(cli, capsys, "connections")
    row = out["connections"]["helper"]
    assert row["voice"] == "ready" and row["history"].startswith("off")
    unset = [k["field"] for k in row["keys"] if not k["set"]]
    assert unset == ["api_id_env", "api_hash_env"]


def test_the_machine_deepgram_key_is_a_named_reference_never_a_fallback(cli, project, capsys,
                                                                        monkeypatch):
    # The prefill names the shared key in the connection where the machine has one...
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    assert cli._connection_prefill("helper", {}) == {"fields": {"deepgram_env": "DEEPGRAM_API_KEY"}}
    assert cli._connection_prefill("helper", {"deepgram_env": "OWN"}) == {}
    # ...and a connection that names none has none, whatever the machine holds.
    add(cli, capsys, secret_env=TOKEN)
    cfg, problem = cli._build_cfg("helper", entry(project))
    assert problem is None and cfg["deepgram"] is None
    assert "no Deepgram key" in cfg["voice_off"]
    monkeypatch.delenv("DEEPGRAM_API_KEY")
    assert cli._connection_prefill("helper", {}) == {}


def test_doctor_names_what_a_connection_lacks(cli, project, capsys, telegram):
    add(cli, capsys, secret_env=TOKEN)
    code, out, err = run(cli, capsys, "doctor")
    assert code == 0, err
    row = out["connections"]["helper"]
    assert row["bot"] == {"id": 123456, "username": "helper_bot", "name": "Helper",
                          "can_join_groups": True}
    assert row["privacy"]["mode"] == "on" and row["webhook"] == {"set": False}
    assert row["history"]["ready"] is False and "no api_id and no api_hash" in \
        row["history"]["reason"]
    assert row["voice"]["ready"] is False and row["missing"] == ["history", "voice"]
    assert row["bot_to_bot"].startswith("unknown")
    # The identity Telegram answered is recorded on the connection.
    assert entry(project)["identity"] == {"id": "123456", "display_name": "@helper_bot"}


def test_doctor_with_history_voice_and_a_webhook(cli, project, capsys, telegram, monkeypatch):
    add(cli, capsys, secret_env=TOKEN, api_id_env="1234", api_hash_env="h" * 32,
        deepgram_env="dg-key")
    monkeypatch.setattr(cli, "_history_check", lambda cfg: {"ready": True, "session": "x"})
    telegram.privacy, telegram.webhook = False, "https://hooks.example.com/bot"
    code, out, err = run(cli, capsys, "doctor")
    assert code == 0, err
    row = out["connections"]["helper"]
    assert row["voice"] == {"ready": True} and row["history"]["ready"] is True
    assert row["privacy"]["mode"] == "off"
    assert row["webhook"]["set"] is True and row["webhook"]["host"] == "hooks.example.com"
    assert row["missing"] == ["polling: a webhook is set, so the service cannot poll this bot"]


def test_doctor_refuses_without_a_token_and_on_a_rejected_one(cli, project, capsys, telegram):
    (project / "capabilities" / "telegrambot").mkdir(parents=True)
    (project / "capabilities" / "telegrambot" / "connections.json").write_text(json.dumps(
        {"connections": {"helper": {"secret_env": "TELEGRAMBOT_HELPER_BOT_TOKEN"}}}))
    code, _out, err = run(cli, capsys, "doctor")
    assert code == 2 and err["error"]["code"] == "credentials_missing"
    assert err["error"]["message"] == (
        "connection 'helper' lacks a bot token (TELEGRAMBOT_HELPER_BOT_TOKEN resolves "
        "nowhere), an api_id, an api_hash, a Deepgram key; without them history and voice "
        "would be off")
    (project / ".env.local").write_text("TELEGRAMBOT_HELPER_BOT_TOKEN=999:wrong\n")
    code, out, _ = run(cli, capsys, "doctor")
    assert code == 2 and "token_rejected" in out["connections"]["helper"]["error"]


def test_history_needs_the_app_and_a_group_it_fully_sees(cli, project, capsys, telegram):
    add(cli, capsys, secret_env=TOKEN)
    code, _out, err = run(cli, capsys, "history", "42", "--before", "10")
    assert code == 7 and err["error"]["code"] == "history_unavailable"
    add(cli, capsys, "other", secret_env=TOKEN, api_id_env="1", api_hash_env="h" * 32)
    code, _out, err = run(cli, capsys, "--connection", "other", "history", "-1001234",
                          "--before", "10")
    assert code == 4 and err["error"]["code"] == "group_not_visible"
    code, _out, err = run(cli, capsys, "--connection", "other", "history", "@ann",
                          "--before", "10")
    assert code == 6 and err["error"]["code"] == "bad_chat"


def test_writes_need_a_grant(cli, project, capsys, telegram):
    add(cli, capsys, secret_env=TOKEN)
    code, _out, err = run(cli, capsys, "send", "42", "hello")
    assert code == 4 and err["error"]["code"] == "read_only"
    assert not [m for m, _b in telegram.requests if m == "sendMessage"]
    path = project / "capabilities" / "telegrambot" / "connections.json"
    data = json.loads(path.read_text())
    data["connections"]["helper"]["allow_write"] = True
    path.write_text(json.dumps(data))
    code, out, err = run(cli, capsys, "send", "42", "-", "--reply-to", "7", stdin="hi there")
    assert code == 0, err
    assert out["message_ids"] and out["chat"] == 42
    sent = [b for m, b in telegram.requests if m == "sendMessage"][0]
    assert sent["text"] == "hi there" and sent["reply_parameters"]["message_id"] == 7


def test_arguments_are_checked_before_anything_runs(cli, project, capsys):
    code, _out, err = run(cli, capsys, "history", "42", "--limit", "500")
    assert code == 6 and err["error"]["code"] == "invalid_arguments"
    code, _out, err = run(cli, capsys, "history", "42", "--lim", "5")
    assert code == 6
    code, _out, err = run(cli, capsys, "prompts", "show", "Context.X")
    assert code == 6 and err["error"]["code"] == "bad_prompt_key"


def test_help_and_manifest_carry_the_surface(cli, project, capsys):
    code, _out, _err = run(cli, capsys, "help")
    assert code == 0
    code, out, _ = run(cli, capsys, "manifest", "--json")
    fields = {f["key"]: f for f in out["connections"]["fields"]}
    assert set(fields) == {"secret_env", "api_id_env", "api_hash_env", "deepgram_env"}
    assert all(f["secret"] and f["env"].startswith("TELEGRAMBOT_{ID}_") for f in fields.values())
    assert fields["secret_env"]["required"] and out["tables"] is True
