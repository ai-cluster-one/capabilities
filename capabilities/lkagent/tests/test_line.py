"""The line's own logic, without a LiveKit session, a harness or a store."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

import line  # noqa: E402


# ── voice ───────────────────────────────────────────────────────────────────

def test_an_unknown_voice_is_spoken_as_marin():
    assert line.voice("Cedar") == "cedar"
    assert line.voice(None) == "marin"
    assert line.voice("Aoede") == "marin"


def test_voice_instructions_are_the_voice_modes_with_who_and_where():
    text = line.voice_instructions("", name="Robin", description="Helps the owner.")
    assert text.startswith("# A live voice conversation")
    assert "# How work leaves this call" in text
    assert "# Who you are\n\nYou are Robin. Helps the owner." in text
    assert text.endswith("# Where the session stands\n\n" + line.NEW_SESSION)
    own = line.voice_instructions("Be brief.", name=None, description=None)
    assert own.startswith("Be brief.") and "# Who you are" not in own


def test_results_are_the_voice_modes_wording():
    assert "Tell them the result in your own words" in line.result("Two meetings.")
    assert line.result("Two meetings.").endswith("Two meetings.")
    assert "came back with nothing" in line.result("  ")


# ── the call's configuration ───────────────────────────────────────────────

def test_the_greeting_compiles_as_callva_does():
    assert line.compiled_greeting({}) == ("prompt", line.GREETING_FROM_PROMPT)
    assert line.compiled_greeting({"greeting_type": "exact", "greeting_exact": "Hi"}) == (
        "message", "Hi")
    assert line.compiled_greeting({"greeting_type": "exact"}) == ("message", "")
    assert line.compiled_greeting({"greeting_type": "instruction",
                                   "greeting_instruction": "Ask about X"}) == (
        "prompt", "Ask about X")
    assert line.compiled_greeting({"greeting_type": "instruction",
                                   "greeting_instruction": ""}) == (
        "prompt", line.GREETING_FROM_PROMPT)


def test_call_config_is_callva_livekits_agent_block(tmp_path):
    body = line.call_config({"greeting_type": "exact", "greeting_exact": "Привет!",
                             "webhook_url": "https://hooks.example.com/call"},
                            name="Robin", description="", variables={"task": "t"})
    agent = body["agent"]
    assert agent["greeting"] == "Привет!" and agent["greeting_type"] == "message"
    assert agent["agent_waits_for_user"] is False
    assert agent["max_duration_seconds"] == 600
    assert agent["call_silence_timeout_seconds"] == 30
    assert agent["user_silence_timeout_seconds"] == 0 and agent["max_prompt_attempts"] == 0
    assert agent["prompt_variables"] == {"task": "t"}
    assert line.call_config({"agent_speaks_first": False}, name=None,
                            description=None)["agent"]["agent_waits_for_user"] is True
    assert body["services"]["webhook"]["url"] == "https://hooks.example.com/call"
    assert "services" not in line.call_config({}, name=None, description=None)
    path = line.write_config(tmp_path, body)
    assert oct(os.stat(path).st_mode)[-3:] == "600"
    assert json.loads(path.read_text()) == body


def test_the_config_parses_with_callva_livekit():
    models = pytest.importorskip("callva.livekit.config.models")
    body = line.call_config({"greeting_type": "instruction",
                             "greeting_instruction": "Hi {{name}}",
                             "user_silence_timeout_seconds": 10, "max_prompt_attempts": 3},
                            name="Robin", description="", variables={"name": "Sam"})
    config = models.CallConfig.parse(body, source="file")
    assert config.greeting == "Hi Sam" and config.agent.speaks_first
    assert line.opening(config) == line.Opening(instruction="Hi Sam")
    assert config.agent.max_duration == 600 and config.agent.call_silence_timeout == 30
    assert config.agent.user_silence_timeout == 10 and config.agent.max_prompt_attempts == 3
    quiet = models.CallConfig.parse(line.call_config({}, name=None, description=None),
                                    source="file")
    assert quiet.agent.user_silence_timeout is None and quiet.agent.max_prompt_attempts is None


def test_a_silent_line_is_the_backends_with_what_was_said():
    assert line.liveness_instruction("  ") == line.LIVENESS_INSTRUCTION
    assert line.liveness_instruction("Mine.") == "Mine."
    prompt = line.liveness([("person", "Book it"), ("voice", "Let me check")], "Do it.")
    assert prompt == (line.LIVENESS + "\n\nDo it.\n\n" + line.HANDOVER_HEADER
                      + "\n\n**User:** Book it  \n**Assistant:** Let me check")
    assert line.liveness([], "Do it.") == line.LIVENESS + "\n\nDo it."


def _config(greeting, kind=None, first=True):
    return SimpleNamespace(greeting=greeting,
                           agent=SimpleNamespace(greeting_type=kind, speaks_first=first))


def test_the_opening_has_three_states():
    assert line.opening(_config("Hello", first=False)).silent
    assert line.opening(_config("  ")).silent
    assert line.opening(_config("Hello")) == line.Opening(line="Hello")
    assert line.opening(_config("Greet them", "prompt")) == line.Opening(instruction="Greet them")
    assert line.opening(_config("Hello", "other")) == line.Opening(line="Hello")
    said = line.opening_instruction(line.Opening(line="Hello"))
    assert "Say, in your own words: Hello" in said and said.startswith("Greet the caller now")
    assert "Follow this instruction: X" in line.opening_instruction(line.Opening(instruction="X"))
    assert line.opening_instruction(line.Opening()) is None


# ── backend ─────────────────────────────────────────────────────────────────

def test_backend_defaults_to_claude_sonnet_with_full_access_and_the_end_call_tool():
    profile = line.backend_profile({})
    assert profile["harness"] == "claude" and profile["model"] == "claude-sonnet-5-5"
    assert profile["permission_mode"] == "bypassPermissions" and "effort" not in profile
    assert profile["append_system_prompt"] == line.end_call_tool()
    assert line.END_CALL_DESCRIPTION in profile["append_system_prompt"]


def test_backend_prompt_is_rendered_and_comes_first():
    pytest.importorskip("callva.livekit.config")
    profile = line.backend_profile({"backend_harness": "codex", "backend_prompt": "Task: {{task}}"},
                                   {"task": "book it"})
    assert profile["append_system_prompt"].startswith("Task: book it\n\n# Tool end_call")
    assert profile["codex_config"] == {"sandbox_mode": "danger-full-access"}
    with pytest.raises(ValueError):
        line.backend_profile({"backend_harness": "gemini"})


def test_backend_profile_is_one_the_harness_runner_accepts():
    hr = pytest.importorskip("callva.harness_runner")
    for harness in ("codex", "claude"):
        profile = hr.Profile.from_dict(line.backend_profile({"backend_harness": harness}))
        assert profile.harness == harness


def test_the_claude_process_is_launched_as_the_voice_mode_launches_one():
    profile = line.backend_profile({"backend_effort": "high"})
    new = line.claude_arguments(profile, session_id="s1", resume=False)
    assert new[:7] == ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                       "--verbose", "--include-partial-messages"]
    assert new[-2:] == ["--session-id", "s1"]
    assert new[new.index("--model") + 1] == "claude-sonnet-5-5"
    assert new[new.index("--permission-mode") + 1] == "bypassPermissions"
    assert new[new.index("--effort") + 1] == "high"
    assert line.claude_arguments(profile, session_id="s1", resume=True)[-2:] == ["--resume", "s1"]
    env = line.claude_environment({"PATH": "/bin", "ANTHROPIC_API_KEY": "x", "GITHUB_API_KEY": "y",
                                   "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
                                   "CAPABILITIES_AUTH_CONTEXT": "/a"})
    assert env == {"PATH": "/bin", "CAPABILITIES_AUTH_CONTEXT": "/a"}


def test_stream_lines_are_one_json_object_each():
    sent = line.user_message("hi\nthere", "u1")
    assert sent.endswith("\n") and sent.count("\n") == 1
    assert json.loads(sent) == {"type": "user", "uuid": "u1",
                                "message": {"role": "user", "content": "hi\nthere"}}
    assert json.loads(line.interrupt("r1"))["request"] == {"subtype": "interrupt"}
    assert json.loads(line.unhandled_answer("r2", "elicitation"))["response"]["response"] == {
        "action": "decline"}
    assert line.stream_event({"type": "command_lifecycle", "command_uuid": "u1",
                              "state": "completed"}) == ("command", "u1", "completed")
    assert line.stream_event({"type": "control_request", "request_id": "r3",
                              "request": {"subtype": "elicitation"}}) == (
        "request", "r3", "elicitation")
    assert line.stream_event("nope") == ("other",)


def test_all_capabilities_needs_no_gate_and_a_list_is_one(tmp_path):
    assert line.authority({"allowed_capabilities": ["*"]}, connection="c", number=None,
                          direction="inbound") is None
    doc = line.authority({"allowed_capabilities": ["tasks"]}, connection="c",
                         number="+1", direction="inbound")
    assert doc["source"] == "lkagent" and doc["allowed_capabilities"] == ["tasks"]
    path = line.write_authority(tmp_path, "room/1", doc)
    assert oct(os.stat(path).st_mode)[-3:] == "600"


def test_backend_env_scrubs_the_workers_secrets():
    env = line.backend_env({"PATH": "/bin", "LIVEKIT_API_SECRET": "s", "OPENAI_API_KEY": "k",
                            "CONFIG_URL": "/x"}, authority_file=None, read_only=True)
    assert set(env) == {"PATH", "CAPABILITIES_READ_ONLY"}


# ── answers and the hang-up ────────────────────────────────────────────────

def test_long_answers_go_in_pieces_an_append_carries():
    pytest.importorskip("livekit.agents")
    text = " ".join(["This is one sentence of a long answer."] * 100)
    parts = line.chunks(text)
    assert len(parts) > 1 and all(line.tokens(p) <= line.APPEND_TOKENS for p in parts)
    assert line.chunks("Short.") == ["Short."]
    assert len(line.fit("x" * 5000)) == int(line.APPEND_TOKENS * line.CHARS_PER_TOKEN)


def test_an_end_call_written_as_text_is_cut_and_run():
    spoken, invoked, cut = line.speakable('Bye, talk soon. {"action": "end_call"}[call ended]')
    assert spoken == "Bye, talk soon." and invoked == ["end_call"] and len(cut) == 2
    assert line.speakable("At [14:30] or [the dentist].") == ("At [14:30] or [the dentist].",
                                                              [], [])
    assert line.speakable('{"action": "delete_all"}')[1] == []


# ── the dialogue and its handovers ──────────────────────────────────────────

def test_a_handover_carries_what_was_said_since_the_last_one():
    d = line.Dialogue()
    d.hear("assistant", "Hello, Robin here.")          # before the person: counts for nothing
    d.hear("user", "Hi, what's on my")
    d.hear("user", "calendar tomorrow?")
    d.hear("assistant", "Let me check.")
    text = line.handover(d.hand_over(""))
    assert text == ("*Voice mode messages*\n\n**User:** Hi, what's on my calendar tomorrow?  \n"
                    "**Assistant:** Let me check.")
    assert d.hand_over() == []


def test_the_pending_words_join_once():
    d = line.Dialogue()
    d.hear("user", "Book it.")
    assert d.hand_over("Book it.") == [("person", "Book it.")]
    assert d.hand_over("And email me.") == [("person", "And email me.")]
    d.hear("user", "And email me.")                  # its own item, after the delegation
    assert d.hand_over() == []
