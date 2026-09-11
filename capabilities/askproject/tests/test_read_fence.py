"""Read mode removes the harmful tools from the turn rather than permitting three.

`--allowedTools` is an allow rule: it says the listed tools need no prompt, and
leaves Bash, Edit and Write in the session for the calling machine's own
settings to decide about. Under a user settings file that allows `Bash(*)` a
read-mode peer therefore kept a working shell. `--tools` decides which built-in
tools exist at all, so these tests pin the read invocation to it and keep act
mode's authority intact.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "askproject", CAPABILITY / "askproject")
    if path.is_file()), CAPABILITY / "bin" / "askproject")


# Writes the argv it was spawned with, so the fence is read off the real
# invocation rather than off the source that builds it.
CLAUDE_FAKE = r'''#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["PEER_ARGV"], "w") as handle:
    json.dump(sys.argv[1:], handle)
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


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    caller = _project(tmp_path / "caller")
    target = _project(tmp_path / "other")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake = fake_bin / "claude"
    fake.write_text(CLAUDE_FAKE)
    fake.chmod(0o755)

    recorded = tmp_path / "peer-argv.json"
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["PEER_ARGV"] = str(recorded)
    env.pop("CLAUDE_PROJECT_DIR", None)

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(target), "what is here?", "--quiet", *extra],
        cwd=caller, env=env, text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(recorded.read_text())


def test_read_mode_restricts_the_tool_set_itself(tmp_path):
    argv = _argv(tmp_path)

    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    # The allow rule stays so the three reading tools still need no prompt.
    assert argv[argv.index("--allowedTools") + 1] == "Read,Glob,Grep"


def test_act_mode_keeps_its_full_tool_set(tmp_path):
    argv = _argv(tmp_path, "--act")

    assert "--tools" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
