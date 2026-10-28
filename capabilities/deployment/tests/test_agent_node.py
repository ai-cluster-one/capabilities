"""An agent node from the standard image: `deployment node create`, `enrol` and
`remove` drive coolify for the resource, the manager for the node row and the
placements, and SSH with docker exec for the node itself."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "deployment"
KEY = "A" * 42 + "Q="
# The id the node is minted with; it is shown by its label, box1.
BOX1 = "b0b100000001"


class Fake:
    """Answers coolify, capabilities, ssh and the capabilities' own
    `connections`, recording every call with its standard input."""

    def __init__(self, tmp_path: Path):
        self.calls: list[dict] = []
        self.rows: dict[str, dict] = {}
        self.fail_add = False
        self.secret_file = tmp_path / ".env.local"
        self.secret_file.write_text("TELEGRAM_BOT_TOKEN=bot-secret\n"
                                    "COOLIFY_INFRA_TOKEN=coolify-secret\n"
                                    "ASSETS_FILES_ACCESS_KEY_ID=files-secret\n"
                                    "DRIVE_SA_JSON=drive-secret\n")
        self.state = "running agent prj_00000000a1a1 as node box1"
        self.gone: set[str] = set()
        self.apps: set[str] = set()
        self.fail_storage = False
        # The manager's `secrets list` for the agent: no key here, by default.
        self.vault = {"key": {"set": False, "fingerprint": None, "matches": None},
                      "secrets": []}
        self.telegram_tier = "project"
        self.config = {"Image": "ghcr.io/agenium-dev/agent-node:0.0.1",
                       "Env": ["CAPABILITIES_PROJECT_ID=prj_00000000a1a1",
                               "CAPABILITIES_PROJECT_ID_ROOT=/app",
                               "AGENT_NODE_NAME=box1", "PATH=/usr/bin"]}
        # The image the node's Coolify service names, which a deploy starts,
        # the container's Docker health, and whether coolify has set-compose.
        self.service_image = self.config["Image"]
        self.health = "healthy"
        self.set_compose = True
        # Coolify hides each service's Compose document, as it does from a
        # token without read:sensitive: set-image cannot read it.
        self.compose = None
        # What the node's container mounts and its memory limit, and the
        # environment and volumes of its image, as docker inspect reads them.
        self.mounts = [{"Type": "volume", "Name": f"svc-new_{name}", "Destination": path}
                       for name, path in (("config", "/home/agent/.config"),
                                          ("state", "/home/agent/.local/state"),
                                          ("claude", "/home/agent/.claude"),
                                          ("codex", "/home/agent/.codex"),
                                          ("work", "/home/agent/work"))]
        self.memory = 2 << 30
        self.image_config = {"Env": ["PATH=/usr/bin"], "Volumes": None}
        self.containers = "abc123\n"
        # The Coolify projects `projects` lists; one read by a uuid not among
        # them answers with a test and a production environment.
        self.projects: list[dict] = []

    def __call__(self, argv, root, *, stdin=None, timeout=120, check=True, env=None):
        self.calls.append({"argv": list(argv), "stdin": stdin})
        out = self.answer(list(argv), stdin)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, SimpleNamespace):
            return out
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def answer(self, argv, stdin):
        tool, args = argv[0], argv[1:]
        if tool == "coolify":
            if "--connection" in args:
                i = args.index("--connection")
                del args[i:i + 2]
            if args == ["projects"]:
                return json.dumps([{k: v for k, v in row.items() if k != "environments"}
                                   for row in self.projects])
            if args[:2] == ["projects", "create"]:
                self.projects.append({"uuid": "prj-made", "name": args[2],
                                      "environments": [{"name": "production"}]})
                return json.dumps({"uuid": "prj-made"})
            if args[:3] == ["projects", "environment", "create"]:
                project = next(row for row in self.projects if row["uuid"] == args[3])
                project["environments"].append({"name": args[4]})
                return json.dumps({"uuid": "env-made"})
            if args[:1] == ["projects"]:
                project = next((row for row in self.projects if row["uuid"] == args[1]), None)
                return json.dumps(project or {"environments": [{"name": "test"},
                                                               {"name": "production"}]})
            if args[:2] == ["service", "create"]:
                return json.dumps({"uuid": "svc-new", "domains": []})
            if args[:1] == ["services"]:
                if args[1] in self.gone or args[1] in self.apps:
                    return self.module.NodeFailure("not_found", "fixture", 3)
                return json.dumps({"uuid": args[1]})
            if args[:2] in (["service", "delete"], ["app", "delete"]):
                return json.dumps({"message": "deleted"})
            if args[:2] == ["app", "create"]:
                self.apps.add("app-new")
                return json.dumps({"uuid": "app-new", "domains": None})
            if args[:1] == ["applications"]:
                if args[1] in self.gone or args[1] not in self.apps:
                    return self.module.NodeFailure("not_found", "fixture", 3)
                return json.dumps({"uuid": args[1]})
            if args[:2] in (["app", "update"], ["env", "bulk"]):
                return json.dumps({"ok": True})
            if args[:2] == ["storage", "add"]:
                if self.fail_storage:
                    return self.module.NodeFailure("validation_failed", "fixture", 6)
                return json.dumps({"uuid": "storage-" + str(len(self.calls))})
            if args == ["help"]:
                return "coolify help\n  service set-image <uuid>\n" + (
                    "  service set-compose <uuid>\n" if self.set_compose else "")
            if args[:2] == ["service", "set-image"]:
                return self.module.NodeFailure("compose_hidden", "fixture", 2)
            if args[:2] == ["service", "set-compose"]:
                assert args[3:] == ["--compose", "-"]
                self.compose = stdin
                self.service_image = json.loads(stdin.split("image: ", 1)[1].splitlines()[0])
                return json.dumps({"uuid": args[2], "written": True})
            if args[:1] == ["deploy"]:
                if args[1] not in self.apps:
                    self.config = {**self.config, "Image": self.service_image}
                return json.dumps({"deployments": [{"resource_uuid": args[1],
                                                    "deployment_uuid": "dep-1"}]})
            if args[:1] == ["servers"]:
                return json.dumps({"uuid": args[1], "ip": "203.0.113.7", "name": "srv"})
            if args == ["connections"]:
                return json.dumps({"connections": {"infra": {"keys": [
                    {"key": "base_url", "secret": False, "set": True,
                     "tier": "connection", "source": "store"},
                    {"key": "COOLIFY_INFRA_TOKEN", "secret": True, "set": True,
                     "tier": "project", "source": str(self.secret_file)}]}}})
            raise AssertionError(args)
        if tool == "capabilities":
            if args[:2] == ["node", "list"]:
                return json.dumps({"nodes": list(self.rows.values())})
            if args[:2] == ["node", "add"]:
                if self.fail_add:
                    return self.module.NodeFailure("store_unreachable", "fixture", 5)
                flags = dict(zip(args[3::2], args[4::2]))
                self.rows[args[2]] = {"node": args[2], "label": flags["--label"],
                                      "kind": flags["--kind"],
                                      "coolify_connection": flags["--coolify-connection"],
                                      "coolify_server": flags["--coolify-server"],
                                      "coolify_resource": flags["--coolify-resource"],
                                      "public_key": None}
                return json.dumps({"node": self.rows[args[2]]})
            if args[:2] == ["node", "set"]:
                self.rows[args[2]]["public_key"] = args[4]
                return json.dumps({"node": self.rows[args[2]]})
            if args[:2] == ["node", "remove"]:
                return json.dumps({"removed": self.rows.pop(args[2])})
            if args[:2] == ["secrets", "list"]:
                return json.dumps(self.vault)
            if args[:1] == ["place"]:
                return json.dumps({"placed": args[1], "node": args[3],
                                   "unplaced_from": ["mac"] if args[1] == "tasks" else []})
            raise AssertionError(args)
        if tool == "ssh":
            if "-G" in args:
                return ""
            remote = shlex.split(args[-1])
            if remote[:2] == ["docker", "ps"]:
                # An application's container is no Compose service named `agent`.
                if any(w.endswith("project=" + app) for app in self.apps for w in remote) \
                        and "label=com.docker.compose.service=agent" in remote:
                    return ""
                return self.containers
            if remote[:3] == ["docker", "image", "inspect"]:
                return json.dumps(self.image_config) + "\n"
            if remote[:2] == ["docker", "inspect"] and remote[3] == "{{json .}}":
                return json.dumps({"Image": "sha256:" + "e" * 64, "Config": self.config,
                                   "Mounts": self.mounts,
                                   "HostConfig": {"Memory": self.memory}}) + "\n"
            if remote[:2] == ["docker", "inspect"] and remote[3].startswith("{{.Config.Image}}"):
                return f"{self.config['Image']} {self.health}\n"
            if remote[:2] == ["docker", "inspect"]:
                return "".join(json.dumps(self.config) + "\n" for _id in remote[4:])
            assert remote[:2] == ["docker", "exec"], remote
            inner = [w for w in remote[2:] if w != "-i"][1:]
            if inner[:2] == ["sh", "-c"]:
                return KEY + "\n"
            if inner[:2] == ["python3", "-c"] and "AGENT_ENCRYPTION_KEY" in inner[2]:
                return ""
            if inner[:2] == ["python3", "-c"]:
                return json.dumps({name: len(keys) for name, keys in json.loads(stdin).items()})
            if inner[:1] == ["capabilities"]:
                return json.dumps({"ok": True})
            if inner[:1] == ["cat"]:
                return self.state
            raise AssertionError(inner)
        if tool == "telegram" and args == ["connections"]:
            vault = self.telegram_tier == "vault"
            return json.dumps({"connections": {"default": {"keys": [
                {"key": "TELEGRAM_BOT_TOKEN", "secret": True, "set": True,
                 "tier": self.telegram_tier,
                 "source": "vault:agt_fixture" if vault else str(self.secret_file)},
                {"key": "TELEGRAM_CHAT", "secret": False, "set": True,
                 "tier": "connection", "source": "store"}]}}})
        if tool == "assets" and args == ["connections"]:
            return json.dumps({"connections": {"files": {"keys": [
                {"key": "bucket", "secret": False, "set": True,
                 "tier": "connection", "source": "store"},
                {"key": "ASSETS_FILES_ACCESS_KEY_ID", "secret": True, "set": True,
                 "tier": "project", "source": str(self.secret_file)},
                {"key": "ASSETS_FILES_SECRET_ACCESS_KEY", "secret": True, "set": False,
                 "tier": None, "source": None}]}}})
        if tool == "rclonec" and args == ["connections"]:
            return json.dumps({"connections": {"drive": {"keys": [
                {"key": "service_account_credentials (DRIVE_SA_JSON)", "secret": True,
                 "set": True, "tier": "project", "source": str(self.secret_file)}]}}})
        if tool == "slack" and args == ["connections"]:
            # Its tokens are in the container's environment alone: it has no
            # connection, and says so by exiting 6.
            return SimpleNamespace(returncode=6, stdout="", stderr=json.dumps(
                {"error": {"code": "connections_required", "message": "fixture"}}))
        if args == ["connections"]:
            return json.dumps({"connections": {}})
        raise AssertionError(argv)

    def of(self, tool):
        return [c for c in self.calls if c["argv"][0] == tool]


@pytest.fixture
def lab(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader("agent_node_deployment", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    fake = Fake(tmp_path)
    fake.module = m
    root = tmp_path / "agent"
    root.mkdir()
    node_run = m._node_run
    monkeypatch.setattr(m, "_root", lambda: root)
    monkeypatch.setattr(m, "_node_run", fake)
    monkeypatch.setattr(m, "_store_placed", lambda root: True)
    monkeypatch.setattr(m, "_project_id", lambda: "prj_00000000a1a1")
    monkeypatch.setattr(m, "_project_slug", lambda root: "lab-agent")
    usable = m._usable_capabilities
    monkeypatch.setattr(m, "_usable_capabilities", lambda root: ["telegram"])
    monkeypatch.setattr(m, "_installed_manifest", lambda name: (None, "not installed"))
    monkeypatch.setattr(m, "_records", lambda: SimpleNamespace(
        connections=lambda *a, **k: {"infra": {"enabled": True, "allow_write": True,
                                               "value": {"base_url": "https://coolify.invalid"}}}))
    monkeypatch.setattr(m, "_agent_node_store_args", lambda root: (
        ["store", "set", "--host", "db.invalid", "--database", "agents", "--user", "agent",
         "--schema", "agentkit", "--password-stdin"], "store-secret\n"))
    monkeypatch.setattr(m.time, "sleep", lambda seconds: None)
    # The id `node create` mints for the node it makes.
    monkeypatch.setattr(m.secrets, "token_hex", lambda n: BOX1)
    return SimpleNamespace(m=m, fake=fake, root=root, usable=usable, node_run=node_run)


def create_args(**extra):
    return SimpleNamespace(**{"node": "box1", "project": "prj-uuid", "server": "srv-uuid",
                              "environment": "test", "version": "0.0.1", "memory": "2g",
                              "connection": None, "repository": None, "branch": None,
                              "dockerfile": None, "context": None, "deploy_key": None,
                              "github_app": None, "docker_socket": False, "host_dir": None,
                              **extra})


def build_args(**extra):
    return create_args(**{"version": None, "repository": "git@github.com:org/product.git",
                          "branch": "main", "dockerfile": "dev/agent-node/Dockerfile",
                          "deploy_key": "key-uuid", "docker_socket": True,
                          "host_dir": ["/srv/product"], **extra})


def test_create_makes_a_resource_of_the_standard_image_and_registers_its_row(lab, capsys):
    lab.m.cmd_node_create(create_args())

    [create] = [c for c in lab.fake.of("coolify") if "create" in c["argv"]]
    argv = create["argv"]
    assert argv[:3] == ["coolify", "--connection", "infra"]
    assert argv[3:6] == ["service", "create", "--compose"]
    for flag, value in (("--project", "prj-uuid"), ("--server", "srv-uuid"),
                        ("--environment", "test"), ("--name", "lab-agent-box1")):
        assert argv[argv.index(flag) + 1] == value
    assert "--instant-deploy" in argv
    # The whole document: the image, its memory limit, its volumes and the two
    # keys it is told - no node identity - and nothing to build or clone.
    assert create["stdin"] == (
        'services:\n'
        '  agent:\n'
        '    image: "ghcr.io/agenium-dev/agent-node:0.0.1"\n'
        '    restart: unless-stopped\n'
        '    mem_limit: "2g"\n'
        '    environment:\n'
        '      CAPABILITIES_PROJECT_ID: "prj_00000000a1a1"\n'
        '      CAPABILITIES_PROJECT_ID_ROOT: "/app"\n'
        '    volumes:\n'
        '      - "config:/home/agent/.config"\n'
        '      - "state:/home/agent/.local/state"\n'
        '      - "claude:/home/agent/.claude"\n'
        '      - "codex:/home/agent/.codex"\n'
        '      - "work:/home/agent/work"\n'
        'volumes:\n'
        '  config: {}\n'
        '  state: {}\n'
        '  claude: {}\n'
        '  codex: {}\n'
        '  work: {}\n')

    assert lab.fake.rows[BOX1] == {"node": BOX1, "label": "box1", "kind": "server",
                                     "coolify_connection": "infra",
                                     "coolify_server": "srv-uuid",
                                     "coolify_resource": "svc-new", "public_key": None}
    answer = json.loads(capsys.readouterr().out)
    assert answer["node"]["coolify_resource"] == "svc-new"
    assert answer["next"] == f"deployment node enrol {BOX1}"


def test_create_refuses_a_node_the_agent_has_and_takes_back_a_resource_it_cannot_register(lab):
    lab.fake.rows[BOX1] = {"node": BOX1, "label": "box1"}
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_create(create_args())
    assert refused.value.code == "node_exists"
    assert not [c for c in lab.fake.of("coolify") if "create" in c["argv"]]

    lab.fake.rows.clear()
    lab.fake.fail_add = True
    with pytest.raises(lab.m.NodeFailure):
        lab.m.cmd_node_create(create_args())
    [delete] = [c["argv"] for c in lab.fake.of("coolify") if "delete" in c["argv"]]
    assert delete[3:6] == ["service", "delete", "svc-new"]
    assert "--delete-volumes" in delete


def test_enrol_gives_the_node_its_key_secrets_placements_and_store(lab, capsys):
    lab.m.cmd_node_create(create_args())
    capsys.readouterr()
    lab.m.cmd_node_enrol(SimpleNamespace(node="box1", services="tasks,automations", wait=30))

    assert lab.fake.rows[BOX1]["public_key"] == KEY
    docker = [c for c in lab.fake.of("ssh") if "docker exec" in c["argv"][-1]]
    # The node's id goes onto its volume first, through the node's own manager.
    assert f"capabilities node id --set {BOX1}" in docker[0]["argv"][-1]
    assert all("label=com.docker.compose.project=svc-new" in c["argv"][-1]
               for c in lab.fake.of("ssh") if "docker ps" in c["argv"][-1])
    # No secret is ever on a command line; each goes on standard input.
    for call in lab.fake.calls:
        assert "bot-secret" not in " ".join(call["argv"])
        assert "store-secret" not in " ".join(call["argv"])
    [secrets] = [c for c in docker if "python3" in c["argv"][-1]]
    assert json.loads(secrets["stdin"]) == {"telegram": {"TELEGRAM_BOT_TOKEN": "bot-secret"}}
    [store] = [c for c in docker if "capabilities store set" in c["argv"][-1]]
    assert store["stdin"] == "store-secret\n"
    places = [c["argv"][1:] for c in lab.fake.of("capabilities") if c["argv"][1] == "place"]
    assert places == [["place", "tasks", "--node", BOX1],
                      ["place", "automations", "--node", BOX1]]
    # The store setting is what the node raises on, so it is written last.
    order = [i for i, c in enumerate(lab.fake.calls)
             if c in (secrets, store) or c["argv"][1:2] == ["place"]]
    assert lab.fake.calls.index(store) == max(order)

    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is True and answer["placed"] == ["tasks", "automations"]
    assert (answer["node"], answer["label"]) == (BOX1, "box1")
    assert answer["unplaced"] == {"tasks": ["mac"]}
    assert answer["secrets"] == {"telegram": ["TELEGRAM_BOT_TOKEN"]}
    assert "bot-secret" not in json.dumps(answer)


def test_enrol_leaves_the_coolify_token_the_node_was_created_through_here(lab, monkeypatch,
                                                                         capsys):
    monkeypatch.setattr(lab.m, "_usable_capabilities", lambda root: ["coolify", "telegram"])
    lab.m.cmd_node_create(create_args())
    capsys.readouterr()
    lab.m.cmd_node_enrol(SimpleNamespace(node="box1", services="", wait=30))

    [secrets] = [c for c in lab.fake.of("ssh")
                 if "docker exec" in c["argv"][-1] and "python3" in c["argv"][-1]]
    assert json.loads(secrets["stdin"]) == {"telegram": {"TELEGRAM_BOT_TOKEN": "bot-secret"}}
    assert "coolify-secret" not in secrets["stdin"]
    assert json.loads(capsys.readouterr().out)["secrets"] == {"telegram": ["TELEGRAM_BOT_TOKEN"]}


def test_a_capability_enabled_globally_is_usable_and_one_disabled_is_not(lab, monkeypatch):
    rows = {"assets": {"scope": "global", "value": {"enabled": True}},
            "telegram": {"scope": "project", "value": {"enabled": True}},
            "slack": {"scope": "project", "value": {"enabled": False}}}
    monkeypatch.setattr(lab.m, "_records", lambda: SimpleNamespace(
        resolve=lambda kind, collection: rows))
    assert lab.usable(lab.root) == ["assets", "telegram"]


def test_enrol_carries_every_usable_capabilitys_connections_and_names_what_it_skips(
        lab, monkeypatch, capsys):
    # assets is enabled globally, not for the project: the agent uses it all
    # the same, so its connection's keys go to the node.
    monkeypatch.setattr(lab.m, "_usable_capabilities",
                        lambda root: ["assets", "coolify", "rclonec", "telegram"])
    lab.m.cmd_node_create(create_args())
    capsys.readouterr()
    lab.m.cmd_node_enrol(SimpleNamespace(node="box1", services="", wait=30))

    [secrets] = [c for c in lab.fake.of("ssh")
                 if "docker exec" in c["argv"][-1] and "python3" in c["argv"][-1]]
    assert json.loads(secrets["stdin"]) == {
        "assets": {"ASSETS_FILES_ACCESS_KEY_ID": "files-secret"},
        "rclonec": {"DRIVE_SA_JSON": "drive-secret"},
        "telegram": {"TELEGRAM_BOT_TOKEN": "bot-secret"}}
    out = capsys.readouterr().out
    answer = json.loads(out)
    assert {(row["capability"], row["key"]): row["reason"]
            for row in answer["secrets_skipped"]} == {
        ("assets", "ASSETS_FILES_SECRET_ACCESS_KEY"): "not set on this machine",
        ("coolify", "COOLIFY_INFRA_TOKEN"):
            "the connection the node was created through stays on this machine"}
    for value in ("files-secret", "drive-secret", "bot-secret", "coolify-secret"):
        assert value not in out


def test_enrol_names_each_secret_a_capability_reads_from_the_environment_alone(
        lab, monkeypatch, capsys):
    from capabilities_contract import db
    manifest = {"credentials": {"keys": [
        {"key": "TELEGRAM_BOT_TOKEN", "secret": True},
        {"key": "TELEGRAM_FLAT_TOKEN", "secret": True},
        {"key": "TELEGRAM_VOICE", "secret": True}]},
        "service": {"deploy": {"environment": {
            "required": ["TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN"],
            "optional": [{"key": "TELEGRAM_SESSION"}, {"key": "TELEGRAM_VOICE"},
                         {"key": next(k for k in db.KEYS if k.endswith("PASSWORD"))},
                         {"key": "TELEGRAM_CHANNEL_ENABLED", "default": "true"}]}}}}
    monkeypatch.setattr(lab.m, "_installed_manifest",
                        lambda name: (manifest, None) if name == "telegram" else (None, "x"))
    monkeypatch.setenv("TELEGRAM_SESSION", "session-secret")
    lab.m.cmd_node_create(create_args())
    capsys.readouterr()
    lab.m.cmd_node_enrol(SimpleNamespace(node="box1", services="", wait=30))
    out = capsys.readouterr().out
    answer = json.loads(out)
    # Its service reads them from its environment and no connection reports
    # them, so no node receives them: each is named, by its key alone, for it
    # to be moved into a connection. A key its connection reports, a flat
    # credential its service does not read and the database's are not.
    assert answer["environment_only"] == {
        "telegram": ["TELEGRAM_API_HASH", "TELEGRAM_SESSION", "TELEGRAM_VOICE"]}
    assert "connection" in answer["environment_only_hint"]
    assert "session-secret" not in out


def test_enrol_names_the_secrets_of_a_capability_with_no_connection_to_report(
        lab, monkeypatch, capsys):
    manifest = {"credentials": {"keys": [{"key": "SLACK_BOT_TOKEN", "secret": True}]},
                "service": {"deploy": {"environment": {
                    "required": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"], "optional": []}}}}
    monkeypatch.setattr(lab.m, "_usable_capabilities", lambda root: ["slack", "telegram"])
    monkeypatch.setattr(lab.m, "_installed_manifest",
                        lambda name: (manifest, None) if name == "slack" else (None, "x"))
    lab.m.cmd_node_create(create_args())
    capsys.readouterr()
    lab.m.cmd_node_enrol(SimpleNamespace(node="box1", services="", wait=30))
    answer = json.loads(capsys.readouterr().out)
    assert answer["environment_only"] == {"slack": ["SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"]}
    assert [row["capability"] for row in answer["secrets_skipped"]] == ["slack"]
    assert "gave no report (exit 6)" in answer["secrets_skipped"][0]["reason"]


def test_a_gate_the_store_cannot_answer_refuses_enrol_as_input(lab, monkeypatch):
    def unreachable():
        raise lab.m.StoreError("store_unreachable", "fixture", None)
    monkeypatch.setattr(lab.m, "_records", lambda: SimpleNamespace(
        resolve=lambda kind, collection: unreachable()))
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.usable(lab.root)
    assert (refused.value.code, refused.value.exit_code) == ("store_unreachable", 6)


AGENT_KEY = "k" * 43


def _agent_key(lab, monkeypatch, matches=True):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(lab.root.parent / "config"))
    monkeypatch.delenv("AGENT_ENCRYPTION_KEY", raising=False)
    (lab.root / ".env.local").write_text(f"AGENT_ENCRYPTION_KEY={AGENT_KEY}\n")
    lab.fake.vault = {"key": {"set": True, "fingerprint": "f" * 64, "matches": matches},
                      "secrets": [{"name": "TELEGRAM_BOT_TOKEN", "readable": True},
                                  {"name": "OLD_ONE", "readable": False}]}


@pytest.mark.parametrize("tier", ["project", "vault"])
def test_enrol_gives_the_node_the_agents_key_instead_of_a_secret_its_vault_holds(
        lab, monkeypatch, capsys, tier):
    # Kept in a file here as well, or resolved from the vault alone: either
    # way the node reads it from the vault, and it is not reported skipped.
    _agent_key(lab, monkeypatch)
    lab.fake.telegram_tier = tier
    lab.m.cmd_node_create(create_args())
    capsys.readouterr()
    lab.m.cmd_node_enrol(SimpleNamespace(node="box1", services="", wait=30))

    docker = [c for c in lab.fake.of("ssh") if "docker exec" in c["argv"][-1]]
    [placed] = [c for c in docker if "AGENT_ENCRYPTION_KEY" in c["argv"][-1]]
    assert placed["stdin"] == AGENT_KEY
    for call in lab.fake.calls:
        assert AGENT_KEY not in " ".join(call["argv"])
    # The one file secret is in the vault, so no capability's file is written.
    assert not [c for c in docker if "credentials.env" in c["argv"][-1]]
    [store] = [c for c in docker if "capabilities store set" in c["argv"][-1]]
    assert lab.fake.calls.index(placed) < lab.fake.calls.index(store)
    answer = json.loads(capsys.readouterr().out)
    assert answer["key"] == {"placed": True, "fingerprint": "f" * 64}
    assert answer["secrets"] == {} and answer["secrets_in_vault"] == {
        "telegram": ["TELEGRAM_BOT_TOKEN"]}
    assert not [row for row in answer["secrets_skipped"] if row["key"] == "TELEGRAM_BOT_TOKEN"]
    assert AGENT_KEY not in json.dumps(answer)


def test_enrol_refuses_a_key_that_does_not_open_the_vault_before_touching_the_node(
        lab, monkeypatch, capsys):
    _agent_key(lab, monkeypatch, matches=False)
    lab.m.cmd_node_create(create_args())
    before = len(lab.fake.of("ssh"))
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_enrol(SimpleNamespace(node="box1", services="", wait=30))
    assert refused.value.code == "key_mismatch"
    assert len(lab.fake.of("ssh")) == before


def test_enrol_reports_a_node_that_did_not_raise_its_agent(lab, capsys):
    lab.m.cmd_node_create(create_args())
    lab.fake.state = "idle: waiting for store-setting"
    with pytest.raises(lab.m.NodeFailure) as failed:
        lab.m.cmd_node_enrol(SimpleNamespace(node="box1", services="", wait=0))
    assert failed.value.code == "node_not_raised"


def test_remove_deletes_its_own_resource_with_its_volumes_and_its_row(lab, capsys):
    lab.m.cmd_node_create(create_args())
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_remove(SimpleNamespace(node="box1", yes=False))
    assert refused.value.code == "confirmation_required"

    # Renamed since it was made, its container still told the old label: it
    # is the node's own all the same, and its new label names it.
    lab.fake.rows[BOX1]["label"] = "renamed"
    lab.m.cmd_node_remove(SimpleNamespace(node="renamed", yes=True))
    [delete] = [c["argv"] for c in lab.fake.of("coolify") if "delete" in c["argv"]]
    assert delete == ["coolify", "--connection", "infra", "service", "delete", "svc-new",
                      "--yes", "--delete-volumes", "--delete-configurations"]
    assert BOX1 not in lab.fake.rows

    lab.fake.rows["mac"] = {"node": "mac", "label": "mac", "kind": "mac",
                            "coolify_resource": None}
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_remove(SimpleNamespace(node="mac", yes=True))
    assert refused.value.code == "node_not_coolify"


@pytest.mark.parametrize("config", [
    {"Image": "registry.invalid/other-service:1.0",
     "Env": ["CAPABILITIES_PROJECT_ID=prj_00000000a1a1"]},
    {"Image": "ghcr.io/agenium-dev/agent-node:0.0.1",
     "Env": ["CAPABILITIES_PROJECT_ID=prj_00000000b2b2", "AGENT_NODE_NAME=box1"]},
])
def test_remove_refuses_a_resource_that_is_not_the_nodes_own(lab, capsys, config):
    lab.m.cmd_node_create(create_args())
    lab.fake.rows[BOX1]["coolify_resource"] = "svc-other"
    lab.fake.config = config
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_remove(SimpleNamespace(node="box1", yes=True))
    assert refused.value.code == "node_resource_not_own"
    assert not [c for c in lab.fake.of("coolify") if "delete" in c["argv"]]
    assert BOX1 in lab.fake.rows


def test_remove_of_a_resource_already_gone_removes_only_the_row(lab, capsys):
    lab.m.cmd_node_create(create_args())
    lab.fake.gone.add("svc-new")
    capsys.readouterr()
    lab.m.cmd_node_remove(SimpleNamespace(node="box1", yes=True))
    assert json.loads(capsys.readouterr().out)["resource_deleted"] is False
    assert not [c for c in lab.fake.of("coolify") if "delete" in c["argv"]]
    assert BOX1 not in lab.fake.rows


def test_a_node_is_made_only_for_an_agent_that_lives_in_the_store(lab, monkeypatch):
    monkeypatch.setattr(lab.m, "_store_placed", lambda root: False)
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_create(create_args())
    assert refused.value.code == "not_store_mode"
    assert not lab.fake.of("coolify")


def test_create_builds_a_node_from_a_repository_with_the_socket_and_same_path_dirs(lab, capsys):
    lab.m.cmd_node_create(build_args(memory="4g"))

    coolify = [c for c in lab.fake.of("coolify")]
    verbs = [c["argv"][3:5] for c in coolify]
    assert verbs == [["projects", "prj-uuid"], ["app", "create"], ["app", "update"],
                     ["env", "bulk"]] + [["storage", "add"]] * 7 + [["deploy", "app-new"]]
    create = coolify[1]["argv"]
    for flag, value in (("--project", "prj-uuid"), ("--server", "srv-uuid"),
                        ("--environment", "test"), ("--private-deploy-key", "key-uuid"),
                        ("--git-repository", "git@github.com:org/product.git"),
                        ("--git-branch", "main"), ("--build-pack", "dockerfile"),
                        ("--base-directory", "/"),
                        ("--dockerfile-location", "/dev/agent-node/Dockerfile"),
                        ("--name", "lab-agent-box1"), ("--limits-memory", "4g")):
        assert create[create.index(flag) + 1] == value
    assert "--no-autogenerate-domain" in create and "--no-health-check-enabled" in create
    assert "--instant-deploy" not in create and "--github-app" not in create
    assert coolify[2]["argv"][5:] == ["app-new", "--no-auto-deploy-enabled"]
    # Only the two keys, as for a node of the standard image.
    assert coolify[3]["stdin"] == ("CAPABILITIES_PROJECT_ID=prj_00000000a1a1\n"
                                   "CAPABILITIES_PROJECT_ID_ROOT=/app\n")
    storages = [c["argv"][5:] for c in coolify[4:11]]
    assert storages == [
        ["app-new", "--volume", "config", "--mount-path", "/home/agent/.config"],
        ["app-new", "--volume", "state", "--mount-path", "/home/agent/.local/state"],
        ["app-new", "--volume", "claude", "--mount-path", "/home/agent/.claude"],
        ["app-new", "--volume", "codex", "--mount-path", "/home/agent/.codex"],
        ["app-new", "--volume", "work", "--mount-path", "/home/agent/work"],
        ["app-new", "--host-path", "/var/run/docker.sock", "--mount-path", "/var/run/docker.sock"],
        ["app-new", "--host-path", "/srv/product", "--mount-path", "/srv/product"]]

    assert lab.fake.rows[BOX1]["coolify_resource"] == "app-new"
    answer = json.loads(capsys.readouterr().out)
    assert answer["resource"] == {"kind": "application", "uuid": "app-new"}
    assert answer["deployment"] == "dep-1"
    assert answer["host_paths"] == ["/var/run/docker.sock", "/srv/product"]
    assert answer["image"] == {"repository": "git@github.com:org/product.git",
                               "branch": "main", "context": "/",
                               "dockerfile": "/dev/agent-node/Dockerfile"}


def test_create_takes_back_an_application_it_could_not_finish(lab):
    lab.fake.fail_storage = True
    with pytest.raises(lab.m.NodeFailure):
        lab.m.cmd_node_create(build_args())
    [delete] = [c["argv"] for c in lab.fake.of("coolify") if "delete" in c["argv"]]
    assert delete[3:] == ["app", "delete", "app-new", "--yes", "--delete-volumes",
                          "--delete-configurations"]
    assert BOX1 not in lab.fake.rows


@pytest.mark.parametrize("extra, wrong", [
    ({"repository": "git@github.com:org/product.git"}, "either --version"),
    ({"docker_socket": True}, "--docker-socket"),
    ({"host_dir": ["/srv/product"]}, "--host-dir"),
])
def test_a_standard_image_node_takes_no_build_and_no_host_path(lab, extra, wrong):
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_create(create_args(**extra))
    assert refused.value.code == "input" and wrong in refused.value.message
    assert not lab.fake.of("coolify")


@pytest.mark.parametrize("extra", [
    {"context": "/dev/other"},
    {"host_dir": ["/home/agent/work"]},
    {"host_dir": ["/srv/../etc"]},
    {"deploy_key": "key-uuid", "github_app": "app-uuid"},
])
def test_a_repository_node_refuses_what_it_cannot_build_or_bind(lab, extra):
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_create(build_args(**extra))
    assert refused.value.code == "input"
    assert not lab.fake.of("coolify")


def test_enrol_and_remove_reach_a_node_built_from_a_repository(lab, capsys):
    lab.m.cmd_node_create(build_args())
    capsys.readouterr()
    lab.m.cmd_node_enrol(SimpleNamespace(node="box1", services="", wait=30))
    assert json.loads(capsys.readouterr().out)["ok"] is True

    lab.fake.config = {"Image": "app-new:0123abc",
                       "Env": ["CAPABILITIES_PROJECT_ID=prj_00000000a1a1"]}
    before = len(lab.fake.calls)
    lab.m.cmd_node_remove(SimpleNamespace(node="box1", yes=True))
    [delete] = [c["argv"] for c in lab.fake.of("coolify") if "delete" in c["argv"]]
    assert delete[3:] == ["app", "delete", "app-new", "--yes", "--delete-volumes",
                          "--delete-configurations"]
    # On the server remove only reads: the host paths stay as they are.
    remote = [shlex.split(c["argv"][-1])[:2] for c in lab.fake.calls[before:]
              if c["argv"][0] == "ssh" and "-G" not in c["argv"]]
    assert remote and all(r in (["docker", "ps"], ["docker", "inspect"]) for r in remote)
    assert BOX1 not in lab.fake.rows


def test_remove_refuses_an_application_that_does_not_run_its_own_build_as_the_node(lab):
    lab.m.cmd_node_create(build_args())
    lab.fake.config = {"Image": "ghcr.io/agenium-dev/agent-node:0.0.3",
                       "Env": ["AGENT_NODE_NAME=box1"]}
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_remove(SimpleNamespace(node="box1", yes=True))
    assert refused.value.code == "node_resource_not_own"
    assert not [c for c in lab.fake.of("coolify") if "delete" in c["argv"]]


def test_exec_runs_one_command_in_the_node_container_with_its_stdio_and_exit_code(lab, monkeypatch):
    lab.m.cmd_node_create(create_args())
    ran = []
    monkeypatch.setattr(lab.m.subprocess, "call", lambda argv, **kw: ran.append(argv) or 3)
    with pytest.raises(SystemExit) as exited:
        lab.m.cmd_node_exec(SimpleNamespace(node="box1",
                                            argv=["telegram", "login", "--events"]))
    assert exited.value.code == 3
    # No terminal, so each event line comes back as written; -i forwards stdin.
    [argv] = ran
    assert argv[:3] == ["ssh", "-o", "BatchMode=yes"] and "-t" not in argv
    assert argv[-2] == "root@203.0.113.7"
    assert shlex.split(argv[-1]) == ["docker", "exec", "-i", "abc123",
                                     "telegram", "login", "--events"]


def test_exec_forwards_piped_stdin_past_the_lookups_it_runs_first(lab, monkeypatch, tmp_path):
    lab.m.cmd_node_create(create_args())
    # Each lookup goes through the real _node_run to a real child that reads
    # its standard input, as ssh does, and is then answered by the fake.
    real_run = lab.m.subprocess.run

    def lookup(argv, **kw):
        real_run(["sh", "-c", "cat >/dev/null"], **kw)
        return lab.fake(argv, kw["cwd"], stdin=kw.get("input"))

    received = tmp_path / "received"
    monkeypatch.setattr(lab.m, "_node_run", lab.node_run)
    monkeypatch.setattr(lab.m.subprocess, "run", lookup)
    monkeypatch.setattr(lab.m.subprocess, "call", lambda argv, **kw: real_run(
        ["sh", "-c", "cat > " + shlex.quote(str(received))]).returncode)
    piped = tmp_path / "piped"
    piped.write_text("x")
    saved = lab.m.os.dup(0)
    try:
        with piped.open() as source:
            lab.m.os.dup2(source.fileno(), 0)
        with pytest.raises(SystemExit) as exited:
            lab.m.cmd_node_exec(SimpleNamespace(node="box1", argv=["cat"]))
    finally:
        lab.m.os.dup2(saved, 0)
        lab.m.os.close(saved)
    assert exited.value.code == 0
    assert received.read_text() == "x"


def test_exec_refuses_an_unknown_node_and_a_missing_command(lab, monkeypatch):
    monkeypatch.setattr(lab.m.subprocess, "call", lambda argv, **kw: pytest.fail("ran"))
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_exec(SimpleNamespace(node="box1", argv=["true"]))
    assert refused.value.code == "node_unknown"
    lab.m.cmd_node_create(create_args())
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_exec(SimpleNamespace(node="box1", argv=[]))
    assert refused.value.code == "input"


@pytest.fixture
def signin(lab, monkeypatch, tmp_path):
    """A node whose container is this machine: `ssh` is `sh -c`, `docker exec
    -i <container>` runs the rest here, and the tools and capabilities it runs
    are scripts in a folder put first on PATH. The real sign-in runner runs."""
    lab.fake.rows[BOX1] = {"node": BOX1, "label": "box1", "coolify_resource": "svc-new"}
    bin_dir = tmp_path / "node-bin"
    bin_dir.mkdir()

    def tool(name, body):
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    tool("docker", 'shift; [ "$1" = -i ] && shift; shift; exec "$@"\n')
    monkeypatch.setenv("PATH", f"{bin_dir}:{lab.m.os.environ['PATH']}")
    monkeypatch.setattr(lab.m, "_agent_node_reach",
                        lambda root, row, write: ({}, {"address": "node.invalid"}, "c1"))
    monkeypatch.setattr(lab.m, "_node_ssh_argv", lambda host, **kw: ["sh", "-c"])
    sessions = []

    def run(argv, root, **kw):
        if argv[:3] == ["capabilities", "node", "state"]:
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"nodes": [
                {"node": BOX1, "label": "box1", "sessions": []},
                {"node": "a1a1a1a1a1a1", "label": "mac", "sessions": sessions}]}))
        if argv[0] == "capabilities":
            return lab.fake(argv, root, **kw)
        return lab.node_run(argv, root, **kw)

    monkeypatch.setattr(lab.m, "_node_run", run)
    lab.m._EVENTS_ON[0] = True
    yield SimpleNamespace(tool=tool, sessions=sessions, dir=tmp_path)
    lab.m._EVENTS_ON[0] = False


def login_args(tool, **extra):
    return SimpleNamespace(**{"harness": "box1", "tool": tool, "events": True, "target": None,
                              "print_command": False, "connection": None, "method": None,
                              **extra})


def events(out):
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def test_login_on_a_node_speaks_the_tools_link_code_and_wait_as_events(lab, signin, capfd):
    signin.tool("gh", 'echo "! First copy your one-time code: AB12-CD34" >&2\n'
                      'echo "Open this URL to continue in your web browser: '
                      'https://github.com/login/device" >&2\n'
                      'echo "$*" > "$(dirname "$0")/../gh-argv"\n')
    lab.m.cmd_node_login(login_args("gh"))
    out = capfd.readouterr()
    said = events(out.out)
    assert [e["event"] for e in said] == ["link", "code", "waiting", "done"]
    assert said[0] == {"event": "link", "node": BOX1, "tool": "gh",
                       "url": "https://github.com/login/device"}
    assert said[1]["code"] == "AB12-CD34"
    assert said[3]["result"] == {"ok": True, "node": BOX1, "tool": "gh"}
    assert (signin.dir / "gh-argv").read_text().split() == [
        "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web",
        "--skip-ssh-key"]
    # The tool's own words are kept, on stderr.
    assert "one-time code" in out.err


def test_claude_asks_for_the_code_back_and_gets_the_line_on_stdin(lab, signin, capfd,
                                                                  monkeypatch):
    signin.tool("claude", 'echo "If the browser didn\'t open, visit: '
                          'https://claude.example/oauth?code=true&state=s"\n'
                          'printf "Paste code here if prompted > "\n'
                          'read code; echo "$code" > "$(dirname "$0")/../pasted"\n'
                          'echo "Login successful."\n')
    monkeypatch.setattr(lab.m.sys, "stdin", io.StringIO("abc#def\n"))
    lab.m.cmd_node_login(login_args("claude"))
    said = events(capfd.readouterr().out)
    assert [e["event"] for e in said] == ["link", "code_needed", "waiting", "done"]
    assert said[0]["url"] == "https://claude.example/oauth?code=true&state=s"
    assert (signin.dir / "pasted").read_text() == "abc#def\n"


def test_a_sign_in_that_fails_says_why_with_the_tools_exit_code(lab, signin, capfd):
    signin.tool("codex", 'printf "1. Open this link\\n   \\033[94mhttps://auth.example/'
                         'device\\033[0m\\n2. Enter this one-time code\\n   '
                         '\\033[94mWXYZ-23456\\033[0m\\n"\n'
                         'echo "Error logging in: device code expired" >&2; exit 3\n')
    with pytest.raises(lab.m.NodeFailure) as failed:
        lab.m.cmd_node_login(login_args("codex"))
    said = events(capfd.readouterr().out)
    assert [e["event"] for e in said] == ["link", "code", "waiting"]
    assert said[0]["url"] == "https://auth.example/device" and said[1]["code"] == "WXYZ-23456"
    assert (failed.value.code, failed.value.exit_code) == ("login_failed", 3)
    assert failed.value.message.endswith("Error logging in: device code expired")


def test_cancelling_a_sign_in_stops_the_tool_on_the_node(lab, signin, capfd):
    import signal
    import threading
    signin.tool("gh", 'trap \'echo stopped > "$(dirname "$0")/../stopped"; exit 143\' TERM\n'
                      'echo "Open this URL: https://github.com/login/device" >&2\n'
                      'while :; do sleep 0.1; done\n')
    timer = threading.Timer(1.0, lab.m.os.kill, (lab.m.os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        with pytest.raises(lab.m.NodeFailure) as failed:
            lab.m.cmd_node_login(login_args("gh"))
    finally:
        timer.cancel()
    assert (failed.value.code, failed.value.exit_code) == ("cancelled", 6)
    assert (signin.dir / "stopped").read_text() == "stopped\n"
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


WHATSAPP = {"connections": {"device_bound": True, "link": [
    {"id": "qr", "argv": ["whatsapp", "pair", "--qr", "--events"]},
    {"id": "code", "argv": ["whatsapp", "pair", "--events"]}]}}


def whatsapp_tool(signin, *, live=False, bound=True):
    connections = {"connections": {"main": {
        "keys": [{"key": "expected_account_id", "set": bound}],
        "session": {"live": live, "account": "15550001111" if live else None}}}}
    signin.tool("whatsapp", f"""case "$1" in
manifest) echo '{json.dumps(WHATSAPP)}';;
connections) echo '{json.dumps(connections)}';;
*) echo "$*" > "$(dirname "$0")/../ceremony"; read number
   echo '{{"event": "pairing_code", "code": "ABCD-EFGH"}}'; echo "$number" > "$(dirname "$0")/../number"; exit 7;;
esac
""")


def test_a_device_sign_in_runs_the_capabilitys_ceremony_and_says_the_devices_in_use(
        lab, signin, capfd, monkeypatch):
    whatsapp_tool(signin)
    signin.sessions.append({"capability": "whatsapp", "connection": "main", "live": True,
                            "account": "15550001111"})
    signin.sessions.append({"capability": "telegram", "connection": "main", "live": True,
                            "account": "42"})
    monkeypatch.setattr(lab.m.sys, "stdin", io.StringIO("15550001111\n"))
    with pytest.raises(SystemExit) as exited:
        lab.m.cmd_node_login(login_args("whatsapp", method="code"))
    said = events(capfd.readouterr().out)
    assert said == [{"event": "devices", "node": BOX1, "capability": "whatsapp",
                     "connection": "main", "limit": 4,
                     "in_use": [{"node": "a1a1a1a1a1a1", "label": "mac",
                                 "account": "15550001111", "live": True}]},
                    {"event": "pairing_code", "code": "ABCD-EFGH"}]
    # The capability's own exit code, its ceremony with the connection, no --recreate.
    assert exited.value.code == 7
    assert (signin.dir / "ceremony").read_text().split() == [
        "pair", "--events", "--connection", "main"]
    assert (signin.dir / "number").read_text() == "15550001111\n"


def test_a_device_sign_in_never_replaces_a_session(lab, signin, capfd):
    whatsapp_tool(signin, live=True)
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_login(login_args("whatsapp"))
    assert refused.value.code == "already_signed_in"
    # Nor takes a connection another node signs in through out from under it.
    whatsapp_tool(signin, bound=False)
    signin.sessions.append({"capability": "whatsapp", "connection": "main", "live": True,
                            "account": "15550001111"})
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_login(login_args("whatsapp"))
    assert refused.value.code == "connection_unbound"
    assert "expected_account_id=15550001111" in refused.value.message
    assert not (signin.dir / "ceremony").exists()
    assert not events(capfd.readouterr().out)


def test_node_login_refuses_what_it_cannot_sign_in(lab, signin, capfd):
    signin.tool("tasks", "echo '{\"connections\": {}}'\n")
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_login(login_args("tasks"))
    assert refused.value.code == "not_device_bound"
    for args in (login_args("gh", events=False), login_args(None),
                 login_args("gh", connection="main"), login_args("Bad Tool")):
        with pytest.raises(lab.m.NodeFailure) as refused:
            lab.m.cmd_node_login(args)
        assert refused.value.code == "input"


def update_args(**extra):
    return SimpleNamespace(**{"node": "box1", "version": "0.0.10", "memory": None, "wait": 30,
                              **extra})


def test_update_moves_a_standard_image_node_to_the_tag_and_waits_for_its_agent(lab, capsys):
    lab.m.cmd_node_create(create_args())
    capsys.readouterr()
    lab.m.cmd_node_update(update_args())

    coolify = [c["argv"][3:] for c in lab.fake.of("coolify")
               if c["argv"][3:4] in (["service"], ["deploy"]) and "create" not in c["argv"]]
    assert coolify == [["service", "set-compose", "svc-new", "--compose", "-"],
                       ["deploy", "svc-new"]]
    # The whole document node create writes, with the new tag, and no read of
    # the Compose Coolify hides from a token without read:sensitive.
    created = next(c["stdin"] for c in lab.fake.of("coolify") if "create" in c["argv"])
    assert lab.fake.compose == created.replace("agent-node:0.0.1", "agent-node:0.0.10")
    assert lab.fake.compose == lab.m._agent_node_compose(
        "prj_00000000a1a1", "ghcr.io/agenium-dev/agent-node:0.0.10", "2g")
    # Through the connection the row names, and nothing deleted.
    assert all(c["argv"][1:3] == ["--connection", "infra"] for c in lab.fake.of("coolify")
               if c["argv"][1:2] != ["help"])
    assert not [c for c in lab.fake.of("coolify") if "delete" in c["argv"]]
    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is True and answer["redeployed"] is True
    assert answer["node"] == BOX1 and answer["resource"] == "svc-new"
    assert answer["image"] == answer["running_image"] == "ghcr.io/agenium-dev/agent-node:0.0.10"
    assert answer["previous_image"] == "ghcr.io/agenium-dev/agent-node:0.0.1"
    assert answer["health"] == "healthy" and answer["state"].startswith("running agent")
    assert answer["volumes"] == ["config", "state", "claude", "codex", "work"]
    # The retired identity key is dropped and named, never its value.
    assert answer["memory"] == "2g" and answer["env_dropped"] == ["AGENT_NODE_NAME"]
    assert answer["volumes_added"] == []
    assert "box1" not in lab.fake.compose


def test_update_keeps_the_memory_limit_the_node_runs_with(lab, capsys):
    lab.m.cmd_node_create(create_args(memory="1536m"))
    lab.fake.memory = 1536 << 20
    capsys.readouterr()
    lab.m.cmd_node_update(update_args())
    assert 'mem_limit: "1536m"' in lab.fake.compose
    assert json.loads(capsys.readouterr().out)["memory"] == "1536m"


@pytest.mark.parametrize("version", ["0.0.10", "0.0.1"])
def test_update_writes_the_memory_limit_it_is_given(lab, capsys, version):
    lab.m.cmd_node_create(create_args())
    capsys.readouterr()
    lab.m.cmd_node_update(update_args(version=version, memory="5g"))
    # On the tag or off it, a new limit writes the document and redeploys it.
    assert lab.fake.compose == lab.m._agent_node_compose(
        "prj_00000000a1a1", f"ghcr.io/agenium-dev/agent-node:{version}", "5g")
    assert [c["argv"][3:] for c in lab.fake.of("coolify")
            if c["argv"][3:4] == ["deploy"]] == [["deploy", "svc-new"]]
    answer = json.loads(capsys.readouterr().out)
    assert answer["memory"] == "5g" and answer["redeployed"] is True


def test_update_with_the_memory_limit_the_node_runs_with_does_not_redeploy_it(lab, capsys):
    lab.m.cmd_node_create(create_args())
    capsys.readouterr()
    lab.m.cmd_node_update(update_args(version="0.0.1", memory="2048m"))
    assert lab.fake.compose is None
    assert json.loads(capsys.readouterr().out)["redeployed"] is False


def _refused_before_any_change(lab, code):
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_update(update_args())
    assert refused.value.code == code and refused.value.exit_code == 6
    assert lab.fake.compose is None
    assert not [c for c in lab.fake.of("coolify") if c["argv"][3:4] == ["deploy"]]
    return refused.value.message


@pytest.mark.parametrize("change,named", [
    # A volume under another name would start the node empty.
    (lambda mounts: mounts[0].update(Name="other_config"), "/home/agent/.config"),
    (lambda mounts: mounts[1].update(Type="bind", Name=""), "/home/agent/.local/state"),
    # One the document does not mount would be dropped.
    (lambda mounts: mounts.append({"Type": "volume", "Name": "svc-new_extra",
                                   "Destination": "/data"}), "/data"),
    (lambda mounts: mounts.append({"Type": "bind", "Source": "/srv",
                                   "Destination": "/srv"}), "/srv"),
])
def test_update_refuses_volumes_that_are_not_node_creates(lab, change, named):
    lab.m.cmd_node_create(create_args())
    change(lab.fake.mounts)
    assert named in _refused_before_any_change(lab, "node_volumes_unconfirmed")


def test_update_gives_a_node_created_before_its_work_volume_that_volume(lab, capsys):
    # A node created before node create declared `work` mounts nothing there:
    # the document's volume is added, new and empty, and none is renamed.
    lab.m.cmd_node_create(create_args())
    lab.fake.mounts[:] = [m for m in lab.fake.mounts if m["Destination"] != "/home/agent/work"]
    capsys.readouterr()
    lab.m.cmd_node_update(update_args())
    assert lab.fake.compose == lab.m._agent_node_compose(
        "prj_00000000a1a1", "ghcr.io/agenium-dev/agent-node:0.0.10", "2g")
    answer = json.loads(capsys.readouterr().out)
    assert answer["volumes_added"] == ["work"] and answer["redeployed"] is True


def test_update_on_the_tag_adds_a_missing_volume(lab, capsys):
    lab.m.cmd_node_create(create_args())
    lab.fake.mounts[:] = [m for m in lab.fake.mounts if m["Destination"] != "/home/agent/work"]
    capsys.readouterr()
    lab.m.cmd_node_update(update_args(version="0.0.1"))
    assert lab.fake.compose is not None
    assert [c for c in lab.fake.of("coolify") if c["argv"][3:4] == ["deploy"]]
    assert json.loads(capsys.readouterr().out)["volumes_added"] == ["work"]


def test_update_starts_again_a_node_on_the_tag_whose_container_stopped(lab, capsys, monkeypatch):
    # An update that left the node down is finished by running it once more.
    lab.m.cmd_node_create(create_args())
    running = lab.m._agent_node_running
    deployed = lambda: [c for c in lab.fake.of("coolify") if c["argv"][3:4] == ["deploy"]]
    monkeypatch.setattr(lab.m, "_agent_node_running",
                        lambda *a: running(*a) if deployed() else None)
    capsys.readouterr()
    lab.m.cmd_node_update(update_args(version="0.0.1"))
    assert lab.fake.compose is None and len(deployed()) == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is True and answer["redeployed"] is True


def test_update_keeps_an_anonymous_volume_the_image_declares(lab, capsys):
    lab.m.cmd_node_create(create_args())
    lab.fake.image_config["Volumes"] = {"/cache": {}}
    lab.fake.mounts.append({"Type": "volume", "Name": "f" * 64, "Destination": "/cache"})
    lab.m.cmd_node_update(update_args())
    assert lab.fake.compose is not None


@pytest.mark.parametrize("change,named", [
    (lambda fake: setattr(fake, "containers", "abc123\ndef456\n"), "found 2"),
    # A node whose container is gone has nothing to confirm its volumes from.
    (lambda fake: setattr(fake, "containers", ""), "coolify deploy svc-new"),
    (lambda fake: setattr(fake, "memory", 0), "no memory limit"),
    (lambda fake: fake.config["Env"].remove("CAPABILITIES_PROJECT_ID_ROOT=/app"),
     "CAPABILITIES_PROJECT_ID_ROOT differ"),
    (lambda fake: fake.config["Env"].append("EXTRA_SETTING=1"), "EXTRA_SETTING would be dropped"),
    # A node still keyed by its name reads AGENT_NODE_NAME at boot to move to its id.
    (lambda fake: fake.config["Env"].append("AGENT_NODE_ID=other"), "AGENT_NODE_ID"),
])
def test_update_refuses_what_it_cannot_establish_without_the_compose(lab, change, named):
    lab.m.cmd_node_create(create_args())
    change(lab.fake)
    assert named in _refused_before_any_change(lab, "node_document_unconfirmed")


def test_update_keeps_the_name_a_node_still_keyed_by_it_needs(lab):
    lab.m.cmd_node_create(create_args())
    lab.fake.config["Env"] = [e for e in lab.fake.config["Env"]
                              if not e.startswith("AGENT_NODE_NAME=")] + [
        "AGENT_NODE_NAME=" + BOX1]
    assert "AGENT_NODE_NAME" in _refused_before_any_change(lab, "node_document_unconfirmed")


def test_update_takes_coolifys_own_keys_and_the_images_environment(lab, capsys):
    lab.m.cmd_node_create(create_args())
    lab.fake.image_config["Env"] += ["LANG=C.UTF-8"]
    lab.fake.config["Env"] += ["LANG=C.UTF-8", "COOLIFY_CONTAINER_NAME=agent-svc",
                               "COOLIFY_RESOURCE_UUID=svc-new", "AGENT_NODE_ID=" + BOX1]
    capsys.readouterr()
    lab.m.cmd_node_update(update_args())
    assert json.loads(capsys.readouterr().out)["env_dropped"] == ["AGENT_NODE_ID",
                                                                 "AGENT_NODE_NAME"]


def test_update_of_a_node_already_on_the_tag_does_not_redeploy_it(lab, capsys):
    lab.m.cmd_node_create(create_args())
    capsys.readouterr()
    lab.m.cmd_node_update(update_args(version="0.0.1"))
    assert not [c for c in lab.fake.of("coolify") if c["argv"][3:4] == ["deploy"]]
    assert json.loads(capsys.readouterr().out)["redeployed"] is False


def test_update_refuses_a_node_built_from_a_repository_and_points_at_coolify_deploy(lab):
    lab.m.cmd_node_create(build_args())
    before = len(lab.fake.calls)
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_update(update_args())
    assert refused.value.code == "node_built_from_repository"
    assert refused.value.exit_code == 6
    assert "coolify deploy app-new" in refused.value.message
    assert not [c for c in lab.fake.calls[before:]
                if c["argv"][0] == "coolify" and ("set-compose" in c["argv"] or "deploy" in c["argv"])]


@pytest.mark.parametrize("config", [
    {"Image": "registry.invalid/other-service:1.0",
     "Env": ["CAPABILITIES_PROJECT_ID=prj_00000000a1a1"]},
    {"Image": "ghcr.io/agenium-dev/agent-node:0.0.1",
     "Env": ["CAPABILITIES_PROJECT_ID=prj_00000000b2b2"]},
])
def test_update_refuses_a_resource_that_is_not_the_nodes_own(lab, config):
    lab.m.cmd_node_create(create_args())
    lab.fake.config = config
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_update(update_args())
    assert refused.value.code == "node_resource_not_own"
    assert lab.fake.compose is None


def test_update_refuses_without_set_compose_a_bad_tag_and_an_unknown_node(lab):
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_update(update_args())
    assert refused.value.code == "node_unknown"
    lab.m.cmd_node_create(create_args())
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_update(update_args(version="../x"))
    assert refused.value.code == "input"
    for memory in ("", "5 g", "0g", "5t"):
        with pytest.raises(lab.m.NodeFailure) as refused:
            lab.m.cmd_node_update(update_args(memory=memory))
        assert refused.value.code == "input" and refused.value.exit_code == 6
        assert refused.value.message == "--memory takes a size such as 2g or 1536m"
    lab.fake.set_compose = False
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_update(update_args())
    assert refused.value.code == "provider_not_ready"
    assert lab.fake.compose is None


@pytest.mark.parametrize("health,state", [("unhealthy", "running agent prj as node box1"),
                                          ("healthy", "idle: waiting for store-setting")])
def test_update_reports_a_node_that_did_not_come_back(lab, capsys, health, state):
    lab.m.cmd_node_create(create_args())
    capsys.readouterr()
    lab.fake.health, lab.fake.state = health, state
    with pytest.raises(lab.m.NodeFailure) as failed:
        lab.m.cmd_node_update(update_args(wait=0))
    assert failed.value.code == "node_not_raised" and failed.value.exit_code == 5
    assert json.loads(capsys.readouterr().out)["ok"] is False


def add_args(**extra):
    return SimpleNamespace(**{"node": "box1", "server": "srv-uuid", "version": "0.0.1",
                              "memory": "2g", "project": None, "environment": "production",
                              "services": "", "connection": None, "wait": 0, "events": True,
                              **extra})


@pytest.fixture
def stream(lab):
    lab.m._EVENTS_ON[0] = True
    yield
    lab.m._EVENTS_ON[0] = False


def steps(out):
    return [(e["step"], e["state"]) for e in events(out) if e["event"] == "step"]


def test_add_takes_a_node_from_nothing_to_raised_and_says_each_step(lab, stream, capsys):
    lab.m.cmd_node_add(add_args(services="tasks"))

    said = capsys.readouterr().out
    assert steps(said) == [(name, state) for name in (
        "project", "environment", "create", "container", "id", "key_pair", "encryption_key",
        "secrets", "place", "store", "raised") for state in ("started", "done")]
    checks = {e["step"]: e for e in events(said) if e.get("state") == "done"}
    # No project carried the agent's name: one is made, with its environment.
    assert checks["project"]["check"] == {"uuid": "prj-made", "name": "lab-agent",
                                          "created": True}
    assert checks["environment"]["check"] == {"name": "production", "created": False}
    assert checks["create"]["check"] == {"node": BOX1, "resource": "svc-new", "created": True}
    assert checks["place"]["service"] == "tasks"
    [create] = [c["argv"] for c in lab.fake.of("coolify") if c["argv"][3:5] == ["service",
                                                                                "create"]]
    assert create[create.index("--project") + 1] == "prj-made"
    assert create[create.index("--environment") + 1] == "production"
    assert lab.fake.rows[BOX1]["public_key"] == KEY
    [done] = [e for e in events(said) if e["event"] == "done"]
    result = done["result"]
    assert result["ok"] is True and result["created"] is True
    assert result["project"] == {"uuid": "prj-made", "name": "lab-agent"}
    assert result["placed"] == ["tasks"] and result["resource"] == "svc-new"
    assert "bot-secret" not in said


def test_add_makes_the_environment_a_given_project_lacks(lab, stream, capsys):
    lab.fake.projects.append({"uuid": "prj-uuid", "name": "other",
                              "environments": [{"name": "production"}]})
    lab.m.cmd_node_add(add_args(project="prj-uuid", environment="staging"))

    made = [c["argv"][3:] for c in lab.fake.of("coolify") if "create" in c["argv"]]
    assert made[0] == ["projects", "environment", "create", "prj-uuid", "staging"]
    assert ["projects", "create"] not in [argv[:2] for argv in made]
    checks = {e["step"]: e["check"] for e in events(capsys.readouterr().out)
              if e.get("state") == "done"}
    assert checks["project"]["created"] is False
    assert checks["environment"] == {"name": "staging", "created": True}


def test_add_again_after_a_failure_keeps_what_is_done_and_resumes(lab, stream, capsys):
    # The container never comes up: the node is made and the add stops there.
    lab.fake.containers = ""
    with pytest.raises(lab.m.NodeFailure) as failed:
        lab.m.cmd_node_add(add_args())
    assert failed.value.code == "node_not_running"
    said = capsys.readouterr().out
    assert steps(said)[-1] == ("container", "failed")
    [failure] = [e for e in events(said) if e.get("state") == "failed"]
    assert failure["error"]["code"] == "node_not_running"

    # Then it comes up, but the agent does not rise in time.
    lab.fake.containers = "abc123\n"
    lab.fake.state = "idle: waiting for store-setting"
    with pytest.raises(lab.m.NodeFailure) as failed:
        lab.m.cmd_node_add(add_args())
    assert failed.value.code == "node_not_raised"
    assert steps(capsys.readouterr().out)[-1] == ("raised", "failed")

    # The third run finds the project, the environment and the node done.
    lab.fake.state = "running agent prj_00000000a1a1 as node box1"
    lab.m.cmd_node_add(add_args())
    said = capsys.readouterr().out
    checks = {e["step"]: e["check"] for e in events(said) if e.get("state") == "done"}
    assert checks["project"] == {"uuid": "prj-made", "name": "lab-agent", "created": False}
    assert checks["create"] == {"node": BOX1, "resource": "svc-new", "created": False}
    assert [e for e in events(said) if e["event"] == "done"][0]["result"]["ok"] is True
    made = [c["argv"][3:5] for c in lab.fake.of("coolify") if "create" in c["argv"]]
    assert made == [["projects", "create"], ["service", "create"]]
    assert list(lab.fake.rows) == [BOX1]


def test_add_refuses_before_writing_what_it_could_not_finish(lab, monkeypatch, capsys):
    lab.fake.rows["a1a1a1a1a1a1"] = {"node": "a1a1a1a1a1a1", "label": "box1",
                                     "coolify_connection": "infra",
                                     "coolify_server": "other-srv",
                                     "coolify_resource": "svc-old"}
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_add(add_args(events=False))
    assert refused.value.code == "node_exists"
    lab.fake.rows.clear()
    _agent_key(lab, monkeypatch, matches=False)
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_add(add_args(events=False))
    assert refused.value.code == "key_mismatch"
    assert not [c for c in lab.fake.of("coolify") if "create" in c["argv"]]
    assert not lab.fake.of("ssh")


def test_add_without_events_prints_the_answer_alone(lab, capsys):
    lab.m.cmd_node_add(add_args(events=False))
    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is True and answer["node"] == BOX1
    assert answer["environment"] == "production" and answer["created"] is True


class Registry:
    """Answers the registry's anonymous token and its tag list, a page at a
    time, as ghcr.io does."""

    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def __call__(self, request, timeout=None):
        url = getattr(request, "full_url", request)
        self.urls.append(url)
        if "/token?" in url:
            body, link = {"token": "anon"}, None
        else:
            assert request.get_header("Authorization") == "Bearer anon"
            index = int(url.rsplit("page=", 1)[1]) if "page=" in url else 0
            body = {"name": "agenium-dev/agent-node", "tags": self.pages[index]}
            link = (f'</v2/agenium-dev/agent-node/tags/list?page={index + 1}>; rel="next"'
                    if index + 1 < len(self.pages) else None)
        answer = io.BytesIO(json.dumps(body).encode())
        answer.headers = {"Link": link} if link else {}
        return answer


@pytest.fixture
def registry(lab, monkeypatch):
    import urllib.request
    fake = Registry([["probe", "0.0.1", "latest", "0.0.9", "0.0.10-amd64"],
                     ["0.0.10-arm64", "0.0.10", "0.0.2"]])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def test_node_image_names_the_newest_release_its_registry_holds(lab, registry, capsys):
    lab.m.cmd_node_image(SimpleNamespace(json=True))
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "version": "0.0.10", "image": "ghcr.io/agenium-dev/agent-node:0.0.10"}
    assert registry.urls[0] == ("https://ghcr.io/token?scope=repository:"
                                "agenium-dev/agent-node:pull")
    lab.m.cmd_node_image(SimpleNamespace(json=False))
    assert capsys.readouterr().out.strip() == "ghcr.io/agenium-dev/agent-node:0.0.10"


def test_node_image_says_when_the_registry_cannot_name_a_release(lab, monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", Registry([["latest", "probe"]]))
    with pytest.raises(lab.m.NodeFailure) as unknown:
        lab.m.cmd_node_image(SimpleNamespace(json=True))
    assert unknown.value.code == "image_release_unknown" and unknown.value.exit_code == 5

    def unreachable(request, timeout=None):
        raise OSError("no route")
    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    with pytest.raises(lab.m.NodeFailure) as unknown:
        lab.m.cmd_node_image(SimpleNamespace(json=True))
    assert unknown.value.code == "image_release_unknown"


def test_create_and_add_without_a_version_take_the_current_release(lab, registry, capsys):
    lab.m.cmd_node_create(create_args(version=None))
    assert json.loads(capsys.readouterr().out)["image"] == "ghcr.io/agenium-dev/agent-node:0.0.10"
    lab.fake.rows.clear()
    lab.fake.calls.clear()
    lab.m.cmd_node_add(add_args(version=None, events=False))
    assert json.loads(capsys.readouterr().out)["image"] == "ghcr.io/agenium-dev/agent-node:0.0.10"
    [create] = [c for c in lab.fake.of("coolify") if c["argv"][3:5] == ["service", "create"]]
    assert 'image: "ghcr.io/agenium-dev/agent-node:0.0.10"' in create["stdin"]
