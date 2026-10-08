from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "deployment"
FIXTURE = Path(__file__).parent / "fixtures" / "node" / "provider.json"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def node(tmp_path, monkeypatch):
    root = tmp_path / "desk"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "doctrine.md").write_text("base\n")
    (root / "memory").mkdir()
    (root / "memory" / "day.md").write_text("base\n")
    (root / ".gitattributes").write_text('"memory/**" merge=union\n')
    (root / "capabilities").mkdir()
    (root / "capabilities" / "project.json").write_text(json.dumps(
        {"schema": "capabilities.project.v1", "id": "prj_fixture", "slug": "fixture-body"}))
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    remote = tmp_path / "node.git"
    subprocess.run(["git", "clone", "--bare", str(root), str(remote)], check=True, capture_output=True)
    git(root, "remote", "add", "node-test", str(remote))
    body = tmp_path / "body"
    subprocess.run(["git", "clone", str(remote), str(body)], check=True, capture_output=True)
    git(body, "config", "user.name", "node")
    git(body, "config", "user.email", "node@example.invalid")
    binpath = tmp_path / "bin"
    binpath.mkdir()
    trace = tmp_path / "trace.jsonl"
    fake = binpath / "coolify"
    fake.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
if '--connection' in args:
 i=args.index('--connection')
 if (args[0] == 'ids' and i == 0) or (args[0] != 'ids' and i != 0): sys.exit(2)
 del args[i:i+2]
if args == ['help']:
 print('coolify wait <uuid>\\ncoolify app rollback <uuid> --to <commit>'); sys.exit()
with open(os.environ['NODE_TRACE'], 'a') as f: f.write(json.dumps({'argv':args,'stdin':sys.stdin.read()})+'\\n')
fixtures=json.loads(Path(os.environ['NODE_FIXTURE']).read_text())
if args[:2] == ['ids','set'] or args[:2] == ['env','bulk']: print('{}'); sys.exit()
if args[0] == 'wait' and 'deployment-fixture' in args and os.environ.get('NODE_WAIT_FAIL'):
 print(json.dumps({'error':{'code':'deployment_failed','message':'fixture build failed'}}),file=sys.stderr); sys.exit(5)
if args[:3] == ['ids','get','node_app'] and os.environ.get('NODE_NEW_APP'):
 print(json.dumps({'error':{'code':'not_found','message':'fixture absent'}}),file=sys.stderr); sys.exit(3)
key=' '.join(args)
if key not in fixtures: key=' '.join(args[:2])
if key not in fixtures: key=args[0]
if args[:2] == ['ids','get']: print(fixtures[key]); sys.exit()
print(json.dumps(fixtures[key]))
''')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(binpath) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("NODE_TRACE", str(trace))
    monkeypatch.setenv("NODE_FIXTURE", str(FIXTURE))
    loader = importlib.machinery.SourceFileLoader("node_deployment", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    monkeypatch.setattr(m, "_root", lambda: root)
    target = {"name": "test", "provider": "coolify", "connection": "fixture", "server": "node_server",
              "project": "node_project", "environment": "production", "deploy_key": "node_key",
              "resource": {"identifier_label": "node_app"}}
    runtime = m._runtime_template("agent-box-checkout", "fixture-body")
    monkeypatch.setattr(m, "_resolve_target", lambda r, n: (
        target, root / "deployment" / "targets" / "test" / "target.json"))
    monkeypatch.setattr(m, "_load_runtime", lambda r: runtime)
    entry = {"base_url": "https://coolify.example.invalid", "ssh": {"key_path": str(tmp_path / "mac-key")}}
    monkeypatch.setattr(m, "_records", lambda: SimpleNamespace(connections=lambda *a, **kw: {
        "fixture": {"enabled": True, "allow_write": True, "value": entry}}))
    monkeypatch.setattr(m, "_sync", lambda *a, **kw: {"ok": True})
    key = tmp_path / "private-node-key"
    key.write_text("fixture-private-material")
    monkeypatch.setattr(m, "_node_prepare_repo", lambda *a: ("git@192.0.2.10:/srv/git/body.git", key))
    monkeypatch.setattr(m, "find_store_setting", lambda: ({"host": "store.example.invalid", "port": 5432,
        "database": "fixture", "user": "fixture", "sslmode": "require", "db_schema": "fixture_schema",
        "password": "fixture-store-secret"}, {"read": "family", "db_schema_named": True}))
    calls = []
    def ssh(r, host, script, **kw):
        calls.append((script, kw.get("stdin")))
        if script.startswith("cat /etc/ssh/"): return "ssh-ed25519 fixture-public-key"
        if script.startswith("docker ps"): return "aabbccddeeff"
        if 'test -s' in script: return "logged_out"
        if 'supervisorctl' in script: return "body-sync RUNNING pid 1\n"
        if 'store show' in script: return json.dumps({"configured": True})
        if 'store doctor' in script: return json.dumps({"checks": {"tls": {"ok": True}, "plain_text_refused": {"ok": True}}})
        return ""
    monkeypatch.setattr(m, "_node_ssh", ssh)
    monkeypatch.setattr(m, "_launchd_status", lambda label: {"loaded": False})
    args = SimpleNamespace(target="test", to=None, build=False, follow=False, json=True,
                           harness="claude", print_command=True)
    return SimpleNamespace(m=m, root=root, body=body, remote=remote, target=target, runtime=runtime,
                           key=key, args=args, trace=trace, ssh=calls, entry=entry, tmp=tmp_path)


def rows(node):
    return [json.loads(line) for line in node.trace.read_text().splitlines()]


def commit(root, path, text):
    (root / path).write_text(text)
    git(root, "add", path)
    git(root, "commit", "-qm", "fixture change")


def test_deploy_fixture_sets_env_off_argv_and_waits(node, capsys):
    node.m.cmd_deploy(node.args)
    result = json.loads(capsys.readouterr().out)
    assert result["release"]["deployment"] == "deployment-fixture"
    requests = rows(node)
    assert [r["argv"][0] for r in requests][-3:] == ["env", "deploy", "wait"]
    env = next(r["stdin"] for r in requests if r["argv"][:2] == ["env", "bulk"])
    assert "AGENT_STORE_PASSWORD=fixture-store-secret" in env
    assert "AGENT_STORE_DB_SCHEMA=fixture_schema" in env
    assert "AGENT_REPO_URL=git@host.docker.internal:/srv/git/body.git" in env
    assert "fixture-store-secret" not in json.dumps([r["argv"] for r in requests]) + json.dumps(result)
    assert "fixture-private-material" not in json.dumps(result)


def test_deploy_carries_no_schema_the_setting_does_not_name(node, monkeypatch, capsys):
    monkeypatch.setattr(node.m, "find_store_setting", lambda: ({"host": "store.example.invalid",
        "port": 5432, "database": "fixture", "user": "fixture", "sslmode": "require",
        "db_schema": "agentkit", "password": "fixture-store-secret"},
        {"read": "legacy", "db_schema_named": False}))
    node.m.cmd_deploy(node.args)
    capsys.readouterr()
    env = next(r["stdin"] for r in rows(node) if r["argv"][:2] == ["env", "bulk"])
    assert "AGENT_STORE_HOST=store.example.invalid" in env
    assert "AGENT_STORE_DB_SCHEMA" not in env


def test_first_deploy_records_app_and_disables_auto_deploy(node, monkeypatch, capsys):
    monkeypatch.setenv("NODE_NEW_APP", "1")
    node.m.cmd_deploy(node.args)
    capsys.readouterr()
    calls = rows(node)
    create = next(r["argv"] for r in calls if r["argv"][:2] == ["app", "create"])
    assert ["app", "update", "application-fixture", "--no-auto-deploy-enabled"] in [r["argv"] for r in calls]
    assert create[create.index("--health-check-host") + 1] == "127.0.0.1"
    assert create[create.index("--private-deploy-key") + 1] == "key-fixture"
    assert ["ids", "set", "node_app", "application-fixture"] in [r["argv"] for r in calls]


def test_failed_deploy_rolls_back_and_keeps_release(node, monkeypatch):
    old = {"commit": git(node.root, "rev-parse", "HEAD"), "deployment": "previous"}
    node.m._node_save(node.root, node.target, {"release": old})
    monkeypatch.setenv("NODE_WAIT_FAIL", "1")
    with pytest.raises(node.m.NodeFailure, match="rollback=healthy"):
        node.m.cmd_deploy(node.args)
    assert ["app", "rollback", "application-fixture", "--to", old["commit"]] in [r["argv"] for r in rows(node)]
    assert node.m._node_state(node.root, node.target)["release"] == old


def test_rollback_waits_and_tracks_release(node, capsys):
    node.m._node_save(node.root, node.target, {"release": {"commit": "current", "rollback_to": ["previous"]}})
    node.m.cmd_rollback(node.args)
    assert json.loads(capsys.readouterr().out)["release"]["commit"] == "previous"
    assert rows(node)[-1]["argv"][0] == "wait"


@pytest.mark.parametrize("build", [False, True])
def test_logs_runtime_and_build(node, capsys, build):
    node.m._node_save(node.root, node.target, {"release": {"deployment": "deployment-fixture"}})
    node.args.build = build
    node.m.cmd_logs(node.args)
    assert "fixture" in capsys.readouterr().out
    assert rows(node)[-1]["argv"][0] == ("deployments" if build else "logs")


@pytest.mark.parametrize("harness,expected", [("claude", "claude auth login"), ("codex", "codex login --device-auth")])
def test_login_prepares_owner_terminal_command(node, capsys, harness, expected):
    node.args.harness = harness
    node.m.cmd_node_login(node.args)
    command = json.loads(capsys.readouterr().out)["command"]
    assert "ssh -t" in command and "docker exec -it aabbccddeeff" in command and expected in command
    assert all("label=coolify.applicationId=42" in script for script, _ in node.ssh if script.startswith("docker ps"))


def test_status_reports_actual_fixture_facts(node, capsys):
    node.m.cmd_node_status(node.args)
    data = json.loads(capsys.readouterr().out)
    assert data["server"]["address"] == "192.0.2.10"
    assert data["application"]["status"] == "running:healthy"
    assert data["harness"] == {"claude": "logged_out", "codex": "logged_out"}
    assert data["store"] == {"configured": True, "reachable": True, "tls_enforced": True}
    assert data["services"][0]["placement"] == "node"
    assert data["errors"] == []


def test_grant_refuses_before_any_ssh_or_provider_write(node, monkeypatch):
    monkeypatch.setattr(node.m, "_records", lambda: SimpleNamespace(connections=lambda *a, **kw: {}))
    with pytest.raises(node.m.NodeFailure) as failure:
        node.m.cmd_deploy(node.args)
    assert failure.value.code == "connection_not_granted"
    assert failure.value.exit_code == 4
    assert not node.ssh and not node.trace.exists()


def test_drift_refuses_before_mutation(node, monkeypatch):
    monkeypatch.setattr(node.m, "_sync", lambda *a, **kw: {"ok": False})
    with pytest.raises(node.m.NodeFailure) as failure:
        node.m.cmd_deploy(node.args)
    assert failure.value.code == "deployment_drift"
    assert not node.ssh
    assert all(r["argv"][0] in ("ids", "servers") for r in rows(node))


def test_real_git_sync_both_directions_and_memory_union(node, capsys):
    # Mac edits doctrine while disconnected; node writes memory on the same day.
    commit(node.root, "doctrine.md", "desk doctrine\n")
    commit(node.root, "memory/day.md", "base\ndesk append\n")
    commit(node.body, "memory/day.md", "base\nnode append\n")
    git(node.body, "push", "-q", "origin", "main")
    node.m.cmd_node_sync(node.args)
    assert json.loads(capsys.readouterr().out)["sync"]["state"] == "level"
    git(node.body, "pull", "--rebase", "-q", "origin", "main")
    assert (node.body / "doctrine.md").read_text() == "desk doctrine\n"
    for body in (node.root, node.body):
        assert set((body / "memory/day.md").read_text().splitlines()) == {"base", "desk append", "node append"}
    assert git(node.root, "rev-parse", "HEAD") == git(node.body, "rev-parse", "HEAD")


def test_conflict_aborts_and_reports_paths_preserving_both_commits(node):
    commit(node.root, "doctrine.md", "desk\n")
    before = git(node.root, "rev-parse", "HEAD")
    commit(node.body, "doctrine.md", "node\n")
    git(node.body, "push", "-q", "origin", "main")
    with pytest.raises(node.m.NodeFailure) as failure:
        node.m._node_sync(node.root, node.target)
    assert failure.value.code == "diverged"
    assert git(node.root, "rev-parse", "HEAD") == before
    assert not git(node.root, "status", "--porcelain")
    assert node.m._node_sync_status(node.root, node.target)["conflicts"] == ["doctrine.md"]
    assert node.m._node_sync_status(node.root, node.target)["state"] == "diverged"


def test_optional_mirror_pushes_only_after_success(node):
    mirror = node.tmp / "mirror.git"
    subprocess.run(["git", "init", "--bare", str(mirror)], check=True, capture_output=True)
    git(node.root, "remote", "add", "mirror", str(mirror))
    node.target["mirror"] = "mirror"
    commit(node.root, "doctrine.md", "new\n")
    node.m._node_sync(node.root, node.target)
    assert git(mirror, "rev-parse", "main") == git(node.root, "rev-parse", "HEAD")


def test_memory_root_is_asked_of_contextkit_and_existing_attrs_preserved(node, monkeypatch):
    (node.root / ".contextkit").mkdir()
    (node.root / ".contextkit/config.toml").write_text("")
    memory = node.root / "body notes" / "memory"
    memory.mkdir(parents=True)
    calls = []
    def run(argv, root, **kw):
        calls.append(argv)
        return SimpleNamespace(stdout=str(memory) + "\n")
    monkeypatch.setattr(node.m, "_node_run", run)
    path, text = node.m._memory_attributes(node.root)
    assert calls == [["contextkit", "path", "memory"]]
    assert path.name == ".gitattributes" and '"body notes/memory/**" merge=union' in text
    assert '"memory/**" merge=union' in text


def test_memory_path_outside_body_refused(node, monkeypatch):
    (node.root / ".contextkit").mkdir()
    (node.root / ".contextkit/config.toml").write_text("")
    monkeypatch.setattr(node.m, "_node_run", lambda *a, **kw: SimpleNamespace(stdout=str(node.tmp)))
    with pytest.raises(node.m.NodeFailure, match="inside the body"):
        node.m._memory_attributes(node.root)


def test_entrypoint_pins_node_host_and_sets_store_without_password_argv(node):
    script = node.m._entrypoint_template("fixture", checkout=True)
    assert 'AGENT_GIT_KNOWN_HOSTS_B64' in script
    assert '--password-stdin' in script
    assert 'printf \'%s\' "$AGENT_STORE_PASSWORD"' in script
    assert '--schema "$AGENT_STORE_DB_SCHEMA"' in script
    assert '--password "$' not in script
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0


def test_generated_node_loop_keeps_offline_commits_and_unions_appends(node):
    program = node.tmp / "body-sync.sh"
    program.write_text(node.m._body_sync_template("fixture", str(node.body)))
    program.chmod(0o755)
    # Snapshot first: an unreachable hub does not cost the node's own changes.
    git(node.body, "remote", "set-url", "origin", str(node.tmp / "absent.git"))
    (node.body / "memory/day.md").write_text("base\noffline append\n")
    env = {**os.environ, "AGENT_BODY_SYNC_QUIET": "0"}
    before = git(node.body, "rev-parse", "HEAD")
    p = subprocess.run([str(program), "--once"], capture_output=True, text=True, env=env)
    assert p.returncode != 0
    assert git(node.body, "rev-parse", "HEAD") != before
    git(node.body, "remote", "set-url", "origin", str(node.remote))
    commit(node.root, "memory/day.md", "base\ndesk append\n")
    git(node.root, "push", "-q", "node-test", "main")
    p = subprocess.run([str(program), "--once"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    node.m._node_sync(node.root, node.target)
    assert set((node.root / "memory/day.md").read_text().splitlines()) == {"base", "offline append", "desk append"}


def test_placement_filters_only_the_selected_substrate(node, monkeypatch):
    descriptor = {"summary": "fixture", "deploy": {"default_policy": "auto", "mounts": []}}
    monkeypatch.setattr(node.m, "_discover_service_descriptors", lambda root: ({"fixture": descriptor}, []))
    monkeypatch.setattr(node.m, "_enabled_capabilities", lambda root: ["fixture"])
    node.runtime["service_policy"] = {"default_mode": "enabled", "capabilities": {"fixture": "enabled"}, "placement": {"fixture": "local"}}
    active, embedded, errors, _ = node.m._active_descriptors(node.root, node.runtime)
    assert not active and not embedded and not errors
    node.runtime["profile"] = "host-agents"
    assert "fixture" in node.m._active_descriptors(node.root, node.runtime)[0]
    node.runtime["service_policy"]["placement"]["fixture"] = "both"
    for profile in ("host-agents", "agent-box-checkout"):
        node.runtime["profile"] = profile
        assert "fixture" in node.m._active_descriptors(node.root, node.runtime)[0]


def test_host_interval_service_and_memory_line_are_compiled(node, monkeypatch):
    node.runtime["profile"] = "host-agents"
    (node.root / "deployment/targets").mkdir(parents=True)
    (node.root / "deployment/targets/test.json").write_text(json.dumps(node.target))
    texts, findings = node.m._host_artifact_texts(node.root, node.runtime, {})
    import plistlib
    job = next(plistlib.loads(text.encode()) for path, text in texts.items() if path.name.endswith('.node-sync-test.plist'))
    assert job["StartInterval"] == 60 and job["RunAtLoad"]
    launcher = texts[Path(job["ProgramArguments"][0])]
    assert "deployment node sync --target test" in launcher
    assert job["WorkingDirectory"] == str(node.root)


def test_wait_compatibility_poll_checks_release_and_resource(node, monkeypatch):
    original = node.m._node_run
    def run(argv, root, **kw):
        if argv == ["coolify", "help"]:
            return SimpleNamespace(stdout="coolify app rollback")
        return original(argv, root, **kw)
    monkeypatch.setattr(node.m, "_node_run", run)
    result = node.m._node_wait(node.root, node.target, "application-fixture", "deployment-fixture", timeout=2)
    assert result == {"deployment": "finished", "resource": "running:healthy"}
    assert [r["argv"][0] for r in rows(node)] == ["deployments", "applications"]


def test_prepared_repo_uses_one_node_key_and_public_stdin(node, monkeypatch):
    # Exercise the actual preparation with the recorded SSH host surface.
    loader = importlib.machinery.SourceFileLoader("node_prepare", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    original = importlib.util.module_from_spec(spec)
    loader.exec_module(original)
    monkeypatch.setattr(original, "_node_ssh", node.m._node_ssh)
    mac_key = node.tmp / "mac-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(mac_key)], check=True)
    git(node.root, "remote", "remove", "node-test")
    host = {"address": "192.0.2.10", "key_path": str(mac_key)}
    first = original._node_prepare_repo(node.root, node.runtime, node.target, host, "main")
    second = original._node_prepare_repo(node.root, node.runtime, node.target, host, "main")
    assert first == second
    assert first[0].startswith("git@192.0.2.10:/srv/git/")
    assert first[1].stat().st_mode & 0o777 == 0o600
    assert node.ssh[0][1].count("ssh-ed25519") == 2
    assert "uploadpack.allowReachableSHA1InWant true" in node.ssh[0][0]
    assert "PRIVATE KEY" not in str(node.ssh)


def test_read_only_switch_fences_new_mutating_verbs(node):
    (node.root / "capabilities").mkdir(exist_ok=True)
    (node.root / "capabilities/settings.json").write_text(json.dumps({"capabilities": {"deployment": {"enabled": True}}}))
    env = {**os.environ, "CAPABILITIES_READ_ONLY": "1"}
    for args in (("deploy",), ("rollback",), ("node", "sync"), ("node", "login", "claude")):
        p = subprocess.run([str(SCRIPT), *args, "--target", "test"], cwd=node.root,
                           capture_output=True, text=True, env=env)
        assert p.returncode == 4, p.stdout + p.stderr
        assert "read_only" in p.stderr
    assert not node.trace.exists()


def test_sync_lock_refuses_overlapping_writer(node):
    import fcntl
    path = node.m._node_state_path(node.root, node.target).with_suffix('.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(node.m.NodeFailure) as failure:
            node.m._node_sync(node.root, node.target)
        assert failure.value.code == 'node_busy'


def test_first_deploy_uses_the_existing_production_environment_and_the_bundle(node, monkeypatch, capsys):
    monkeypatch.setenv("NODE_NEW_APP", "1")
    node.m.cmd_deploy(node.args)
    capsys.readouterr()
    calls = [r["argv"] for r in rows(node)]
    create = next(argv for argv in calls if argv[:2] == ["app", "create"])
    assert create[create.index("--environment") + 1] == "production"
    # Coolify runs Compose from the base directory, so it is the bundle's own
    # folder and the Compose file is named relative to it.
    assert create[create.index("--base-directory") + 1] == "/deployment/targets/test"
    assert create[create.index("--docker-compose-location") + 1] == "/docker-compose.yaml"
    assert not any("environment" in " ".join(argv) and argv[0] != "app" for argv in calls)


def test_deploy_creates_no_environment_of_its_own(node, monkeypatch):
    monkeypatch.setenv("NODE_NEW_APP", "1")
    node.target["environment"] = "staging"
    from urllib import request
    monkeypatch.setattr(request, "urlopen", lambda *a, **kw: pytest.fail("deploy called the API itself"))
    with pytest.raises(node.m.NodeFailure) as failure:
        node.m.cmd_deploy(node.args)
    assert failure.value.code == "environment_missing"
    assert failure.value.exit_code == 3
    calls = [r["argv"] for r in rows(node)]
    assert not any(argv[:2] == ["app", "create"] for argv in calls)
    assert not hasattr(node.m, "_node_create_environment")


def test_an_absent_environment_field_means_production(node, monkeypatch, capsys):
    monkeypatch.setenv("NODE_NEW_APP", "1")
    del node.target["environment"]
    node.m.cmd_deploy(node.args)
    capsys.readouterr()
    create = next(r["argv"] for r in rows(node) if r["argv"][:2] == ["app", "create"])
    assert create[create.index("--environment") + 1] == "production"


def test_every_deploy_points_an_existing_app_at_the_bundle(node, capsys):
    node.m.cmd_deploy(node.args)
    capsys.readouterr()
    calls = [r["argv"] for r in rows(node)]
    update = ["app", "update", "application-fixture", "--name", "fixture-body-test",
              "--base-directory", "/deployment/targets/test",
              "--docker-compose-location", "/docker-compose.yaml"]
    assert update in calls
    assert calls.index(update) < next(i for i, argv in enumerate(calls) if argv[0] == "deploy")


def _created_name(node):
    create = next(r["argv"] for r in rows(node) if r["argv"][:2] == ["app", "create"])
    return create[create.index("--name") + 1]


def test_the_app_is_named_after_the_project_not_the_checkout_folder(node, monkeypatch, capsys):
    monkeypatch.setenv("NODE_NEW_APP", "1")
    node.m.cmd_deploy(node.args)
    capsys.readouterr()
    # The checkout folder is "desk"; the recorded project slug names the app.
    assert node.root.name == "desk"
    assert _created_name(node) == "fixture-body-test"


@pytest.mark.parametrize("source,expected", [("config", "fixture-agent-test"),
                                             ("folder", "fixture-body-test")])
def test_a_contextkit_name_set_in_config_names_the_app(node, monkeypatch, capsys, source, expected):
    monkeypatch.setenv("NODE_NEW_APP", "1")
    monkeypatch.setattr(node.m, "_has_contextkit", lambda root: True)
    real = node.m._node_run
    def run(argv, root, **kw):
        if argv[:3] == ["contextkit", "identity", "show"]:
            name = "Fixture Agent" if source == "config" else root.name
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"name": name, "name_source": source}), "")
        if argv[:2] == ["contextkit", "build"] or argv[:3] == ["contextkit", "path", "memory"]:
            return subprocess.CompletedProcess(argv, 0, str(root / "memory"), "")
        return real(argv, root, **kw)
    monkeypatch.setattr(node.m, "_node_run", run)
    node.m.cmd_deploy(node.args)
    capsys.readouterr()
    # A name ContextKit only derived from the folder is the folder, so the
    # recorded slug wins over it.
    assert _created_name(node) == expected


def test_a_root_layout_runtime_keeps_its_compose_file_at_the_root(node, capsys):
    node.runtime["compose_file"] = "docker-compose.yaml"
    node.runtime["compiler"]["artifacts"] = {"dockerfile": "Dockerfile", "entrypoint": "entrypoint.sh",
                                             "env_example": ".env.example", "dockerignore": ".dockerignore"}
    node.m.cmd_deploy(node.args)
    capsys.readouterr()
    assert ["app", "update", "application-fixture", "--name", "fixture-body-test",
            "--base-directory", "/", "--docker-compose-location",
            "/docker-compose.yaml"] in [r["argv"] for r in rows(node)]


def test_target_identity_does_not_collide_after_slugging(node):
    first, second = {"name": "prod_test"}, {"name": "prod-test"}
    assert node.m._node_remote(first) != node.m._node_remote(second)
    assert node.m._node_state_path(node.root, first) != node.m._node_state_path(node.root, second)


@pytest.mark.parametrize("embedded", [False, True])
def test_descriptor_env_resolves_machine_tier_before_process(node, monkeypatch, embedded):
    config = node.tmp / "config"
    credentials = config / "fixture" / "credentials.env"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("FIXTURE_SECRET='machine|secret'\n")
    monkeypatch.setattr(node.m, "_CONFIG_HOME", config)
    monkeypatch.setattr(node.m, "_project_env", lambda: {})
    monkeypatch.setenv("FIXTURE_SECRET", "process-secret")
    owner = node.runtime["services"]["agent"] if embedded else node.runtime["services"].setdefault("fixture", {})
    owner["required_env"] = ["FIXTURE_SECRET"]
    if embedded:
        owner["embedded_services"] = ["fixture"]
    else:
        owner["capability"] = "fixture"
    host = {"address": "192.0.2.10"}
    env = node.m._node_environment(node.root, node.runtime, node.target, host, "git@192.0.2.10:/srv/git/body.git", node.key, "main")
    assert "FIXTURE_SECRET=machine|secret\n" in env
    monkeypatch.setattr(node.m, "_project_env", lambda: {"FIXTURE_SECRET": "project-secret"})
    env = node.m._node_environment(node.root, node.runtime, node.target, host, "git@192.0.2.10:/srv/git/body.git", node.key, "main")
    assert "FIXTURE_SECRET=project-secret\n" in env


def test_local_coolify_host_uses_pairing_address_and_openssh_identity(node, monkeypatch):
    monkeypatch.setattr("socket.gethostbyname", lambda host: "192.0.2.10")
    key = node.root / "identity"
    key.write_text("fixture key")
    monkeypatch.setattr(node.m, "_records", lambda: SimpleNamespace(connections=lambda *a, **kw: {
        "fixture": {"enabled": True, "allow_write": True, "value": {"base_url": "https://node.example.invalid"}}}))
    original = node.m._node_coolify
    def provider(root, target, *args, **kw):
        if args[0] == "servers":
            return {"uuid": "server-fixture", "name": "localhost", "ip": "host.docker.internal", "is_coolify_host": True}
        return original(root, target, *args, **kw)
    monkeypatch.setattr(node.m, "_node_coolify", provider)
    run = node.m._node_run
    def command(argv, root, **kw):
        if argv[:2] == ["ssh", "-G"]:
            return SimpleNamespace(stdout=f"identityfile {key}\n")
        return run(argv, root, **kw)
    monkeypatch.setattr(node.m, "_node_run", command)
    host = node.m._node_host(node.root, node.target)
    assert host["address"] == "192.0.2.10"
    assert host["key_path"] == str(key)


def test_first_deploy_requires_no_unavailable_rollback(node, monkeypatch):
    monkeypatch.setattr(node.m, "_node_run", lambda *a, **kw: SimpleNamespace(stdout="coolify wait"))
    node.m._node_provider_requirements(node.root, create=True)
    with pytest.raises(node.m.NodeFailure, match="app rollback"):
        node.m._node_provider_requirements(node.root, create=False)


def test_container_discovery_without_numeric_api_id(node, monkeypatch):
    original = node.m._node_coolify
    def provider(root, target, *args, **kw):
        if args[0] == "applications":
            return {"uuid": "application-fixture", "status": "running:healthy"}
        return original(root, target, *args, **kw)
    monkeypatch.setattr(node.m, "_node_coolify", provider)
    host = {"connection": "fixture"}
    assert node.m._node_container(node.root, host, "application-fixture") == "aabbccddeeff"
    assert "label=com.docker.compose.project=application-fixture" in node.ssh[-1][0]
