"""The Teams section of the composed context, read from the store at composition.

While `teams` is enabled and the agent is in a team, a top-level Teams section
stands ahead of the agent's own doctrine. `teams section` prints it and the
teams declaration names that command for the standing place, so the manager
runs it and knows nothing of what it says; for a teams payload installed before
it declared the command, the manager composes the same section itself. It is: the lines that say
the teams bind it and its own doctrine wins on conflict, each team under a
level-2 heading with its line, and for a team in always mode its enabled
always documents whole under level-3 headings, their own headings shifted
below them, and its enabled on-request documents as one level-3 group. The
`teams` entry in the capability list keeps the ordinary form. The section
carries nothing that changes between sessions, and an agent with teams
disabled gets none of it. The module starts a throwaway PostgreSQL when
`initdb` and `pg_ctl` are on PATH and psycopg is importable, and skips
otherwise.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MANAGER = REPO / "bin" / "capabilities"
TEAMS = REPO / "capabilities" / "teams" / "bin" / "teams"
ADMIN = "pgadmin"
SCHEMA = "tmc"
AGENT = "prj_" + "0a1b2c3d4e5f"
OTHER = "prj_" + "1b2c3d4e5f60"
STYLE = "---\ndescription: How we write.\nload: always\n---\n# Style\n\nOne line per paragraph.\n"
DEPLOY = "---\ndescription: Read before a release.\nload: on-request\n---\nRelease from main.\n"
CLEARED = ("CAPABILITIES_", "CLAUDE", "AGENTKIT_DB_")


def _manager_module():
    loader = importlib.machinery.SourceFileLoader(
        "capabilities_manager_teams_under_test", str(MANAGER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = _manager_module()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def cluster():
    pytest.importorskip("psycopg")
    for tool in ("initdb", "pg_ctl"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    root = Path(tempfile.mkdtemp(prefix="capabilities-teams-context-pg-"))
    port = _free_port()
    env = {**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}

    def run(*argv: str) -> None:
        done = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=120)
        assert done.returncode == 0, (argv, done.stdout + done.stderr)
    try:
        run("initdb", "-D", str(root / "data"), "-U", ADMIN, "-E", "UTF8",
            "--locale=en_US.UTF-8", "--auth=trust")
        with (root / "data" / "postgresql.conf").open("a") as conf:
            conf.write(f"\nlisten_addresses = '127.0.0.1'\nport = {port}\n"
                       "unix_socket_directories = ''\n")
        run("pg_ctl", "-D", str(root / "data"), "-l", str(root / "pg.log"), "-w", "-t", "60",
            "start")
        yield port
    finally:
        subprocess.run(["pg_ctl", "-D", str(root / "data"), "-m", "immediate", "stop"],
                       env=env, capture_output=True, timeout=60)
        shutil.rmtree(root, ignore_errors=True)


class Lab:
    def __init__(self, tmp: Path, port: int):
        import psycopg
        self.tmp = tmp
        database = "t_" + uuid.uuid4().hex[:12]
        with psycopg.connect(host="127.0.0.1", port=port, user=ADMIN, dbname="postgres",
                             autocommit=True) as conn:
            conn.execute(f"CREATE DATABASE {database} ENCODING 'UTF8' TEMPLATE template0")
        self.url = f"postgresql://{ADMIN}@127.0.0.1:{port}/{database}?sslmode=disable"
        self.project = tmp / "project"
        (self.project / ".git").mkdir(parents=True)
        (self.project / "capabilities").mkdir()
        (self.project / ".env.local").write_text(f"CAPABILITIES_PROJECT_ID={AGENT}\n")
        self.enable(True)
        self._snapshot()

    def enable(self, enabled: bool) -> None:
        (self.project / "capabilities" / "settings.json").write_text(json.dumps(
            {"capabilities": {"teams": {"enabled": enabled}}}))

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(CLEARED)}
        env.update({
            "HOME": str(self.tmp / "home"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "XDG_DATA_HOME": str(self.tmp / "data"),
            "CAPABILITIES_HOME": str(self.tmp / "registry"),
            "CAPABILITIES_BIN": str(self.tmp / "bin"),
            "CAPABILITIES_MANAGER_BIN": str(MANAGER),
            "AGENTKIT_DB_URL": self.url,
            "AGENTKIT_DB_SCHEMA": SCHEMA,
            "AGENT_NODE_ID": "746573742d6e",
            "USER": "tester",
        })
        return env

    def register(self, *agents: str) -> None:
        """The store holds a row only for an agent it has registered."""
        import psycopg
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute(f"SET search_path TO {SCHEMA}")
            for agent in agents:
                conn.execute("INSERT INTO agents (id, kind) VALUES (%s, 'agent') "
                             "ON CONFLICT (id) DO NOTHING", (agent,))

    def _snapshot(self, legacy: bool = False) -> None:
        """teams as install records it: its stub, its own manifest, and an
        executable that runs this tree's teams. With `legacy`, the manifest a
        teams payload carried before it declared its command."""
        manifest = json.loads(self.teams("manifest", "--json"))
        if legacy:
            manifest["contribution"] = {"markdown": manifest["contribution"]["markdown"]}
        entry = self.tmp / "registry" / "teams"
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "teams").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{TEAMS}" "$@"\n')
        (entry / "teams").chmod(0o755)
        (entry / "stub").write_text(f"{manifest['summary']} Run `teams help`.\n")
        (entry / "manifest.json").write_text(json.dumps(manifest))

    def teams(self, *args: str, stdin: str | None = None) -> str:
        done = subprocess.run([sys.executable, str(TEAMS), *args], cwd=self.project,
                              env=self.env(), input=stdin, text=True, capture_output=True,
                              timeout=180)
        assert done.returncode == 0, done.stderr
        return done.stdout

    def fragment(self) -> str:
        return self.manager("context", "--fragment")

    def build(self) -> str:
        return self.manager("build", "--print")

    def manager(self, *args: str) -> str:
        done = subprocess.run([sys.executable, str(MANAGER), *args],
                              cwd=self.project, env=self.env(), text=True,
                              capture_output=True, timeout=180, stdin=subprocess.DEVNULL)
        assert done.returncode == 0, done.stderr
        return done.stdout


@pytest.fixture()
def lab(tmp_path, cluster):
    return Lab(tmp_path, cluster)


def _section(fragment: str, name: str = "teams") -> str | None:
    """One capability's entry in the capability list."""
    block = fragment.split(M.CONTEXT_BEGIN, 1)[1].split(M.CONTEXT_END, 1)[0]
    parts = re.split(r"(?m)^## (\S+)\n", block)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}.get(name)


def _teams(text: str) -> str | None:
    """The Teams section, up to the top-level heading that follows it."""
    if "# Teams\n" not in text:
        return None
    body = text.split("# Teams\n", 1)[1]
    return re.split(r"(?m)^# (?:Inline Context|Capabilities)\n", body, maxsplit=1)[0]


def _outline(text: str) -> list[str]:
    """The heading lines of `text`, outside code fences."""
    lines, fence = [], False
    for line in text.split("\n"):
        if line.startswith(("```", "~~~")):
            fence = not fence
        elif not fence and re.match(r"^#{1,6} ", line):
            lines.append(line)
    return lines


def _seed(lab: Lab) -> None:
    lab.teams("create", "crew", "--description", "The release crew: turn to it for releases.")
    lab.teams("create", "ops", "--description", "Operations: turn to it for the servers.")
    lab.teams("create", "far", "--description", "A team this agent is not in.", "--always")
    lab.register(AGENT, OTHER)
    for team in ("crew", "ops"):
        lab.teams("members", "add", team, AGENT)
    lab.teams("members", "add", "far", OTHER)
    for team in ("crew", "ops", "far"):
        lab.teams("doc", "save", team, "style", "--from", "-", stdin=STYLE)
        lab.teams("doc", "save", team, "deploy", "--from", "-", stdin=DEPLOY)


def test_teams_is_core():
    assert "teams" in M.CORE_CAPABILITIES


def test_the_teams_entry_in_the_capability_list_is_its_stub_and_its_commands(lab):
    _seed(lab)
    entry = _section(lab.fragment())
    manifest = json.loads(lab.teams("manifest", "--json"))
    assert entry == f"\n{manifest['summary']} Run `teams help`.\n\n" \
        f"{manifest['contribution']['markdown']}\n"


def test_an_ordinary_team_is_its_line_and_how_to_read_it(lab):
    _seed(lab)
    fragment = lab.fragment()
    teams = _teams(fragment)
    assert fragment.startswith("# Teams\n\nYou are a member of the teams below, and their "
                               "rules bind you. Where a team's rule conflicts with your own "
                               "doctrine further down, your own doctrine wins.\n")
    assert ("## crew\n\nThe release crew: turn to it for releases. Read its instructions on "
            "request: `teams show crew`.\n") in teams
    assert "## ops\n\nOperations: turn to it for the servers. Read its instructions" in teams
    assert "far" not in fragment
    assert "One line per paragraph." not in fragment
    assert "###" not in teams


def test_an_always_team_composes_its_documents_under_level_three_headings(lab):
    _seed(lab)
    lab.teams("mode", "ops", "always")
    lab.teams("doc", "save", "ops", "secrets-off", "--from", "-",
              stdin="---\ndescription: Switched off.\nload: always\n---\nNever shown.\n")
    lab.teams("doc", "off", "ops", "secrets-off")
    lab.teams("doc", "save", "ops", "runbook", "--from", "-",
              stdin="---\ndescription: How we run.\nload: always\n---\nRun from main.\n\n"
                    "## Steps\n\n```sh\n# not a heading\n```\n")
    teams = _teams(lab.fragment())
    assert ("## ops\n\nOperations: turn to it for the servers.\n\n"
            "### runbook\n\nRun from main.\n\n#### Steps\n\n```sh\n# not a heading\n```\n\n"
            "### Style\n\nOne line per paragraph.\n\n"
            "### On request\n\n- `teams doc show ops deploy` - Read before a release.\n") in teams
    assert "Never shown." not in teams and "secrets-off" not in teams
    assert _outline(teams) == ["## crew", "## ops", "### runbook", "#### Steps", "### Style",
                               "### On request"]


def test_the_section_is_the_same_in_every_session_and_follows_the_store(lab):
    _seed(lab)
    lab.teams("mode", "crew", "always")
    first = lab.fragment()
    assert lab.fragment() == first
    assert not re.search(r"\b20\d\d-\d\d-\d\d|Rendered|tokens", _teams(first))
    lab.teams("doc", "save", "crew", "style", "--from", "-",
              stdin=STYLE.replace("One line", "Each line"))
    assert "Each line per paragraph." in _teams(lab.fragment())


def test_an_agent_in_no_team_has_no_teams_section(lab):
    lab.teams("create", "crew", "--description", "The release crew.")
    fragment = lab.fragment()
    assert _teams(fragment) is None and "# Teams" not in fragment
    assert _section(fragment).strip().startswith("Teams of agents")


def test_teams_disabled_removes_its_section_and_entry_and_nothing_else(lab):
    _seed(lab)
    lab.teams("mode", "ops", "always")
    other = lab.tmp / "registry" / "alpha"
    other.mkdir(parents=True)
    (other / "alpha").write_text("#!/bin/sh\n")
    (other / "stub").write_text("Alpha does one thing. Run `alpha help`.\n")

    def settings(teams: bool) -> None:
        (lab.project / "capabilities" / "settings.json").write_text(json.dumps(
            {"capabilities": {"teams": {"enabled": teams}, "alpha": {"enabled": True}}}))
    settings(True)
    on = lab.fragment()
    settings(False)
    off = lab.fragment()
    assert "## alpha\n\nAlpha does one thing." in off
    assert "The release crew" not in off and _section(off) is None
    entry = "## teams\n" + _section(on)
    section, rest = on.split("# Capabilities\n", 1)
    assert section.startswith("# Teams\n")
    assert off == "# Capabilities\n" + rest.replace(entry, "")


def test_a_command_the_section_prints_quotes_a_name_that_holds_a_space(lab):
    lab.teams("create", "Release crew", "--description", "The release crew.", "--always")
    lab.register(AGENT)
    lab.teams("members", "add", "Release crew", AGENT)
    lab.teams("doc", "save", "Release crew", "deploy rules", "--from", "-", stdin=DEPLOY)
    lab.teams("create", "ops", "--description", "Operations.")
    lab.teams("members", "add", "ops", AGENT)
    teams = _teams(lab.fragment())
    assert "- `teams doc show 'Release crew' 'deploy rules'` - Read before a release." in teams
    assert "Read its instructions on request: `teams show ops`." in teams
    lab.teams("rename", "Release crew", "Releases")
    teams = _teams(lab.fragment())
    assert "## Releases\n\nThe release crew.\n" in teams
    assert "`teams doc show Releases 'deploy rules'`" in teams


def test_build_places_the_teams_section_before_the_agents_own_doctrine(lab):
    _seed(lab)
    lab.teams("mode", "ops", "always")
    mission = lab.project / "context" / "identity" / "MISSION.md"
    mission.parent.mkdir(parents=True)
    stored = ("---\ntitle: Mission\ndescription: Why.\nload: inline\norder: 1\n---\n"
              "# Mission\n\nShip it.\n\n## Scope\n\n```md\n# kept\n```\n")
    mission.write_text(stored)
    built = lab.build()
    order = ["# Guide Menu", "# Teams", "## crew", "## ops", "### Style", "### On request",
             "# Inline Context", "## Source: `context/identity/MISSION.md`", "### Mission",
             "#### Scope", "# Capabilities", "## teams"]
    outline = _outline(built)
    positions = [outline.index(heading) for heading in order]
    assert positions == sorted(positions)
    assert "```md\n# kept\n```" in built
    assert mission.read_text() == stored
    style = lab.teams("doc", "show", "ops", "style")
    assert style == STYLE
    lab.enable(False)
    assert "# Teams" not in lab.build()


def test_teams_declares_its_section_as_a_standing_command(lab):
    contribution = json.loads(lab.teams("manifest", "--json"))["contribution"]
    assert contribution["command"] == ["section"] and contribution["place"] == "standing"
    assert M._validate_manifest("teams", json.loads(lab.teams("manifest", "--json"))) == []
    assert lab.teams("section") == ""
    _seed(lab)
    section = lab.teams("section")
    assert section.startswith("# Teams\n\n")
    assert lab.fragment().startswith(section + "\n# Capabilities\n")


def test_a_teams_payload_that_declares_no_command_keeps_its_section(lab):
    # A manager carrying this change beside a teams installed before it: the
    # manager composes the section itself, the same in both forms.
    _seed(lab)
    lab.teams("mode", "ops", "always")
    lab.teams("doc", "save", "ops", "runbook", "--from", "-",
              stdin="---\ndescription: How we run.\nload: always\n---\nRun.\n\n## Steps\n")
    declared_fragment, declared_build = lab.fragment(), lab.build()
    lab._snapshot(legacy=True)
    assert lab.fragment() == declared_fragment
    assert lab.build() == declared_build
    assert "# Teams\n" in declared_build


def test_a_teams_section_that_fails_costs_one_line_in_its_place(lab):
    _seed(lab)
    entry = lab.tmp / "registry" / "teams" / "teams"
    entry.write_text("#!/bin/sh\nexit 5\n")
    built = lab.build()
    assert ("---\n\nThe context contribution of `teams` is unavailable: `teams section` "
            "exited 5; `teams doctor` names the repair.\n\n---\n\n# Inline Context") in built
    assert "## crew" not in built and "## teams\n" in built


def test_shifting_moves_headings_and_never_code():
    text = ("Title\n=====\n\n# One\n\n## Two\n\n##### Five\n\n```sh\n# shell\n```\n\n"
            "    # indented code\n\n- item\n---\n\nhash#tag")
    assert M._shift_headings(text, 2) == (
        "### Title\n\n### One\n\n#### Two\n\n###### Five\n\n```sh\n# shell\n```\n\n"
        "    # indented code\n\n- item\n---\n\nhash#tag")


def test_init_without_a_store_says_nothing_about_teams(tmp_path):
    # The image-build path: a store-gated agent initialised where no database
    # is configured writes its context offline, and teams has no part in it.
    project = tmp_path / "agent"
    (project / ".git").mkdir(parents=True)
    (project / "capabilities").mkdir()
    (project / "capabilities" / "project.json").write_text(json.dumps(
        {"schema": "capabilities.project.v1", "slug": "agent", "id": AGENT,
         "records": "store"}))
    doc = project / "context" / "identity" / "AGENT.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\ntitle: Agent\ndescription: Who.\nload: inline\norder: 10\n---\n"
                   "# Agent\n\nBody.\n")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(CLEARED)}
    env.update({"HOME": str(tmp_path / "home"),
                "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "XDG_DATA_HOME": str(tmp_path / "data"),
                "XDG_CACHE_HOME": str(tmp_path / "cache")})
    result = subprocess.run([sys.executable, str(MANAGER), "init", "--claude", "--id", AGENT],
                            cwd=project, env=env, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr
    warnings = json.loads(result.stdout)["hooks"]["build"]["warnings"]
    assert not [warning for warning in warnings if "teams" in warning], warnings
