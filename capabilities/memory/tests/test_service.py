"""The service, in project mode and in machine mode, started for real against
a store schema of each case's own and stopped when the case ends."""

from __future__ import annotations

import json
import os
import signal
import time

import pytest

from _cli import World, drop_schema, error, needs_store, new_schema, project_id

pytestmark = needs_store


@pytest.fixture
def world(tmp_path):
    schema = new_schema()
    made = World(tmp_path, schema)
    started: list = []
    made.started = started
    try:
        yield made
    finally:
        for cwd, machine in started:
            made.run(cwd, "service", "stop", *(["--machine"] if machine else []), timeout=60)
        for pidfile in made.state.rglob("daemon.pid"):
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        drop_schema(schema)


def start(world, cwd, machine=False, **extra):
    out = world.ok(cwd, "service", "start", *(["--machine"] if machine else []), **extra)
    world.started.append((cwd, machine))
    assert out["started"], out
    return out


def sidecar(root):
    return json.loads((root / "capabilities" / "memory" / "state" / "context.json").read_text())


def until(check, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.3)
    raise AssertionError("the condition did not hold in time")


def test_project_mode_renders_on_every_change_and_reloads(world):
    root = world.project("p")
    world.ok(root, "add", "first", "--title", "one")
    start(world, root)
    until(lambda: sidecar(root).get("rendered_by") == "the memory service")
    # A change the service did not make reaches the file through the store's
    # change versions: written straight into the store, as another machine would.
    from _cli import engine, setting
    E = engine()
    with E.Store(setting(world.schema)).open() as store:
        store.add(project_id(root), "two", "second", scope=None, pinned=False,
                  actor="elsewhere", node="another-node")
        last = store.last_event(project_id(root))
    until(lambda: sidecar(root)["last_event"] == last
          and sidecar(root)["rendered_by"] == "the memory service")

    view = world.ok(root, "service", "status")
    assert view["running"] and view["current"] and view["project"]["state"] == "served", json.dumps(view)
    settings = root / "capabilities" / "memory" / "service" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"version": 1, "window_days": 7}))
    assert world.ok(root, "service", "reload")["reloaded"] is True
    settings.write_text(json.dumps({"window_days": 0}))
    refused = world.run(root, "service", "reload")
    assert refused.returncode == 6 and error(refused)["code"] == "settings_invalid"
    settings.write_text(json.dumps({"version": 1, "window_days": 7}))
    doctor = world.ok(root, "service", "doctor")
    assert doctor["ok"] and doctor["process"]["state"] == "served"
    lines = world.ok(root, "service", "logs")["lines"]
    assert all("memory service [p]:" in line for line in lines)
    assert world.ok(root, "service", "stop")["stopped"] is True
    assert world.ok(root, "service", "status")["running"] is False


def test_service_activation_needs_an_explicit_project_enable(world):
    world.enable_globally()
    root = world.project("p", enabled=None)
    done = world.run(root, "service", "start")
    assert done.returncode == 4 and error(done)["code"] == "project_enable_required"
    done = world.run(root, "service", "join")
    assert done.returncode == 4


def test_machine_mode_serves_joined_projects_and_refuses_the_rest(world):
    a, b = world.project("a"), world.project("b")
    world.ok(a, "add", "in a", "--title", "a1")
    assert world.ok(a, "service", "join")["joined"] is True
    assert world.ok(b, "service", "join")["joined"] is True
    done = world.run(a, "service", "start")
    assert done.returncode == 4 and error(done)["code"] == "joined_to_machine_service"
    start(world, a, machine=True)
    until(lambda: (a / "capabilities" / "memory" / "state" / "context.json").exists()
          and sidecar(a)["rendered_by"] == "the memory service")
    # A project that stops enabling memory for itself is let go, the other served.
    (b / "capabilities" / "settings.json").write_text(
        json.dumps({"capabilities": {"memory": {"enabled": False}}}))

    def states():
        view = world.ok(a, "service", "status", "--machine")
        rows = {p["project"]: p for p in view["projects"]}
        if rows["a"]["state"] == "served" and rows["b"]["state"] == "refused":
            return view
        return None

    view = until(states, 40)
    assert view["current"] is True and view["list"]["joined"] == 2
    here = world.ok(a, "service", "status")
    assert here["mode"] == "machine" and here["project"]["state"] == "served"
    doctor = world.ok(a, "service", "doctor", "--machine")
    assert doctor["ok"] and doctor["process"]["stores"][0]["connected"]
    done = world.run(a, "service", "stop")
    assert done.returncode == 4
    world.ok(a, "add", "later", "--title", "a2", MEMORY_ACTOR="tester")
    until(lambda: sidecar(a)["last_event"] == world.ok(a, "status")["last_event"]
          and sidecar(a)["rendered_by"] == "the memory service")
    left = world.ok(a, "service", "leave", "--machine", "--project", project_id(b))
    assert left["left"] is True
    machine_log = world.ok(a, "service", "logs", "--machine", "--tail", "500")["lines"]
    assert any("[a]: served" in line for line in machine_log)
    assert any("[b]: no longer served" in line for line in machine_log)
    assert world.ok(a, "service", "stop", "--machine")["stopped"] is True


def test_the_service_grooms_a_window_that_no_longer_fits(world, tmp_path):
    root = world.project("p")
    settings = root / "capabilities" / "memory" / "service" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"version": 1, "block_chars": 2000, "entry_chars": 1000,
                                    "quiet_minutes": 0}))
    ids = [world.ok(root, "add", f"entry {i} " + "z" * 700, "--title", f"e{i}")["entry"]["id"]
           for i in range(4)]
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"merges": [{"sources": ids[:2], "title": "merged",
                                            "body": "the first two, merged", "reason": "x"}],
                                "drops": []}))
    start(world, root, FAKE_GROOM_PLAN=plan)
    until(lambda: world.ok(root, "status")["grooming"]["passes"] == 1, 60)
    titles = {e["title"] for e in world.ok(root, "list", "--json")["entries"]}
    assert "merged" in titles and "e0" not in titles
    log = world.ok(root, "log", "--json")["events"]
    groom = next(e for e in log if e["kind"] == "groom")
    assert groom["reason"].startswith("service:")
    assert groom["actor"].startswith("memory service on ")


def test_between_steps_the_service_holds_one_working_connection_and_no_listener(world):
    """The service follows the store's change versions on the process's one
    working connection, kept between steps, and holds no listening session."""
    import psycopg
    from _cli import DSN
    root = world.project("p")
    world.ok(root, "add", "first", "--title", "one")
    with psycopg.connect(DSN, autocommit=True) as admin:
        # What this test process holds itself is not the service's.
        before = {pid for (pid,) in admin.execute(
            "SELECT pid FROM pg_stat_activity WHERE datname = current_database()")}
    start(world, root)
    until(lambda: sidecar(root).get("rendered_by") == "the memory service")
    seen: dict = {}
    with psycopg.connect(DSN, autocommit=True) as admin:
        began = time.monotonic()
        while time.monotonic() - began < 6.0:
            now = time.monotonic()
            for pid, name in admin.execute(
                    "SELECT pid, application_name FROM pg_stat_activity "
                    "WHERE application_name LIKE 'memory%%' AND datname = current_database()"):
                if pid in before:
                    continue
                first, _last, _name = seen.get(pid, (now, now, name))
                seen[pid] = (first, now, name)
            time.sleep(0.2)
    held = sorted(name for first, last, name in seen.values() if last - first >= 4.0)
    assert held == ["memory"], seen
