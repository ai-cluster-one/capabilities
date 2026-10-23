"""An agent node from the standard image: `deployment node create`, `enrol` and
`remove` drive coolify for the resource, the manager for the node row and the
placements, and SSH with docker exec for the node itself."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "deployment"
KEY = "A" * 42 + "Q="


class Fake:
    """Answers coolify, capabilities, ssh and the capabilities' own
    `connections`, recording every call with its standard input."""

    def __init__(self, tmp_path: Path):
        self.calls: list[dict] = []
        self.rows: dict[str, dict] = {}
        self.fail_add = False
        self.secret_file = tmp_path / ".env.local"
        self.secret_file.write_text("TELEGRAM_BOT_TOKEN=bot-secret\n"
                                    "COOLIFY_INFRA_TOKEN=coolify-secret\n")
        self.state = "running agent prj_00000000a1a1 as node box1"
        self.gone: set[str] = set()
        self.config = {"Image": "ghcr.io/agenium-dev/agent-node:0.0.1",
                       "Env": ["AGENT_NODE_NAME=box1", "PATH=/usr/bin"]}

    def __call__(self, argv, root, *, stdin=None, timeout=120, check=True, env=None):
        self.calls.append({"argv": list(argv), "stdin": stdin})
        out = self.answer(list(argv), stdin)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def answer(self, argv, stdin):
        tool, args = argv[0], argv[1:]
        if tool == "coolify":
            if "--connection" in args:
                i = args.index("--connection")
                del args[i:i + 2]
            if args[:1] == ["projects"]:
                return json.dumps({"environments": [{"name": "test"}, {"name": "production"}]})
            if args[:2] == ["service", "create"]:
                return json.dumps({"uuid": "svc-new", "domains": []})
            if args[:1] == ["services"]:
                if args[1] in self.gone:
                    return self.module.NodeFailure("not_found", "fixture", 3)
                return json.dumps({"uuid": args[1]})
            if args[:2] == ["service", "delete"]:
                return json.dumps({"message": "deleted"})
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
                self.rows[args[2]] = {"node": args[2], "kind": flags["--kind"],
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
            if args[:1] == ["enable"]:
                return json.dumps({"ok": True})
            raise AssertionError(args)
        if tool == "ssh":
            if "-G" in args:
                return ""
            remote = shlex.split(args[-1])
            if remote[:2] == ["docker", "ps"]:
                return "abc123\n"
            if remote[:2] == ["docker", "inspect"]:
                return json.dumps(self.config) + "\n"
            assert remote[:2] == ["docker", "exec"], remote
            inner = [w for w in remote[2:] if w != "-i"][1:]
            if inner[:2] == ["sh", "-c"]:
                return KEY + "\n"
            if inner[:2] == ["python3", "-c"]:
                return json.dumps({name: len(keys) for name, keys in json.loads(stdin).items()})
            if inner[:1] == ["capabilities"]:
                return json.dumps({"ok": True})
            if inner[:1] == ["cat"]:
                return self.state
            raise AssertionError(inner)
        if tool == "telegram" and args == ["connections"]:
            return json.dumps({"connections": {"default": {"keys": [
                {"key": "TELEGRAM_BOT_TOKEN", "secret": True, "set": True,
                 "tier": "project", "source": str(self.secret_file)},
                {"key": "TELEGRAM_CHAT", "secret": False, "set": True,
                 "tier": "connection", "source": "store"}]}}})
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
    monkeypatch.setattr(m, "_root", lambda: root)
    monkeypatch.setattr(m, "_node_run", fake)
    monkeypatch.setattr(m, "_store_placed", lambda root: True)
    monkeypatch.setattr(m, "_project_id", lambda: "prj_00000000a1a1")
    monkeypatch.setattr(m, "_project_slug", lambda root: "lab-agent")
    monkeypatch.setattr(m, "_enabled_capabilities", lambda root: ["telegram"])
    monkeypatch.setattr(m, "_records", lambda: SimpleNamespace(
        connections=lambda *a, **k: {"infra": {"enabled": True, "allow_write": True,
                                               "value": {"base_url": "https://coolify.invalid"}}}))
    monkeypatch.setattr(m, "_agent_node_store_args", lambda root: (
        ["store", "set", "--host", "db.invalid", "--database", "agents", "--user", "agent",
         "--schema", "agentkit", "--password-stdin"], "store-secret\n"))
    monkeypatch.setattr(m.time, "sleep", lambda seconds: None)
    return SimpleNamespace(m=m, fake=fake, root=root)


def create_args(**extra):
    return SimpleNamespace(**{"node": "box1", "project": "prj-uuid", "server": "srv-uuid",
                              "environment": "test", "version": "0.0.1", "memory": "2g",
                              "connection": None, **extra})


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
    # The whole document: the image, its memory limit, its state volumes and
    # the three keys it is told, and nothing to build or clone.
    assert create["stdin"] == (
        'services:\n'
        '  agent:\n'
        '    image: "ghcr.io/agenium-dev/agent-node:0.0.1"\n'
        '    restart: unless-stopped\n'
        '    mem_limit: "2g"\n'
        '    environment:\n'
        '      CAPABILITIES_PROJECT_ID: "prj_00000000a1a1"\n'
        '      CAPABILITIES_PROJECT_ID_ROOT: "/app"\n'
        '      AGENT_NODE_NAME: "box1"\n'
        '    volumes:\n'
        '      - "config:/home/agent/.config"\n'
        '      - "state:/home/agent/.local/state"\n'
        '      - "claude:/home/agent/.claude"\n'
        '      - "codex:/home/agent/.codex"\n'
        'volumes:\n'
        '  config: {}\n'
        '  state: {}\n'
        '  claude: {}\n'
        '  codex: {}\n')

    assert lab.fake.rows["box1"] == {"node": "box1", "kind": "server",
                                     "coolify_connection": "infra",
                                     "coolify_server": "srv-uuid",
                                     "coolify_resource": "svc-new", "public_key": None}
    assert json.loads(capsys.readouterr().out)["node"]["coolify_resource"] == "svc-new"


def test_create_refuses_a_node_the_agent_has_and_takes_back_a_resource_it_cannot_register(lab):
    lab.fake.rows["box1"] = {"node": "box1"}
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

    assert lab.fake.rows["box1"]["public_key"] == KEY
    docker = [c for c in lab.fake.of("ssh") if "docker exec" in c["argv"][-1]]
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
    enables = [c["argv"][1:] for c in lab.fake.of("capabilities") if c["argv"][1] == "enable"]
    assert enables == [["enable", "tasks", "--project", "--node", "box1"],
                       ["enable", "automations", "--project", "--node", "box1"]]
    # The store setting is what the node raises on, so it is written last.
    order = [i for i, c in enumerate(lab.fake.calls)
             if c in (secrets, store) or c["argv"][1:2] == ["enable"]]
    assert lab.fake.calls.index(store) == max(order)

    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is True and answer["placed"] == ["tasks", "automations"]
    assert answer["secrets"] == {"telegram": ["TELEGRAM_BOT_TOKEN"]}
    assert "bot-secret" not in json.dumps(answer)


def test_enrol_leaves_the_coolify_token_the_node_was_created_through_here(lab, monkeypatch,
                                                                         capsys):
    monkeypatch.setattr(lab.m, "_enabled_capabilities", lambda root: ["coolify", "telegram"])
    lab.m.cmd_node_create(create_args())
    capsys.readouterr()
    lab.m.cmd_node_enrol(SimpleNamespace(node="box1", services="", wait=30))

    [secrets] = [c for c in lab.fake.of("ssh")
                 if "docker exec" in c["argv"][-1] and "python3" in c["argv"][-1]]
    assert json.loads(secrets["stdin"]) == {"telegram": {"TELEGRAM_BOT_TOKEN": "bot-secret"}}
    assert "coolify-secret" not in secrets["stdin"]
    assert json.loads(capsys.readouterr().out)["secrets"] == {"telegram": ["TELEGRAM_BOT_TOKEN"]}


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

    lab.m.cmd_node_remove(SimpleNamespace(node="box1", yes=True))
    [delete] = [c["argv"] for c in lab.fake.of("coolify") if "delete" in c["argv"]]
    assert delete == ["coolify", "--connection", "infra", "service", "delete", "svc-new",
                      "--yes", "--delete-volumes", "--delete-configurations"]
    assert "box1" not in lab.fake.rows

    lab.fake.rows["mac"] = {"node": "mac", "kind": "mac", "coolify_resource": None}
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_remove(SimpleNamespace(node="mac", yes=True))
    assert refused.value.code == "node_not_coolify"


@pytest.mark.parametrize("config", [
    {"Image": "registry.invalid/other-service:1.0", "Env": ["AGENT_NODE_NAME=box1"]},
    {"Image": "ghcr.io/agenium-dev/agent-node:0.0.1", "Env": ["AGENT_NODE_NAME=box2"]},
])
def test_remove_refuses_a_resource_that_is_not_the_nodes_own(lab, capsys, config):
    lab.m.cmd_node_create(create_args())
    lab.fake.rows["box1"]["coolify_resource"] = "svc-other"
    lab.fake.config = config
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_remove(SimpleNamespace(node="box1", yes=True))
    assert refused.value.code == "node_resource_not_own"
    assert not [c for c in lab.fake.of("coolify") if "delete" in c["argv"]]
    assert "box1" in lab.fake.rows


def test_remove_of_a_resource_already_gone_removes_only_the_row(lab, capsys):
    lab.m.cmd_node_create(create_args())
    lab.fake.gone.add("svc-new")
    capsys.readouterr()
    lab.m.cmd_node_remove(SimpleNamespace(node="box1", yes=True))
    assert json.loads(capsys.readouterr().out)["resource_deleted"] is False
    assert not [c for c in lab.fake.of("coolify") if "delete" in c["argv"]]
    assert "box1" not in lab.fake.rows


def test_a_node_is_made_only_for_an_agent_that_lives_in_the_store(lab, monkeypatch):
    monkeypatch.setattr(lab.m, "_store_placed", lambda root: False)
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m.cmd_node_create(create_args())
    assert refused.value.code == "not_store_mode"
    assert not lab.fake.of("coolify")
