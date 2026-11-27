"""The line's numbers: a call is taken only on a configured number and runs under that
number's rights; an outgoing call is placed from one; and recording is off unless set."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

import line  # noqa: E402

FRONT = {"number": "+15550100", "instructions": "Answer for the front desk.",
         "allowed_capabilities": ["tasks"], "read_only": True}
SALES = {"number": "+1 555 0111", "instructions": "", "allowed_capabilities": ["*"],
         "read_only": False}


# ── matching and checking ──────────────────────────────────────────────────

def test_a_number_is_matched_on_its_digits():
    assert line.number_entry([FRONT, SALES], "+15550100") is FRONT
    assert line.number_entry([FRONT, SALES], "15550111") is SALES
    assert line.number_entry([FRONT, SALES], "+15550199") is None
    assert line.number_entry([FRONT], None) is None and line.number_entry(None, "+1") is None


def test_numbers_keyed_by_the_number_read_as_the_list_did():
    keyed = line.keyed([FRONT, SALES])
    assert keyed == {"+15550100": {k: v for k, v in FRONT.items() if k != "number"},
                     "+15550111": {k: v for k, v in SALES.items() if k != "number"}}
    assert line.keyed(keyed) is keyed and line.keyed(None) == {}
    assert line.number_entry(keyed, "+1 555 0100")["instructions"] == FRONT["instructions"]
    assert line.number_entry(keyed, "+15550199") is None
    assert [e["number"] for e in line.entries(keyed)] == ["+15550100", "+15550111"]
    assert line.numbers_problems(keyed) == []
    found = line.numbers_problems({"15550100": {}, "+15550122": {"read_only": "no"}})
    assert any("E.164" in p for p in found)
    assert any("numbers['+15550122'].read_only" in p for p in found)


def test_the_called_number_is_the_sip_trunk_number_inbound_and_the_dispatched_one_outbound():
    attrs = {"sip.trunkPhoneNumber": "+15550100", "sip.phoneNumber": "+15550123"}
    assert line.line_number("inbound", attrs, None) == "+15550100"
    assert line.line_number("inbound", {}, "+15550111") is None
    assert line.line_number("outbound", {}, "+15550111") == "+15550111"
    assert line.line_number("outbound", attrs, None) == "+15550100"


def test_a_numbers_setting_is_checked_whole():
    assert line.numbers_problems([FRONT, SALES]) == [] and line.numbers_problems(None) == []
    found = line.numbers_problems([{"number": "front desk"}, FRONT, dict(FRONT),
                                   {"number": "+15550122", "allowed_capabilities": "tasks",
                                    "read_only": "no", "extra": 1}, "x"])
    assert any("numbers[0].number is a phone number" in p for p in found)
    assert any("configured twice" in p for p in found)
    assert any("numbers[3] has keys this line does not read: extra" in p for p in found)
    assert any("numbers[3].allowed_capabilities" in p for p in found)
    assert any("numbers[3].read_only" in p for p in found)
    assert any("numbers[4] is an object" in p for p in found)


def test_recording_is_off_unless_set_true():
    assert line.recording({}) is False
    assert line.recording({"recording": False}) is False
    assert line.recording({"recording": "yes"}) is False
    assert line.recording({"recording": True}) is True


# ── the worker ──────────────────────────────────────────────────────────────

@pytest.fixture
def worker(monkeypatch):
    pytest.importorskip("livekit.agents")
    pytest.importorskip("callva.livekit.call")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-placeholder")
    monkeypatch.setenv("LKAGENT_AGENT_NAME", "lkagent-test")
    monkeypatch.setenv("LKAGENT_LINE", json.dumps({"settings": {"numbers": [FRONT]}}))
    sys.modules.pop("worker", None)
    import worker as module

    return module


class Room:
    def __init__(self, participants):
        self.name = "call-room"
        self.metadata = ""
        self.remote_participants = participants

    def on(self, *_args):
        return None


class Ctx:
    def __init__(self, metadata="", participants=None):
        self.job = SimpleNamespace(metadata=metadata, room=SimpleNamespace(sid="RM_x"),
                                   id="AJ_x")
        self.room = Room(participants or {})
        self.tagger = None

    async def connect(self):
        return None


def _caller(called="+15550199"):
    return SimpleNamespace(identity="sip_caller", name="Caller Name", kind=None, metadata="",
                           attributes={"sip.phoneNumber": "+15550123",
                                       "sip.trunkPhoneNumber": called})


def _run_entrypoint(worker, monkeypatch, ctx):
    ended, made = [], []

    async def end(ctx=None, **kwargs):
        ended.append(kwargs)

    async def load():
        return SimpleNamespace(prompt="", variables={}, agent=SimpleNamespace())

    def session(**kwargs):
        made.append(kwargs)
        raise RuntimeError("the session would start here")

    monkeypatch.setattr(worker.callva_call, "end", end)
    monkeypatch.setattr(sys.modules["callva.livekit.call.release"], "end", end)
    monkeypatch.setattr(worker.callva_config, "load", load)
    monkeypatch.setattr(worker, "GPTLiveModel", lambda **kwargs: kwargs)
    monkeypatch.setattr(worker, "AgentSession", session)
    asyncio.run(worker.entrypoint(ctx))
    return ended, made


def test_an_inbound_call_to_an_unconfigured_number_is_refused_at_once_and_recorded_failed(
        worker, monkeypatch):
    import calls

    ctx = Ctx(participants={"p": _caller("+15550199")})
    ended, made = _run_entrypoint(worker, monkeypatch, ctx)
    assert made == []                                    # no session was ever started
    assert ended == [{"reason": "+15550199 is not one of this line's numbers",
                      "wait": False, "ended_by": None}]
    recorded = []
    monkeypatch.setattr(worker.RECORDER, "record",
                        lambda body, **facts: recorded.append((body, facts)))
    asyncio.run(worker.on_session_end(ctx))
    body, facts = recorded[0]
    row = calls.row(body, agent_id="a", **facts)
    assert row["status"] == "failed"
    assert row["failure_reason"] == "+15550199 is not one of this line's numbers"
    assert (row["direction"], row["from_number"], row["to_number"]) == (
        "inbound", "+15550123", "+15550199")
    assert row["duration_seconds"] is None and row["ended_by"] is None


def test_an_inbound_call_to_a_configured_number_goes_on_to_its_session(worker, monkeypatch):
    ctx = Ctx(participants={"p": _caller("+1 (555) 0100")})
    ended, made = _run_entrypoint(worker, monkeypatch, ctx)
    assert len(made) == 1
    assert ended == [{"reason": "the call could not be conducted", "wait": False,
                      "ended_by": None}]


def test_an_outbound_call_from_an_unconfigured_number_is_refused(worker, monkeypatch):
    metadata = json.dumps({"callva": {"direction": "outbound", "to": "+15550123",
                                      "from": "+15550111"}})
    ended, made = _run_entrypoint(worker, monkeypatch, Ctx(metadata=metadata))
    assert made == [] and ended[0]["reason"] == "+15550111 is not one of this line's numbers"


def test_the_backend_runs_under_the_rights_of_its_number(worker, monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "STATE_DIR", tmp_path)
    claude = worker.make_backend("+15550123", "inbound", "call-room", {}, FRONT)
    assert claude.env.get("CAPABILITIES_READ_ONLY") == "1"
    authority = json.loads(Path(claude.env["CAPABILITIES_AUTH_CONTEXT"]).read_text())
    assert authority["allowed_capabilities"] == ["tasks"]
    assert authority["source"] == "lkagent" and authority["chat_id"] == "+15550123"
    prompt = claude.profile["append_system_prompt"]
    assert prompt.startswith("Answer for the front desk.\n\n# Tool end_call")
    every = worker.make_backend("+15550123", "outbound", "call-room", {}, SALES)
    assert "CAPABILITIES_READ_ONLY" not in every.env
    assert "CAPABILITIES_AUTH_CONTEXT" not in every.env
    assert every.profile["append_system_prompt"] == line.end_call_tool()


def test_the_session_records_only_when_recording_is_on(worker, monkeypatch):
    class Session:
        async def start(self, **kwargs):
            self.kwargs = kwargs

    off = Session()
    asyncio.run(worker.start_session(off, "agent", "room"))
    assert off.kwargs == {"agent": "agent", "room": "room", "record": False}
    monkeypatch.setitem(worker.SETTINGS, "recording", True)
    on = Session()
    asyncio.run(worker.start_session(on, "agent", "room"))
    assert on.kwargs["record"] is True
