import json

from register import Register
from watermark import Watermark
import daemon


def _reg(tmp_path):
    return Register(tmp_path / "register.json")


def _wm(tmp_path):
    return Watermark(tmp_path / "watermarks.json")


def _collectors():
    posts, jobs = [], []
    def post_message(channel, text, thread_ts):
        posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
    def submit_job(conv, job):
        jobs.append({"conv": conv, "job": job})
    return posts, jobs, post_message, submit_job


def _im(user="U1", channel="D1", ts="100.1", text="hi"):
    return {"type": "message", "channel_type": "im", "user": user,
            "channel": channel, "ts": ts, "text": text}


def _args(tmp_path, settings, post_message, submit_job, owner_dm=None):
    return dict(settings=settings, register=_reg(tmp_path), watermark=_wm(tmp_path),
                inbox_path=tmp_path / "inbox.jsonl", post_message=post_message,
                submit_job=submit_job, owner_dm=owner_dm)


def test_ignored_event(tmp_path):
    posts, jobs, post, submit = _collectors()
    out = daemon.process_event({"type": "reaction_added"},
                               **_args(tmp_path, {}, post, submit))
    assert out["action"] == "ignore"
    assert "reason" not in out          # genuine noise stays silent (no log line)
    assert posts == [] and jobs == []


def test_refused_channel_mention_reports_not_allowed(tmp_path):
    posts, jobs, post, submit = _collectors()
    s = {"allowed_channels": {}, "default_channel_policy": "allowed_only"}
    evt = {"type": "app_mention", "user": "U1", "channel": "C9",
           "ts": "5.0", "text": "<@B1> hi"}
    out = daemon.process_event(evt, **_args(tmp_path, s, post, submit))
    assert out["action"] == "ignore"
    assert out["reason"] == "not_allowed"
    assert out["kind"] == "channel"
    assert out["channel"] == "C9"       # operator can grab the id from the log
    assert out["user"] == "U1"
    assert out["ts"] == "5.0"
    assert out["text"] == "<@B1> hi"    # snippet logged so you see what was said


def test_refused_dm_reports_not_allowed(tmp_path):
    posts, jobs, post, submit = _collectors()
    s = {"direct_messages": {"mode": "allowed_users"}, "allowed_users": {}}
    out = daemon.process_event(_im(user="U1", channel="D1"),
                               **_args(tmp_path, s, post, submit))
    assert out["action"] == "ignore"
    assert out["reason"] == "not_allowed"
    assert out["channel"] == "D1"


def test_answer_enqueues_and_does_not_post(tmp_path):
    posts, jobs, post, submit = _collectors()
    s = {"direct_messages": {"mode": "open"}, "auto_answer": {"users": ["U1"]}}
    out = daemon.process_event(_im(user="U1", text="hello"),
                               **_args(tmp_path, s, post, submit))
    assert out["action"] == "answer"
    assert posts == []
    assert len(jobs) == 1
    assert jobs[0]["conv"] == "D1"
    assert jobs[0]["job"]["role"] == "default"
    assert out["text"] == "hello"


def test_relay_writes_inbox_and_notifies_owner(tmp_path):
    posts, jobs, post, submit = _collectors()
    s = {"direct_messages": {"mode": "open"}, "auto_answer": {"users": []}}
    args = _args(tmp_path, s, post, submit, owner_dm="U0OWNER")
    inbox = args["inbox_path"]
    out = daemon.process_event(_im(user="U1", text="ping"), **args)
    assert out["action"] == "relay"
    assert jobs == []
    assert posts[0]["channel"] == "U0OWNER"
    assert out["text"] == "ping"
    assert json.loads(inbox.read_text().splitlines()[0])["text"] == "ping"


def test_refused_event(tmp_path):
    posts, jobs, post, submit = _collectors()
    s = {"direct_messages": {"mode": "allowed_users"}, "allowed_users": {}}
    out = daemon.process_event(_im(user="U1"), **_args(tmp_path, s, post, submit))
    assert out["action"] == "ignore"


def test_duplicate_enqueues_once(tmp_path):
    posts, jobs, post, submit = _collectors()
    s = {"direct_messages": {"mode": "open"}, "auto_answer": {"users": ["U1"]}}
    args = _args(tmp_path, s, post, submit)
    first = daemon.process_event(_im(ts="100.1"), **args)
    second = daemon.process_event(_im(ts="100.1"), **args)
    assert first["action"] == "answer"
    assert second["action"] == "duplicate"
    assert len(jobs) == 1


def test_control_stop_allowed_for_supervisor(tmp_path):
    posts, jobs, post, submit = _collectors()
    s = {"direct_messages": {"mode": "open"},
         "allowed_users": {"U1": {"name": "S", "role": "supervisor"}},
         "control": {"roles": {"supervisor": {"commands": ["status", "stop"]}}}}
    out = daemon.process_event(_im(user="U1", text="stop"),
                               **_args(tmp_path, s, post, submit))
    assert out["action"] == "control"
    assert out["command"] == "stop"
    assert jobs == []


def test_control_denied_for_default_role(tmp_path):
    posts, jobs, post, submit = _collectors()
    s = {"direct_messages": {"mode": "open"},
         "allowed_users": {"U1": {"name": "A", "role": "default"}},
         "control": {"roles": {"supervisor": {"commands": ["stop"]}}}}
    out = daemon.process_event(_im(user="U1", text="stop"),
                               **_args(tmp_path, s, post, submit))
    assert out["action"] == "control_denied"


def test_watermark_advances_on_accept(tmp_path):
    posts, jobs, post, submit = _collectors()
    s = {"direct_messages": {"mode": "open"}, "auto_answer": {"users": ["U1"]}}
    args = _args(tmp_path, s, post, submit)
    daemon.process_event(_im(ts="123.4"), **args)
    assert args["watermark"].get("D1") == "123.4"
