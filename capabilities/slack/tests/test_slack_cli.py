import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "capabilities" / "slack" / "bin" / "slack"
MANAGER = REPO / "bin" / "capabilities"
BUNDLE = SCRIPT.parent.parent


def _env(tmp_path, project):
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "CLAUDE_PROJECT_DIR": str(project),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        }
    )
    return env


def _project(tmp_path):
    project = tmp_path / "project"
    capdir = project / "capabilities"
    capdir.mkdir(parents=True)
    (capdir / "settings.json").write_text(
        '{"capabilities":{"slack":{"enabled":true}}}\n'
    )
    return project


def _run(argv, env, cwd):
    return subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=120, check=False
    )


def test_manifest_declares_complete_bundle_contract(tmp_path):
    project = _project(tmp_path)
    proc = _run([str(SCRIPT), "manifest", "--json"], _env(tmp_path, project), project)
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads(proc.stdout)
    assert manifest["state"] is True
    assert manifest["docs"]["topics"] == ["service"]
    assert manifest["docs"]["base"]
    assert manifest["service"]["name"] == "assistant"
    keys = {item["key"]: item for item in manifest["credentials"]["keys"]}
    assert keys["SLACK_BOT_TOKEN"]["required"] is True
    assert keys["SLACK_APP_TOKEN"]["required"] is False


def test_guide_menu_surfaces_shipped_service_guide(tmp_path):
    project = _project(tmp_path)
    proc = _run([str(SCRIPT), "guide"], _env(tmp_path, project), project)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == [
        {
            "topic": "service",
            "title": "Slack service",
            "preview": (
                "The optional Slack service receives Socket Mode events and applies "
                "three independent decisions: admission, answer-versus-relay routing, "
                "and per-role capability authority. Run `slack help` for the lifecycle "
                "command surface and "
                "credential scopes; this guide explains the operating model."
            ),
            "command": "slack guide service",
        }
    ]


def test_service_start_obeys_write_gate_before_credentials_or_network(tmp_path):
    project = _project(tmp_path)
    env = _env(tmp_path, project)
    initialized = _run([str(SCRIPT), "service", "init"], env, project)
    assert initialized.returncode == 0, initialized.stderr
    conn = project / "capabilities" / "slack" / "connections.json"
    conn.write_text(
        json.dumps(
            {
                "default": "workspace",
                "connections": {"workspace": {"allow_write": False}},
            }
        )
        + "\n"
    )
    denied = _run([str(SCRIPT), "service", "start"], env, project)
    assert denied.returncode == 4
    assert json.loads(denied.stderr)["error"]["code"] == "read_only"

    conn.write_text(
        json.dumps(
            {
                "default": "workspace",
                "connections": {"workspace": {"allow_write": True}},
            }
        )
        + "\n"
    )
    missing = _run([str(SCRIPT), "service", "start"], env, project)
    assert missing.returncode == 2
    assert json.loads(missing.stderr)["error"]["code"] == "credentials_missing"


def test_invalid_conversation_policy_fails_before_network(tmp_path):
    project = _project(tmp_path)
    policy = project / "capabilities" / "slack" / "policy.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(
        json.dumps(
            {
                "direct_messages": {"mode": "typo"},
                "allowed_users": {},
                "allowed_channels": {},
                "default_channel_policy": "allowed_only",
            }
        )
        + "\n"
    )
    env = _env(tmp_path, project)
    env["SLACK_BOT_TOKEN"] = "not-used"
    proc = _run([str(SCRIPT), "read", "D1"], env, project)
    assert proc.returncode == 6
    assert json.loads(proc.stderr)["error"]["code"] == "bad_policy"


def test_manager_installs_the_complete_slack_bundle(tmp_path):
    project = _project(tmp_path)
    env = _env(tmp_path, project)
    cap_home = tmp_path / "registry"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env.update(
        {
            "CAPABILITIES_HOME": str(cap_home),
            "CAPABILITIES_BIN": str(bin_dir),
            "SLACK_BOT_TOKEN": "test-token",
        }
    )
    proc = _run(
        [str(MANAGER), "install", "slack", "--from", str(BUNDLE), "--yes"], env, project
    )
    assert proc.returncode == 0, proc.stderr
    installed = cap_home / "slack"
    assert (installed / "service" / "daemon.py").is_file()
    assert (installed / "service" / "validation.py").is_file()
    assert (installed / "guides" / "service.md").is_file()
    assert (installed / "slack").is_file()
    assert not (installed / "bin" / "slack").exists()
    manifest = json.loads((installed / "manifest.json").read_text())
    assert manifest["service"]["name"] == "assistant"


def test_audit_rejects_service_directory_missing_from_manifest(tmp_path):
    bundle = tmp_path / "slack"
    shutil.copytree(BUNDLE, bundle)
    script = bundle / "bin" / "slack"
    script.write_text(
        script.read_text().replace(
            "SERVICE = {", "SERVICE = None\n_UNDECLARED_SERVICE = {", 1
        )
    )
    proc = _run(
        [str(MANAGER), "audit", "slack", "--from", str(bundle)], dict(os.environ), REPO
    )
    assert proc.returncode == 7
    failures = json.loads(proc.stdout)["failures"]
    assert any("service/ ships" in failure for failure in failures)


def test_audit_rejects_guide_without_preview(tmp_path):
    bundle = tmp_path / "slack"
    shutil.copytree(BUNDLE, bundle)
    (bundle / "guides" / "service.md").write_text("# Slack service\n")
    proc = _run(
        [str(MANAGER), "audit", "slack", "--from", str(bundle)], dict(os.environ), REPO
    )
    assert proc.returncode == 7
    failures = json.loads(proc.stdout)["failures"]
    assert any("incomplete menu entry" in failure for failure in failures)
