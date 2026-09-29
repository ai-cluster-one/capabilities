"""Under CAPABILITIES_READ_ONLY askproject escalates to nothing.

Act mode exists to hand a peer write tools, so under the switch it is refused
before any peer starts. Read mode still runs, and its peer inherits the switch
by name, so the peer's own capability calls change nothing either. The session
askproject records about the call is its operational state and is still kept.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "askproject", CAPABILITY / "askproject")
    if path.is_file()), CAPABILITY / "bin" / "askproject")

SWITCH = "CAPABILITIES_READ_ONLY"

# Records what switch it was started under, so both the refusal (no record)
# and the carry (the value it saw) are read off the peer itself.
CLAUDE_FAKE = r'''#!/usr/bin/env python3
import json
import os

with open(os.environ["PEER_MARKER"], "a") as seen:
    seen.write(os.environ.get("CAPABILITIES_READ_ONLY", "<unset>") + "\n")
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "PEER ANSWER", "session_id": "claude-session",
    "duration_ms": 1, "num_turns": 1, "total_cost_usd": 0.01, "usage": {},
}), flush=True)
'''


def _project(path: Path) -> Path:
    envelope = path / "capabilities"
    envelope.mkdir(parents=True, exist_ok=True)
    (envelope / "settings.json").write_text(json.dumps({
        "capabilities": {"askproject": {"enabled": True}},
    }) + "\n")
    return path


@pytest.fixture()
def lab(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "claude"
    fake.write_text(CLAUDE_FAKE)
    fake.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["PEER_MARKER"] = str(tmp_path / "peer-saw")
    for leaked in ("CLAUDE_PROJECT_DIR", SWITCH, "CAPABILITIES_STORE_URL"):
        env.pop(leaked, None)
    return {"caller": _project(tmp_path / "caller"),
            "target": _project(tmp_path / "target"),
            "marker": tmp_path / "peer-saw", "env": env}


def _run(lab, *args: str, switch: str | None = None):
    env = dict(lab["env"])
    if switch is not None:
        env[SWITCH] = switch
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=lab["caller"], env=env,
        text=True, capture_output=True, timeout=60)


def _peer_saw(lab) -> list[str]:
    marker = lab["marker"]
    return marker.read_text().splitlines() if marker.exists() else []


@pytest.mark.parametrize("value", ["1", "true"])
def test_act_is_refused_before_any_peer_starts(lab, value):
    proc = _run(lab, str(lab["target"]), "change it", "--act", "--quiet", switch=value)
    assert proc.returncode == 4, proc.stdout + proc.stderr
    error = json.loads(proc.stderr.strip().splitlines()[-1])["error"]
    assert error["code"] == "read_only_switch"
    assert "--act" in error["message"] and SWITCH in error["message"]
    assert _peer_saw(lab) == []


@pytest.mark.parametrize("value", ["1", "TRUE"])
def test_read_mode_runs_and_carries_the_switch_to_its_peer(lab, value):
    proc = _run(lab, str(lab["target"]), "what is here?", "--quiet", switch=value)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["answer"] == "PEER ANSWER"
    assert _peer_saw(lab) == ["1"]
    # The session it recorded is operational state, and it was kept.
    targets = json.loads(_run(lab, "targets", "--json", switch=value).stdout)
    assert [entry["target"] for entry in targets["targets"]] == [str(lab["target"])]


@pytest.mark.parametrize("value", [None, "0", "false"])
def test_with_the_switch_off_read_hands_the_peer_nothing_new(lab, value):
    proc = _run(lab, str(lab["target"]), "what is here?", "--quiet", switch=value)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _peer_saw(lab) == [value if value is not None else "<unset>"]


@pytest.mark.parametrize("value", [None, "0", "false"])
def test_with_the_switch_off_act_still_starts_its_peer(lab, value):
    proc = _run(lab, str(lab["target"]), "change it", "--act", "--quiet", switch=value)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(_peer_saw(lab)) == 1
