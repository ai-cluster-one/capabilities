"""A node Coolify builds from a repository: `deployment node create --repository`
drives coolify for the application and the manager for the node row. The node
of the standard image, and every verb that reaches a node once it exists, is
the manager's (`capabilities node`), tested in its own suite; deployment hands
those forms to it."""
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
        self.calls.append({"argv": list(argv), "stdin": stdin, "env": env})
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
    monkeypatch.setattr(m, "_installed_manifest", lambda name: (None, "not installed"))
    monkeypatch.setattr(m, "_records", lambda: SimpleNamespace(
        connections=lambda *a, **k: {"infra": {"enabled": True, "allow_write": True,
                                               "value": {"base_url": "https://coolify.invalid"}}}))
    monkeypatch.setattr(m.time, "sleep", lambda seconds: None)
    # The id `node create` mints for the node it makes.
    monkeypatch.setattr(m.secrets, "token_hex", lambda n: BOX1)
    return SimpleNamespace(m=m, fake=fake, root=root, node_run=node_run)


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


LENT = {"url": "https://coolify.lent.invalid", "token": "lent-secret"}


@pytest.mark.parametrize("given", ["", "{}", '{"url": "https://c.invalid"}',
                                   '{"url": "https://c.invalid/api/v1", "token": "t"}',
                                   '{"url": "https://c.invalid", "token": "a b"}'])
def test_a_lent_connection_is_a_url_and_a_token_on_stdin(lab, monkeypatch, given):
    monkeypatch.setattr(lab.m.sys, "stdin", io.StringIO(given))
    with pytest.raises(lab.m.NodeFailure) as refused:
        lab.m._node_lend()
    assert refused.value.code == "input" and "a b" not in refused.value.message


FORWARDED = [
    (["node", "image", "--json"], ["node", "image", "--json"]),
    (["node", "create", "box1", "--project", "prj-uuid", "--server", "srv-uuid",
      "--environment", "test", "--coolify-stdin"],
     ["node", "create", "--project=prj-uuid", "--server=srv-uuid", "--environment=test",
      "--memory=2g", "--coolify-stdin", "--", "box1"]),
    (["node", "enrol", "box1"], ["node", "enrol", "--services=", "--wait=300", "--", "box1"]),
    (["node", "add", "box1", "--server", "srv-uuid", "--version", "0.0.2", "--events",
      "--coolify-stdin"],
     ["node", "add", "--server=srv-uuid", "--version=0.0.2", "--memory=2g",
      "--environment=production", "--services=", "--wait=600", "--events", "--coolify-stdin",
      "--", "box1"]),
    (["node", "update", "box1", "--version", "0.0.2", "--memory", "4g"],
     ["node", "update", "--version=0.0.2", "--memory=4g", "--wait=600", "--", "box1"]),
    (["node", "remove", "box1", "--yes"], ["node", "remove", "--yes", "--", "box1"]),
    (["node", "exec", "box1", "--", "ls", "--all"], ["node", "exec", "box1", "--", "ls", "--all"]),
    (["node", "login", "box1", "whatsapp", "--connection", "main", "--events"],
     ["node", "login", "--events", "--connection=main", "--", "box1", "whatsapp"]),
]


@pytest.mark.parametrize("given, handed", FORWARDED)
def test_the_standard_node_is_handed_to_the_manager_with_every_argument_and_stdin(
        lab, monkeypatch, given, handed):
    monkeypatch.setattr(lab.m, "_gate", lambda: None)
    monkeypatch.setattr(lab.m, "_contract", lambda argv: None)
    monkeypatch.setattr(lab.m, "_manager_executable", lambda: "/manager")
    monkeypatch.setattr(lab.m.sys, "stdin", io.StringIO('{"url": "https://c.invalid"}'))
    ran = []

    def execv(path, argv):
        ran.append(argv)
        raise SystemExit(0)
    monkeypatch.setattr(lab.m.os, "execv", execv)
    try:
        with pytest.raises(SystemExit):
            lab.m.main(given)
    finally:
        lab.m._EVENTS_ON[0] = False
    assert ran == [["/manager", *handed]]
    # A lent connection is the manager's to read.
    assert lab.m.sys.stdin.read() == '{"url": "https://c.invalid"}'
    assert not lab.fake.calls


@pytest.mark.parametrize("given, code", [
    (["node", "remove", "box1"], "confirmation_required"),
    (["node", "create", "box1", "--project", "prj-uuid", "--server", "srv-uuid",
      "--environment", "test", "--version", "0.0.1", "--docker-socket"], "input"),
    (["node", "create", "box1", "--project", "prj-uuid", "--server", "srv-uuid",
      "--environment", "test", "--version", "0.0.1", "--branch", ""], "input"),
    (["node", "login", "box1", "gh", "--events", "--target", "test"], "input"),
    (["node", "login", "box1", "gh"], "input"),
])
def test_what_stays_here_is_answered_here(lab, monkeypatch, capsys, given, code):
    monkeypatch.setattr(lab.m, "_gate", lambda: None)
    monkeypatch.setattr(lab.m, "_contract", lambda argv: None)
    monkeypatch.setattr(lab.m.os, "execv", lambda path, argv: pytest.fail("handed over"))
    try:
        with pytest.raises(SystemExit) as refused:
            lab.m.main(given)
    finally:
        lab.m._EVENTS_ON[0] = False
    assert refused.value.code == 6
    assert json.loads(capsys.readouterr().err.splitlines()[-1])["error"]["code"] == code
