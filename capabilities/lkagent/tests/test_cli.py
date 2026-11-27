"""The CLI's declarations and reads, in-process: the LiveKit-only connection, the OpenAI key
through the cascade, the dispatch rules that route to the line (with a fake LiveKit API),
the doctor's warning, inventory's deferral, and the number an outgoing call is placed from.
Nothing here reaches a network or the real home."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

CAPABILITY = Path(__file__).resolve().parents[1]
CLI = CAPABILITY / "bin" / "lkagent"
AGENT = "lkagent-c1"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("AGENTKIT_DB_", "CAPABILITIES_", "OPENAI_")):
            monkeypatch.delenv(key)
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_loader("lkagent_cli", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(CLI)
    source = CLI.read_text().replace('if __name__ == "__main__":\n    main()\n', "")
    exec(compile(source, str(CLI), "exec"), module.__dict__)
    return module


def refusal(capsys) -> dict:
    line = next(l for l in reversed(capsys.readouterr().err.splitlines())
                if l.startswith('{"error"'))
    return json.loads(line)["error"]


# ── what it declares ────────────────────────────────────────────────────────

def test_the_connection_is_livekit_only_and_the_openai_key_is_the_machines(cli):
    assert [f["key"] for f in cli.CONN_FIELDS] == ["url", "api_key_env", "secret_env"]
    assert [f["env"] for f in cli.CONN_FIELDS if f.get("secret")] == [
        "LKAGENT_{ID}_API_KEY", "LKAGENT_{ID}_API_SECRET"]
    assert cli.SETUP_FIELDS == [{
        "key": "OPENAI_API_KEY", "label": "OpenAI API key", "type": "string", "secret": True,
        "required": True, "shared": "OPENAI_API_KEY",
        "help": "The OpenAI key GPT Live, the line's voice, runs on."}]
    assert [k["key"] for k in cli.CRED_KEYS] == ["OPENAI_API_KEY"]
    keys = {k["key"]: k for k in cli.SETTINGS["keys"]}
    assert keys["agent_name"]["default"] == "" and keys["outbound_trunk"]["default"] == ""
    assert keys["recording"]["type"] == "boolean" and keys["recording"]["default"] is False
    assert keys["numbers"]["type"] == "object" and keys["numbers"]["default"] == {}
    for gone in ("openai_key_env", "max_calls", "allowed_capabilities", "read_only",
                 "sip_trunk_id"):
        assert gone not in keys


def test_its_name_title_and_icon(cli):
    # Its icon is LiveKit's brand mark, so it declares no Lucide glyph (SHEBANG.md, lucide_icon).
    assert (cli.NAME, cli.TITLE) == ("lkagent", "LiveKit Agent")
    assert not hasattr(cli, "LUCIDE_ICON")
    assert {lang: t["title"] for lang, t in cli.TRANSLATIONS.items()} == {
        "ru": "LiveKit Agent", "et": "LiveKit Agent"}
    assert all(t["description"] != cli.SUMMARY for t in cli.TRANSLATIONS.values())
    assert cli.INVENTORY == {"network": "optional"} and cli.TABLES is True
    assert (CAPABILITY / "icon.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert (cli._BUNDLE / "service" / "line.py").is_file()   # bin/ in a checkout, root installed


def test_the_agent_name_is_the_setting_else_lkagent_and_the_connection(cli):
    assert cli._agent_name("c1", {}) == "lkagent-c1"
    assert cli._agent_name("c1", {"agent_name": "  "}) == "lkagent-c1"
    assert cli._agent_name("c1", {"agent_name": "front-desk"}) == "front-desk"


def test_the_openai_key_resolves_through_the_cascade_vault_first(cli, tmp_path, monkeypatch):
    assert cli._openai_key()[0] is None
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-environment")
    assert cli._openai_key()[:2] == ("sk-from-the-environment", "env")
    monkeypatch.setattr(cli, "_vault_value",
                        lambda key: ("sk-from-the-vault", "vault:prj_x")
                        if key == "OPENAI_API_KEY" else (None, None))
    assert cli._openai_key() == ("sk-from-the-vault", "vault", "vault:prj_x")


def test_a_settings_document_with_a_bad_number_is_refused(cli):
    assert cli._settings_check("project", {"numbers": [{"number": "+15550100"}]}) == []
    assert cli._settings_check("project", {"numbers": [{"number": "front"}]})
    assert cli._settings_check("project", {"numbers": {"+15550100": {"read_only": True}}}) == []
    assert cli._settings_check("project", {"numbers": {"front": {}}})
    assert cli._settings_check("project", {"numbers": {"+15550100": {"number": "+1"}}})


def test_the_service_is_one_the_node_supervisor_runs(cli):
    deploy = cli.SERVICE["deploy"]
    assert deploy["command"] == ["lkagent", "service", "run"]
    assert deploy["doctor"] == ["lkagent", "service", "doctor"]
    assert {"run", "doctor", "status", "logs"} <= set(cli.SERVICE["verbs"])
    assert {"service status", "service logs"} <= set(cli.HELP_VERBS)


def _file_settings(cli, tmp_path, monkeypatch, document):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(document))
    monkeypatch.setattr(cli, "_settings_home_path", lambda home: path)
    monkeypatch.setattr(cli, "_settings_in_rows", lambda home: False)
    monkeypatch.setattr(cli, "_records", lambda: type("R", (), {
        "guard_write": staticmethod(lambda *a: None)})())
    return path


def test_numbers_kept_as_a_list_are_written_keyed_once_and_nothing_is_lost(
        cli, tmp_path, monkeypatch):
    listed = {"number": "+1 555 0142", "instructions": "Answer as the line.",
              "allowed_capabilities": ["tasks", "memory"], "read_only": False}
    path = _file_settings(cli, tmp_path, monkeypatch, {"numbers": [listed], "voice": "ash"})
    assert cli._key_numbers() == ["project"]
    written = json.loads(path.read_text())
    assert written == {"voice": "ash", "numbers": {"+15550142": {
        "instructions": "Answer as the line.", "allowed_capabilities": ["tasks", "memory"],
        "read_only": False}}}
    assert cli._key_numbers() == []                      # once: keyed stays as it is
    assert cli._line_module().number_entry(written["numbers"], "+15550142")["read_only"] is False


def test_a_list_the_check_refuses_is_not_rewritten(cli, tmp_path, monkeypatch):
    kept = {"numbers": [{"number": "front desk"}]}
    path = _file_settings(cli, tmp_path, monkeypatch, kept)
    assert cli._key_numbers() == [] and json.loads(path.read_text()) == kept


def test_the_voice_is_given_the_openai_base_url_the_cascade_holds(cli, monkeypatch, tmp_path):
    values = {"LKAGENT_C1_API_KEY": "APIkey", "LKAGENT_C1_API_SECRET": "secret-value",
              "OPENAI_API_KEY": "sk-test-placeholder"}
    entry = {"url": "wss://lk.example", "api_key_env": "LKAGENT_C1_API_KEY",
             "secret_env": "LKAGENT_C1_API_SECRET"}
    monkeypatch.setattr(cli, "_connection", lambda wanted: ("c1", dict(entry)))
    monkeypatch.setattr(cli, "_resolve_env_key",
                        lambda key: (values.get(key), "env" if key in values else None, None))
    monkeypatch.setattr(cli, "_line_settings", lambda: {})
    monkeypatch.setattr(cli, "_project_root", lambda: None)
    env, _line = cli._service_plan(None)
    assert "OPENAI_BASE_URL" not in env
    values["OPENAI_BASE_URL"] = "https://eu.api.openai.com/v1"
    env, _line = cli._service_plan(None)
    assert env["OPENAI_BASE_URL"] == "https://eu.api.openai.com/v1"


def test_service_status_and_logs_read_the_state_directory(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_state_dir", lambda: tmp_path)
    assert cli._service_status()["state"] == "stopped"
    assert cli._service_logs(10) == {"log": str(tmp_path / "service.log"), "lines": []}
    (tmp_path / "worker.pid").write_text(str(os.getpid()))
    status = cli._service_status()
    assert status["running"] is True and status["pid"] == os.getpid()
    (tmp_path / "service.log").write_text("".join(f"line {i}\n" for i in range(5)))
    assert cli._service_logs(2)["lines"] == ["line 3", "line 4"]


# ── what LiveKit routes to the line ─────────────────────────────────────────

def _protos():
    pytest.importorskip("livekit.api")
    from livekit import api
    from livekit.protocol import room, sip

    ours = sip.SIPDispatchRuleInfo(
        sip_dispatch_rule_id="SDR_ours", name="front desk", trunk_ids=["ST_in"],
        rule=sip.SIPDispatchRule(dispatch_rule_individual=sip.SIPDispatchRuleIndividual(
            room_prefix="call-")),
        room_config=room.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT)]))
    own_numbers = sip.SIPDispatchRuleInfo(
        sip_dispatch_rule_id="SDR_named", numbers=["+15550111"],
        room_config=room.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT)]))
    other = sip.SIPDispatchRuleInfo(
        sip_dispatch_rule_id="SDR_other", trunk_ids=["ST_in"],
        room_config=room.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name="else")]))
    trunks = [sip.SIPInboundTrunkInfo(sip_trunk_id="ST_in", numbers=["+15550100"]),
              sip.SIPInboundTrunkInfo(sip_trunk_id="ST_unused", numbers=["+15550199"])]
    return [ours, own_numbers, other], trunks


class FakeLiveKit:
    """The LiveKit API as far as the line reads it."""

    def __init__(self, rules, trunks, sip_fails=False):
        from types import SimpleNamespace

        self.asked = []

        async def list_rooms(request):
            self.asked.append("rooms")
            return SimpleNamespace(rooms=[1, 2])

        async def list_dispatch_rule(request):
            self.asked.append("dispatch rules")
            if sip_fails:
                raise RuntimeError("twirp error unimplemented: SIP is not enabled")
            return SimpleNamespace(items=rules)

        async def list_inbound_trunk(request):
            self.asked.append("inbound trunks")
            return SimpleNamespace(items=trunks)

        self.room = SimpleNamespace(list_rooms=list_rooms)
        self.sip = SimpleNamespace(list_dispatch_rule=list_dispatch_rule,
                                   list_inbound_trunk=list_inbound_trunk)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


def test_the_rules_that_dispatch_the_agent_route_their_numbers_or_their_trunks(cli):
    import asyncio

    rules, trunks = _protos()
    found, warnings = asyncio.run(cli._sip_check(FakeLiveKit(rules, trunks), AGENT))
    assert warnings == [] and found["ok"]
    assert [(r["id"], r["kind"], r["numbers"]) for r in found["rules"]] == [
        ("SDR_ours", "dispatch_rule_individual", ["+15550100"]),
        ("SDR_named", None, ["+15550111"])]
    assert found["numbers"] == ["+15550100", "+15550111"]
    assert (found["rules_read"], found["inbound_trunks_read"]) == (3, 2)
    everywhere = cli._routing([{"sip_dispatch_rule_id": "r",
                                "room_config": {"agents": [{"agent_name": AGENT}]}}],
                              [{"sip_trunk_id": "a", "numbers": ["+1"]},
                               {"sip_trunk_id": "b", "numbers": ["+2"]}], AGENT)
    assert everywhere["numbers"] == ["+1", "+2"]


def test_no_rule_routing_to_the_agent_is_a_warning(cli):
    import asyncio

    rules, trunks = _protos()
    found, warnings = asyncio.run(cli._sip_check(FakeLiveKit(rules[2:], trunks), AGENT))
    assert found["ok"] and found["rules"] == [] and found["numbers"] == []
    assert warnings and f"no SIP dispatch rule routes to {AGENT}" in warnings[0]
    failed, said = asyncio.run(cli._sip_check(FakeLiveKit([], [], sip_fails=True), AGENT))
    assert failed["ok"] is False and "SIP is not enabled" in failed["error"]
    assert "SIP service did not answer" in said[0]


def _wired(cli, monkeypatch, fake, settings=None):
    entry = {"url": "wss://livekit.example.com", "api_key_env": "LKAGENT_C1_API_KEY",
             "secret_env": "LKAGENT_C1_API_SECRET", "allow_write": True}
    values = {"LKAGENT_C1_API_KEY": "APIkey", "LKAGENT_C1_API_SECRET": "secret-value",
              "OPENAI_API_KEY": "sk-test-placeholder"}
    monkeypatch.setattr(cli, "_doctor_gate", lambda report, wanted: None)
    monkeypatch.setattr(cli, "_connections_registry",
                        lambda: ({"connections": {"c1": entry}, "default": None}, None))
    monkeypatch.setattr(cli, "_connections_report", lambda: {"connections": {}})
    monkeypatch.setattr(cli, "_select_connection", lambda reg, wanted: ("c1", entry))
    monkeypatch.setattr(cli, "_resolve_env_key",
                        lambda key: (values.get(key), "env" if key in values else None, None))
    monkeypatch.setattr(cli, "_line_settings", lambda: dict(settings or {}))
    monkeypatch.setattr(cli, "LIVEKIT_API", lambda env: fake)


def test_doctor_reports_the_routing_and_warns_when_nothing_routes(cli, monkeypatch, capsys):
    rules, trunks = _protos()
    _wired(cli, monkeypatch, FakeLiveKit(rules[2:], trunks),
           {"numbers": [{"number": "+15550100"}]})
    try:
        cli.cmd_doctor(None, offline=False)
    except SystemExit:
        pass
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["livekit"] == {"ok": True, "rooms": 2}
    assert report["checks"]["sip"]["rules"] == [] and report["checks"]["sip"]["ok"]
    assert report["checks"]["connection"]["agent_name"] == AGENT
    assert any(f"no SIP dispatch rule routes to {AGENT}" in w for w in report["warnings"])
    assert report["checks"]["openai_key"]["ok"] and "sk-" not in json.dumps(report)

    _wired(cli, monkeypatch, FakeLiveKit(rules, trunks), {"numbers": [{"number": "+15550100"}]})
    try:
        cli.cmd_doctor(None, offline=False)
    except SystemExit:
        pass
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["sip"]["numbers"] == ["+15550100", "+15550111"]
    assert report["warnings"] == []


def test_inventory_reaches_livekit_only_under_network(cli, monkeypatch, tmp_path):
    rules, trunks = _protos()
    fake = FakeLiveKit(rules, trunks)
    _wired(cli, monkeypatch, fake, {"numbers": [{"number": "+15550100", "read_only": True,
                                                 "allowed_capabilities": ["tasks"]}]})
    monkeypatch.setattr(cli, "_pid_file", lambda: tmp_path / "no.pid")
    bare = cli._inventory(False)
    assert fake.asked == []
    assert [d["label"] for d in bare["deferred"]] == ["dispatch rules"]
    assert bare["items"] == [{"group": "numbers", "name": "+15550100", "state": "read-only",
                              "detail": "tasks"}]
    assert bare["service"] == {"state": "stopped"}
    networked = cli._inventory(True)
    assert fake.asked == ["dispatch rules", "inbound trunks"] and networked["deferred"] == []
    phones = {i["name"]: i["state"] for i in networked["items"] if i["group"] == "phone numbers"}
    assert phones == {"+15550100": "configured", "+15550111": "not configured"}
    assert [i["name"] for i in networked["items"] if i["group"] == "dispatch rules"] == [
        "front desk", "SDR_named"]


# ── the number an outgoing call is placed from ─────────────────────────────

NUMBERS = [{"number": "+15550100"}, {"number": "+15550111"}]


def test_from_defaults_to_the_only_number_and_must_name_a_configured_one(cli, capsys):
    assert cli._caller_number(NUMBERS[:1], None)["number"] == "+15550100"
    assert cli._caller_number(NUMBERS, "+1 555 0111")["number"] == "+15550111"
    for numbers, wanted, code in ((NUMBERS, None, "ambiguous_number"),
                                  (NUMBERS, "+15550199", "number_not_configured"),
                                  ([], None, "no_number")):
        with pytest.raises(SystemExit) as stopped:
            cli._caller_number(numbers, wanted)
        assert stopped.value.code == 6 and refusal(capsys)["code"] == code


def test_the_call_dials_from_the_line_number_and_tells_the_worker(cli):
    pytest.importorskip("livekit.api")
    dispatch, dial = cli._outbound_requests(agent_name=AGENT, room="lkagent-out-x",
                                            config={"agent": {}}, number="+15550123",
                                            caller="+15550100", trunk="ST_out")
    assert dial.sip_number == "+15550100" and dial.sip_call_to == "+15550123"
    assert dial.sip_trunk_id == "ST_out" and dial.room_name == "lkagent-out-x"
    envelope = json.loads(dispatch.metadata)["callva"]
    assert (envelope["direction"], envelope["from"], envelope["to"]) == (
        "outbound", "+15550100", "+15550123")
    assert dispatch.agent_name == AGENT


def test_call_refuses_an_ambiguous_number_with_exit_6(cli, monkeypatch, capsys):
    _wired(cli, monkeypatch, None, {"numbers": NUMBERS, "outbound_trunk": "ST_out"})
    monkeypatch.setattr(cli, "_connection", lambda wanted: ("c1", {"allow_write": True}))
    with pytest.raises(SystemExit) as stopped:
        cli.cmd_call(None, "+15550123", None, None)
    assert stopped.value.code == 6 and refusal(capsys)["code"] == "ambiguous_number"
