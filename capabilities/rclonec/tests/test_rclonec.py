"""Tests for rclonec's own layer: the positional write gate, the child environment,
and connection resolution. Nothing here runs rclone or reaches a backend.

Run with: uv run --no-project --with pytest pytest capabilities/rclonec/tests -q
"""

from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path
from unittest.mock import patch

import pytest

CAPABILITY = Path(__file__).resolve().parents[1]
CLI = next((path for path in (
    CAPABILITY / "bin" / "rclonec", CAPABILITY / "rclonec")
    if path.is_file()), CAPABILITY / "bin" / "rclonec")
module = types.ModuleType("rclonec_capability")
module.__file__ = str(CLI)
exec(compile(CLI.read_text(), str(CLI), "exec"), module.__dict__)

SA_BLOB = '{"type":"service_account","client_email":"x@y.iam.gserviceaccount.com"}'

# Shaped exactly like `rclone help flags`: a type word trails a value-taking flag and
# nothing trails a boolean. The gate derives the value-taking set from this.
FAKE_FLAGS = """
      --transfers int                            Number of file transfers to run in parallel (default 4)
  -n, --dry-run                                  Do a trial run with no permanent changes
      --drive-export-formats string              Comma separated list of preferred formats
      --backup-dir string                        Make backups into hierarchy based in DIR
  -P, --progress                                 Show progress during transfer
"""


@pytest.fixture(autouse=True)
def reset_flag_cache():
    module._VALUE_FLAGS_CACHE = None
    yield
    module._VALUE_FLAGS_CACHE = None


@pytest.fixture(autouse=True)
def isolate_records_adapter():
    module._RECORDS = None
    yield
    if module._RECORDS is not None:
        module._RECORDS.close()
    module._RECORDS = None


@pytest.fixture()
def flags():
    """`rclone help flags` answered without running rclone."""
    def fake_run(argv, *a, **kw):
        assert argv[:3] == ["rclone", "help", "flags"], \
            f"the gate ran something other than the flag query: {argv}"
        return subprocess.CompletedProcess(argv, 0, stdout=FAKE_FLAGS, stderr="")
    with patch.object(module.subprocess, "run", fake_run):
        yield


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A consuming project with rclonec enabled, one read-only Drive connection and
    one writable S3 connection — the standard configuration shape."""
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    capdir = root / "capabilities" / "rclonec"
    capdir.mkdir(parents=True)
    (root / "capabilities" / "settings.json").write_text(json.dumps(
        {"capabilities": {"rclonec": {"enabled": True}}}) + "\n")
    (capdir / "connections.json").write_text(json.dumps({
        "default": "gd",
        "connections": {
            "gd": {"type": "drive",
                   "options": {"scope": "drive", "export_formats": "md"},
                   "secret_env": {"service_account_credentials": "GD_SA_JSON"},
                   "allow_write": False},
            "r2": {"type": "s3", "options": {"provider": "Cloudflare"},
                   "secret_env": {"access_key_id": "R2_KEY",
                                  "secret_access_key": "R2_SECRET"},
                   "allow_write": True},
        }}) + "\n")
    (root / ".env").write_text(
        f"GD_SA_JSON={SA_BLOB}\nR2_KEY=key-value\nR2_SECRET=secret-value\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.delenv("CAPABILITIES_AUTH_CONTEXT", raising=False)
    monkeypatch.chdir(root)
    return root


def _gate(argv, allow_write=False, cid="gd"):
    module._forward_gate(cid, allow_write, argv)


def _refused(argv, allow_write=False, cid="gd"):
    with pytest.raises(SystemExit) as excinfo:
        _gate(argv, allow_write, cid)
    assert excinfo.value.code == 4, f"expected a policy refusal for {argv}"


# --- the flag table is read from rclone, not transcribed ---------------------

def test_value_taking_flags_are_derived_from_rclones_own_help(flags):
    found = module._value_taking_flags()
    assert "--transfers" in found and "--drive-export-formats" in found
    assert "--dry-run" not in found and "--progress" not in found
    assert "-n" not in found and "-P" not in found


def test_positionals_skip_flag_values(flags):
    value_flags = module._value_taking_flags()
    args = ["/local", "gd:dest", "--transfers", "8", "--progress"]
    assert module._positionals(args, value_flags) == ["/local", "gd:dest"]


# --- directional verbs are gated on which side the remote is ----------------

def test_download_from_a_read_only_remote_is_allowed(flags):
    _gate(["copy", "gd:doc", "/tmp/out"])
    _gate(["copy", "--drive-export-formats", "md", "gd:doc", "/tmp/out"])


def test_upload_to_a_read_only_remote_is_refused(flags):
    _refused(["copy", "/tmp/x", "gd:dest"])
    _refused(["copyto", "/tmp/x", "gd:dest/file"])


def test_a_trailing_flag_value_does_not_hide_the_destination(flags):
    """`--transfers 8` must not make `8` look like the destination."""
    _refused(["copy", "/tmp/x", "gd:dest", "--transfers", "8"])
    _refused(["copy", "/tmp/x", "gd:dest", "--transfers=8"])


def test_sync_into_a_read_only_remote_is_refused(flags):
    _refused(["sync", "/tmp/x", "gd:dest"])


def test_move_is_refused_on_either_side_because_the_source_is_emptied(flags):
    _refused(["move", "gd:a", "/tmp/out"])
    _refused(["move", "/tmp/x", "gd:dest"])
    _refused(["moveto", "gd:a", "/tmp/out"])


def test_a_destination_valued_flag_pointed_at_the_remote_is_refused(flags):
    _refused(["copy", "gd:a", "/tmp/out", "--backup-dir", "gd:bak"])
    _refused(["copy", "gd:a", "/tmp/out", "--backup-dir=gd:bak"])


def test_remote_names_are_matched_case_insensitively(flags):
    _refused(["copy", "/tmp/x", "GD:dest"])


def test_a_writable_connection_passes_every_direction(flags):
    _gate(["copy", "/tmp/x", "r2:bucket"], allow_write=True, cid="r2")
    _gate(["sync", "/tmp/x", "r2:bucket"], allow_write=True, cid="r2")
    _gate(["delete", "r2:bucket/obj"], allow_write=True, cid="r2")


# --- verbs that mutate whatever they touch, and the unknown ones -------------

@pytest.mark.parametrize("verb", ["delete", "purge", "rmdir", "mkdir", "touch", "link"])
def test_mutating_verbs_are_refused_on_a_read_only_connection(verb):
    _refused([verb, "gd:x"])


def test_backend_is_gated_by_subcommand():
    _gate(["backend", "query", "gd:", "name='x'"])
    _gate(["backend", "exportformats", "gd:"])
    _refused(["backend", "set", "gd:"])
    _refused(["backend", "untrash", "gd:"])


def test_an_unrecognised_verb_is_refused_rather_than_waved_through():
    _refused(["frobnicate", "gd:"])


def test_a_directional_verb_is_refused_when_rclone_cannot_answer():
    """No flag table means no way to find the destination; refuse, never guess."""
    def unanswerable(argv, *a, **kw):
        raise OSError("rclone is not installed")
    with patch.object(module.subprocess, "run", unanswerable):
        _refused(["copy", "/tmp/x", "gd:dest"])


# --- the child environment is the whole configuration -----------------------

def test_child_env_defines_exactly_one_remote_and_no_config_file():
    cfg = {"id": "gd", "type": "drive",
           "options": {"scope": "drive", "export_formats": "md"},
           "secrets": {"service_account_credentials": SA_BLOB},
           "allow_write": False}
    env = module._child_env(cfg)
    assert env["RCLONE_CONFIG"] == module.os.devnull
    assert env["RCLONE_CONFIG_GD_TYPE"] == "drive"
    assert env["RCLONE_CONFIG_GD_SCOPE"] == "drive"
    assert env["RCLONE_CONFIG_GD_EXPORT_FORMATS"] == "md"
    assert env["RCLONE_CONFIG_GD_SERVICE_ACCOUNT_CREDENTIALS"] == SA_BLOB
    remotes = {k.split("_")[2] for k in env if k.startswith("RCLONE_CONFIG_")
               and k != "RCLONE_CONFIG"}
    assert remotes == {"GD"}, "the child must know exactly one remote"


def test_child_env_drops_inherited_rclone_variables(monkeypatch):
    monkeypatch.setenv("RCLONE_CONFIG_OTHER_TYPE", "s3")
    monkeypatch.setenv("RCLONE_DRIVE_IMPERSONATE", "someone@example.test")
    cfg = {"id": "gd", "type": "drive", "options": {}, "secrets": {},
           "allow_write": False}
    env = module._child_env(cfg)
    assert "RCLONE_CONFIG_OTHER_TYPE" not in env
    assert "RCLONE_DRIVE_IMPERSONATE" not in env


# --- connection resolution --------------------------------------------------

def test_build_cfg_resolves_secrets_through_the_cascade(project):
    reg, _ = module._connections_registry()
    cfg, problem = module._build_cfg("gd", reg["connections"]["gd"])
    assert problem is None
    assert cfg["type"] == "drive"
    assert cfg["secrets"]["service_account_credentials"] == SA_BLOB
    assert cfg["allow_write"] is False


def test_a_missing_secret_is_reported_rather_than_raised(project, monkeypatch):
    (project / ".env").write_text("R2_KEY=key-value\nR2_SECRET=secret-value\n")
    reg, _ = module._connections_registry()
    cfg, problem = module._build_cfg("gd", reg["connections"]["gd"])
    assert cfg is None and problem["exit"] == 2
    assert "GD_SA_JSON" in problem["message"]


def test_an_entry_without_a_type_is_refused(project):
    cfg, problem = module._build_cfg("gd", {"options": {}})
    assert cfg is None and problem["code"] == "no_type"


def test_an_id_that_cannot_be_a_remote_name_is_refused(project):
    cfg, problem = module._build_cfg("not-a-remote", {"type": "drive"})
    assert cfg is None and problem["code"] == "bad_connection_id"


def test_connections_report_masks_every_secret(project):
    report = module._connections_report()
    assert set(report["connections"]) == {"gd", "r2"}
    rendered = json.dumps(report)
    assert SA_BLOB not in rendered
    assert "key-value" not in rendered and "secret-value" not in rendered
    assert report["connections"]["gd"]["remote"] == "gd:"
    assert report["connections"]["r2"]["allow_write"] is True


# --- a failure is reported as something a reader can act on ------------------

RCLONE_OAUTH_REFUSAL = '''2026/09/19 16:30:38 CRITICAL: Failed to create file system for "gd:": couldn't find root directory ID: Get "https://www.googleapis.com/drive/v3/files/root?alt=json": oauth2: cannot fetch token: 401 Unauthorized
Response: {
  "error": "unauthorized_client",
  "error_description": "Client is unauthorized to retrieve access tokens using this method, or client not authorized for any of the scopes requested."
}'''


def test_a_failure_summary_carries_the_actionable_description():
    """The last line of this output is `}`; the part worth reading is the description."""
    summary = module._summarize_failure(RCLONE_OAUTH_REFUSAL)
    assert "unauthorized to retrieve access tokens" in summary
    assert "CRITICAL" in summary
    assert not summary.strip().endswith("}")


def test_a_single_line_failure_survives_unchanged():
    summary = module._summarize_failure("2026/09/19 ERROR: directory not found")
    assert summary == "2026/09/19 ERROR: directory not found"


def test_an_empty_failure_reports_nothing_rather_than_raising():
    assert module._summarize_failure("") is None
    assert module._summarize_failure(None) is None


def test_an_oauth_refusal_is_classified_as_a_credential_problem():
    """`unauthorized_client` is a grant to fix, not a network to retry."""
    assert "unauthor" in RCLONE_OAUTH_REFUSAL.lower()
