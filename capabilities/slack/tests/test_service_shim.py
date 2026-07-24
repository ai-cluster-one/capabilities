import json
import os
import stat
import subprocess
from pathlib import Path

SHIM = Path(__file__).resolve().parent.parent / "service" / "worker-bin" / "slack"


def _run(args, env):
    return subprocess.run([str(SHIM), *args], capture_output=True, text=True, env=env)


def test_post_appends_to_outbox(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    env = {**os.environ, "SLACK_WORKER_OUTBOX": str(outbox),
           "SLACK_WORKER_CONVERSATION": "D1"}
    r = _run(["post", "D1", "hello", "world"], env)
    assert r.returncode == 0
    line = json.loads(outbox.read_text().splitlines()[0])
    assert line["text"] == "hello world"


def test_post_to_foreign_id_is_rejected(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    env = {**os.environ, "SLACK_WORKER_OUTBOX": str(outbox),
           "SLACK_WORKER_CONVERSATION": "D1"}
    r = _run(["post", "C999", "hi"], env)
    assert r.returncode == 4
    assert not outbox.exists()


def test_read_passes_through_to_real_slack(tmp_path):
    fake = tmp_path / "real-slack"
    fake.write_text("#!/bin/sh\necho REAL:\"$@\"\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "SLACK_WORKER_OUTBOX": str(tmp_path / "o.jsonl"),
           "SLACK_WORKER_CONVERSATION": "D1", "SLACK_REAL_SLACK": str(fake)}
    r = _run(["read", "D1"], env)
    assert r.returncode == 0
    assert "REAL:read D1" in r.stdout


def test_positional_skips_connection_and_thread_flags(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    env = {**os.environ, "SLACK_WORKER_OUTBOX": str(outbox),
           "SLACK_WORKER_CONVERSATION": "D1"}
    r = _run(["post", "--connection", "ionwater", "--thread", "123.4",
              "D1", "hello", "world"], env)
    assert r.returncode == 0
    line = json.loads(outbox.read_text().splitlines()[0])
    assert line["text"] == "hello world"   # flag values skipped, not treated as text


def test_foreign_id_rejected_takes_precedence_over_missing_outbox(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "SLACK_WORKER_OUTBOX"}
    env["SLACK_WORKER_CONVERSATION"] = "D1"   # no outbox configured
    r = _run(["post", "C999", "hi"], env)
    assert r.returncode == 4                  # scope check (4) runs before missing-outbox (3)
