"""The service settings surface: closed, one spelling per key, every refusal
naming its path."""

from __future__ import annotations

import pytest

from _cli import load_service

schema = load_service("settings_schema")


def test_an_empty_document_and_a_full_one_hold():
    assert schema.validate_settings({}) == {}
    schema.validate_settings({
        "assistant_name": "Helper", "prompt_mode": "replace",
        "direct_messages": {"mode": "anyone", "default_role": "direct_user"},
        "allowed_users": {"42": {"name": "Ann", "role": "supervisor", "profile":
                                 "telegrambot-codex", "voice_echo": False,
                                 "project": "~/dev/notes", "delegation": "allowed"}},
        "allowed_groups": {"-1001234": {
            "members": {"43": {"role": "channel_admin", "may_address": False}},
            "triggers": ["username"], "gate": {"enabled": True, "mode": "live",
                                               "model": {"claude": "haiku"}},
            "agent_dialogue": {"max_turns": 0}, "context_prompt": "team",
            "topics": {"5": {"context": "API only", "context_mode": "exclusive"}}}},
        "agent_dialogue": {"max_turns": 4},
        "control": {"roles": {"supervisor": {"commands": "*"}}},
        "authority": {"roles": {"group_member": {"allowed_capabilities": {
            "telegrambot": {"verbs": ["history"]}}}}},
        "defaults": {"tail_size": 30, "debounce": 1.5, "voice_transcription": "auto"}})


@pytest.mark.parametrize("document, path", [
    ({"connection": "x"}, "settings.connection"),
    ({"allowed_users": {"-5": {}}}, "settings.allowed_users.-5"),
    ({"allowed_groups": {"5": {}}}, "settings.allowed_groups.5"),
    ({"allowed_groups": {"-5": {"aliases_typo": []}}}, "settings.allowed_groups.-5.aliases_typo"),
    ({"allowed_groups": {"-5": {"triggers": ["username", "username"]}}},
     "settings.allowed_groups.-5.triggers"),
    ({"defaults": {"tail_size": 500}}, "settings.defaults.tail_size"),
    ({"defaults": {"profile": "telegram-claude"}}, "settings.defaults.profile"),
    ({"allowed_users": {"1": {"context_mode": "cut"}}}, "settings.allowed_users.1.context_mode"),
    ({"control": {"roles": {"x": {"commands": ["record"]}}}},
     "settings.control.roles.x.commands[0]"),
])
def test_a_wrong_key_is_refused_with_its_path(document, path):
    with pytest.raises(ValueError) as caught:
        schema.validate_settings(document)
    assert str(caught.value).startswith(path + ":")


def test_profile_names_and_context_prompts_are_found():
    doc = {"defaults": {"profile": "telegrambot-a"},
           "allowed_groups": {"-1": {"profile": "telegrambot-b", "context_prompt": "team",
                                     "topics": {"2": {"profile": "telegrambot-a"}}}}}
    assert schema.profile_names(doc) == {
        "telegrambot-a": ["defaults.profile", "allowed_groups.-1.topics.2.profile"],
        "telegrambot-b": ["allowed_groups.-1.profile"]}
    assert schema.context_prompts(doc) == ["context.team"]
