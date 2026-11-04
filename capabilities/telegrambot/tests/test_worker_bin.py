"""The worker wrapper a turn finds first on its PATH: it fixes the chat, topic,
connection and requester, writes progress and messages to the turn's outbox,
and refuses what would cross the turn's scope. The real CLI is a stand-in that
prints what it was given."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from _cli import SERVICE_DIR

WRAPPER = SERVICE_DIR / "worker-bin" / "telegrambot"


@pytest.fixture()
def turn(tmp_path):
    echo = tmp_path / "real-cli"
    echo.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                    "print(json.dumps({'argv': sys.argv[1:], 'project': "
                    "os.environ.get('CLAUDE_PROJECT_DIR')}))\n")
    echo.chmod(0o755)
    env = {"PATH": os.environ.get("PATH", ""), "TELEGRAMBOT_TURN": "1",
           "TELEGRAMBOT_AUTHORIZED_CONNECTION": "helper",
           "TELEGRAMBOT_AUTHORIZED_CHAT_ID": "-1009", "TELEGRAMBOT_AUTHORIZED_TOPIC_ID": "5",
           "TELEGRAMBOT_AUTHORIZED_ORIGIN_MESSAGE_ID": "77",
           "TELEGRAMBOT_AUTHORIZED_REQUESTER": "42",
           "TELEGRAMBOT_AUTHORIZED_JOB_PROFILE": "telegrambot-job-claude",
           "TELEGRAMBOT_PROGRESS_OUTBOX": str(tmp_path / "outbox.jsonl"),
           "TELEGRAMBOT_SERVICE_PROJECT": str(tmp_path / "project"),
           "TELEGRAMBOT_REAL_CLI": str(echo)}
    return env, tmp_path


def run(env, *args, stdin=None):
    found = subprocess.run([sys.executable, str(WRAPPER), *args], env=env, input=stdin,
                           capture_output=True, text=True)
    out = json.loads(found.stdout) if found.stdout.strip() else None
    err = json.loads(found.stderr.strip().splitlines()[-1]) if found.stderr.strip() else None
    return found.returncode, out, err


def test_progress_and_send_go_to_the_outbox(turn):
    env, tmp = turn
    assert run(env, "progress", "reading the file")[0] == 0
    assert run(env, "send", "current", "-", stdin="the link: https://example.com")[0] == 0
    lines = [json.loads(l) for l in (tmp / "outbox.jsonl").read_text().splitlines()]
    assert [(l["kind"], l["text"]) for l in lines] == [
        ("progress", "reading the file"), ("message", "the link: https://example.com")]


def test_the_scope_cannot_be_crossed(turn):
    env, _tmp = turn
    code, _out, err = run(env, "send", "-1001", "elsewhere")
    assert code == 4 and err["error"]["code"] == "chat_scope_denied"
    code, _out, err = run(env, "history", "--connection", "other")
    assert code == 4 and err["error"]["code"] == "worker_scope_denied"
    code, _out, err = run(env, "jobs", "register", "x", "--chat", "1")
    assert code == 4 and err["error"]["code"] == "worker_scope_denied"
    del env["TELEGRAMBOT_PROGRESS_OUTBOX"]
    code, _out, err = run(env, "progress", "hello")
    assert code == 4 and err["error"]["code"] == "worker_progress_unavailable"


def test_reads_and_jobs_are_pinned_to_the_turn(turn):
    env, tmp = turn
    code, out, _err = run(env, "history", "--before", "70", "--limit", "10")
    assert code == 0 and out["argv"] == ["--connection", "helper", "history", "-1009", "--topic",
                                         "5", "--before", "70", "--limit", "10"]
    assert out["project"] == str(tmp / "project")
    code, out, _err = run(env, "jobs", "register", "make the report")
    assert out["argv"][:4] == ["--connection", "helper", "jobs", "register"]
    assert out["argv"][4:] == ["--chat", "-1009", "--actor", "42", "--topic", "5",
                               "--requested-by", "42", "--origin-message-id", "77",
                               "--profile", "telegrambot-job-claude", "make the report"]
    code, out, _err = run(env, "jobs", "help")
    assert out["argv"] == ["jobs", "help"]
