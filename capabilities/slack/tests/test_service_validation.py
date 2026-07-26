import json
from pathlib import Path

from validation import validate_settings

TEMPLATE = (
    Path(__file__).resolve().parent.parent / "service" / "templates" / "settings.json"
)


def _template():
    return json.loads(TEMPLATE.read_text())


def test_shipped_template_is_valid_and_fail_closed(tmp_path):
    settings = _template()
    assert validate_settings(settings, project_root=tmp_path) == []
    assert settings["allowed_users"] == {}
    assert settings["allowed_channels"] == {}
    assert settings["auto_answer"] == {"users": [], "channels": []}
    assert settings["defaults"]["worker"] == "stub"
    assert settings["authority"]["roles"]["direct_user"]["allowed_capabilities"] == {}
    assert settings["authority"]["roles"]["supervisor"] == {
        "allowed_capabilities": {"*": True}
    }


def test_real_worker_does_not_require_a_separate_full_access_switch(tmp_path):
    settings = _template()
    settings["defaults"]["worker"] = "codex"
    assert validate_settings(settings, project_root=tmp_path) == []


def test_auto_answer_must_be_admitted(tmp_path):
    settings = _template()
    settings["auto_answer"]["users"] = ["U1"]
    settings["auto_answer"]["channels"] = ["C1"]
    problems = validate_settings(settings, project_root=tmp_path)
    assert any("auto_answer.users" in problem for problem in problems)
    assert any("auto_answer.channels" in problem for problem in problems)


def test_wildcard_authority_is_valid_for_a_role(tmp_path):
    settings = _template()
    settings["authority"]["roles"]["direct_user"]["allowed_capabilities"] = {"*": True}
    assert validate_settings(settings, project_root=tmp_path) == []


def test_authority_is_validated_on_user_channel_and_member_overrides(tmp_path):
    settings = _template()
    settings["allowed_users"]["U1"] = {"allowed_capabilities": {"../bad": True}}
    settings["allowed_channels"]["C1"] = {
        "members": {"U2": {"allowed_capabilities": {"also/bad": True}}}
    }
    problems = validate_settings(settings, project_root=tmp_path)
    assert any("allowed_users.U1" in problem for problem in problems)
    assert any("members.U2" in problem for problem in problems)


def test_numeric_limits_are_enforced(tmp_path):
    settings = _template()
    settings["defaults"]["max_parallel_jobs"] = 0
    settings["defaults"]["worker_timeout"] = 7200
    problems = validate_settings(settings, project_root=tmp_path)
    assert any("max_parallel_jobs" in problem for problem in problems)
    assert any("worker_timeout" in problem for problem in problems)


def test_roles_controls_and_worker_profiles_are_validated(tmp_path):
    settings = _template()
    settings["assistant_name"] = ""
    settings["direct_messages"]["default_role"] = 7
    settings["control"]["roles"]["supervisor"]["commands"].append("destroy")
    settings["allowed_users"]["U1"] = {
        "role": "",
        "control": {"commands": ["set", "unknown"]},
    }
    settings["defaults"]["workers"]["claude"]["effort"] = "unlimited"
    settings["defaults"]["workers"]["codex"]["extra"] = "value"
    problems = validate_settings(settings, project_root=tmp_path)
    assert any("assistant_name" in problem for problem in problems)
    assert any("direct_messages.default_role" in problem for problem in problems)
    assert any("unknown commands" in problem for problem in problems)
    assert any("claude.effort" in problem for problem in problems)
    assert any("unknown field" in problem for problem in problems)
