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
    assert settings["defaults"]["workspace_mode"] == "read_only"
    assert settings["authority"]["roles"]["default"] == {
        "allowed_capabilities": {},
        "network_domains": [],
    }


def test_real_worker_requires_explicit_trusted_ingress(tmp_path):
    settings = _template()
    settings["defaults"]["worker"] = "codex"
    problems = validate_settings(settings, project_root=tmp_path)
    assert any("trusted_ingress" in problem for problem in problems)
    settings["defaults"]["trusted_ingress"] = True
    assert not any(
        "trusted_ingress" in problem
        for problem in validate_settings(settings, project_root=tmp_path)
    )
    assert any(
        "worker_home" in problem
        for problem in validate_settings(settings, project_root=tmp_path)
    )
    worker_home = tmp_path / "worker-home"
    worker_home.mkdir()
    worker_home.chmod(0o700)
    settings["defaults"]["worker_home"] = str(worker_home)
    assert validate_settings(settings, project_root=tmp_path) == []


def test_auto_answer_must_be_admitted(tmp_path):
    settings = _template()
    settings["auto_answer"]["users"] = ["U1"]
    settings["auto_answer"]["channels"] = ["C1"]
    problems = validate_settings(settings, project_root=tmp_path)
    assert any("auto_answer.users" in problem for problem in problems)
    assert any("auto_answer.channels" in problem for problem in problems)


def test_wildcard_authority_is_invalid(tmp_path):
    settings = _template()
    settings["authority"]["roles"]["default"]["allowed_capabilities"] = {"*": True}
    problems = validate_settings(settings, project_root=tmp_path)
    assert any("wildcard" in problem for problem in problems)


def test_wildcard_network_domain_is_invalid(tmp_path):
    settings = _template()
    settings["authority"]["roles"]["default"]["network_domains"] = ["*"]
    problems = validate_settings(settings, project_root=tmp_path)
    assert any("overbroad domains" in problem for problem in problems)


def test_codex_cannot_receive_unenforceable_capability_authority(tmp_path):
    settings = _template()
    worker_home = tmp_path / "worker-home"
    worker_home.mkdir(mode=0o700)
    settings["defaults"].update(
        {
            "worker": "codex",
            "worker_home": str(worker_home),
            "trusted_ingress": True,
        }
    )
    settings["authority"]["roles"]["default"]["allowed_capabilities"] = {
        "youtrack": True
    }
    problems = validate_settings(settings, project_root=tmp_path)
    assert any("codex worker" in problem for problem in problems)


def test_real_worker_home_must_be_private(tmp_path):
    settings = _template()
    worker_home = tmp_path / "worker-home"
    worker_home.mkdir(mode=0o755)
    worker_home.chmod(0o755)
    settings["defaults"].update(
        {
            "worker": "claude",
            "worker_home": str(worker_home),
            "trusted_ingress": True,
        }
    )
    problems = validate_settings(settings, project_root=tmp_path)
    assert any("group or other users" in problem for problem in problems)


def _claude_settings(tmp_path):
    settings = _template()
    worker_home = tmp_path / "worker-home"
    worker_home.mkdir(mode=0o700)
    worker_home.chmod(0o700)
    settings["defaults"].update(
        {
            "worker": "claude",
            "worker_home": str(worker_home),
            "trusted_ingress": True,
        }
    )
    return settings, worker_home


def _read_root_problems(settings, tmp_path):
    return [
        problem
        for problem in validate_settings(settings, project_root=tmp_path)
        if "read_roots" in problem
    ]


def test_read_roots_accept_existing_directories(tmp_path):
    settings, _home = _claude_settings(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    settings["defaults"]["read_roots"] = [str(docs)]
    assert _read_root_problems(settings, tmp_path) == []


def test_read_roots_reject_missing_relative_and_overbroad_paths(tmp_path):
    settings, _home = _claude_settings(tmp_path)
    settings["defaults"]["read_roots"] = [
        str(tmp_path / "missing"),
        "relative/docs",
        "/",
        str(Path.home()),
        str(Path.home().parent),
    ]
    problems = _read_root_problems(settings, tmp_path)
    assert any("does not exist" in problem for problem in problems)
    assert any("absolute path" in problem for problem in problems)
    assert sum("overbroad" in problem for problem in problems) == 3


def test_read_roots_reject_worker_home_exposure(tmp_path):
    settings, worker_home = _claude_settings(tmp_path)
    settings["defaults"]["read_roots"] = [str(worker_home), str(tmp_path)]
    problems = _read_root_problems(settings, tmp_path)
    assert sum("exposes the worker home" in problem for problem in problems) == 2


def test_read_roots_must_be_string_list(tmp_path):
    settings, _home = _claude_settings(tmp_path)
    settings["defaults"]["read_roots"] = "/tmp"
    assert any(
        "list of strings" in problem for problem in _read_root_problems(settings, tmp_path)
    )


def test_codex_rejects_read_roots(tmp_path):
    settings, _home = _claude_settings(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    settings["defaults"]["worker"] = "codex"
    settings["defaults"]["read_roots"] = [str(docs)]
    assert any(
        "claude worker only" in problem for problem in _read_root_problems(settings, tmp_path)
    )


def test_numeric_limits_are_enforced(tmp_path):
    settings = _template()
    settings["defaults"]["max_parallel_jobs"] = 0
    settings["defaults"]["worker_timeout"] = 7200
    problems = validate_settings(settings, project_root=tmp_path)
    assert any("max_parallel_jobs" in problem for problem in problems)
    assert any("worker_timeout" in problem for problem in problems)
