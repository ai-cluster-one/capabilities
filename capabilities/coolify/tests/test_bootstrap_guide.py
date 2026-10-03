#!/usr/bin/env python3
"""Tests for the `bootstrap` guide: it ships, it is on the menu, and it keeps
the steps and the secret rules a setup session relies on.

Run with: uv run --with httpx --with pytest pytest capabilities/coolify/tests/test_bootstrap_guide.py
"""

import argparse
import re
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_capability = Path(__file__).resolve().parents[1]
_coolify_path = next((path for path in (
    _capability / "bin" / "coolify", _capability / "coolify")
    if path.is_file()), _capability / "bin" / "coolify")
coolify_module = types.ModuleType("coolify_bootstrap_guide")
coolify_module.__file__ = str(_coolify_path)
exec(_coolify_path.read_text(), coolify_module.__dict__)
sys.modules["coolify_bootstrap_guide"] = coolify_module

GUIDE = _capability / "guides" / "bootstrap.md"


def _steps() -> dict[int, str]:
    text = GUIDE.read_text()
    parts = re.split(r"^## (\d)\. ", text, flags=re.MULTILINE)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts), 2)}


def test_the_guide_is_on_the_menu_with_a_preview():
    entry = next(e for e in coolify_module._guide_menu() if e["topic"] == "bootstrap")
    assert entry["command"] == "coolify guide bootstrap"
    assert entry["preview"].startswith("This guide takes a setup session")


def test_the_guide_names_the_version_it_was_proven_against():
    text = GUIDE.read_text()
    assert "Proven against Coolify 4.3.23" in text
    assert "recheck each step marked *internal*" in text


def test_the_guide_has_the_eight_steps_in_order_each_with_a_check():
    steps = _steps()
    assert sorted(steps) == list(range(1, 9))
    for number, body in steps.items():
        assert "Check" in body, f"step {number} has no check"


def test_each_pitfall_sits_at_its_step():
    steps = _steps()
    assert "seeder fails silently" in steps[1]
    assert "/data/coolify/source/.env" in steps[1]
    assert "undocumented" in steps[2] and "POST only" in steps[2]
    assert "--token-stdin --global --default" in steps[2]
    assert "capabilities set coolify grant <name> '{\"enabled\": true}'" in steps[2]
    assert "--project" not in steps[2]
    assert "resets the instance setting every time Coolify starts" in steps[3]
    assert "ufw" in steps[4] and "DOCKER-USER" in steps[4]
    assert "share one Let's Encrypt rate limit" in steps[4]
    assert "git-shell" in steps[5] and "uploadpack.allowReachableSHA1InWant" in steps[5]
    assert "without verifying the host key" in steps[5]
    assert "hostnossl all all all reject" in steps[6]
    assert "127.0.0.1" in steps[7]
    assert "never by sourcing" in GUIDE.read_text() and "`|`" in steps[8]


def test_step_six_ends_with_the_store_pointer():
    paragraphs = [p for p in _steps()[6].strip().split("\n\n") if p.strip()]
    assert "`capabilities store set`" in paragraphs[-1]


def test_no_secret_is_put_on_a_command_line_by_the_guide():
    text = GUIDE.read_text()
    assert "--password " not in text
    assert "--token " not in text
    assert not re.search(r"^\s*(source|\.) \S*credentials", text, flags=re.MULTILINE)


def _guide_commands() -> list[str]:
    """Every `coolify ...` command the guide gives, in code and inline."""
    text = GUIDE.read_text()
    found = []
    for match in re.finditer(r"(?:^|`|\|\s)coolify ((?:--|[a-z])[^`\n#]*)", text, flags=re.MULTILINE):
        command = match.group(1).strip()
        if command.startswith(("php ", "-bootstrap")) or command == "connect":
            continue  # not a command: a path, or the verb named in prose
        found.append(command)
    return found


class _Parsed(Exception):
    pass


def _parses(command: str) -> int:
    """0 when the CLI's own parser accepts the command, else argparse's exit."""
    words = re.sub(r"<[^>]+>", "x", command).split()
    words = [w.strip('"') for w in words]
    if words[0] == "connections":
        return 0 if len(words) == 1 else 2  # a contract verb, taking no arguments
    real = argparse.ArgumentParser.parse_args

    def parse_then_stop(self, args=None, namespace=None):
        real(self, args, namespace)
        raise _Parsed()

    with (
        patch.object(sys, "argv", ["coolify", *words]),
        patch.object(coolify_module, "_gate", lambda: None),
        patch.object(argparse.ArgumentParser, "parse_args", parse_then_stop),
        patch.object(sys, "stderr", open("/dev/null", "w")),
    ):
        try:
            coolify_module.main()
        except _Parsed:
            return 0
        except SystemExit as exc:
            return exc.code
    return 0


def test_the_guide_gives_commands_to_check():
    commands = _guide_commands()
    assert any(c.startswith("connect ") for c in commands)
    assert any(c.startswith("--connection ") and c.endswith("doctor") for c in commands)
    assert any(c.startswith("database create ") for c in commands)


@pytest.mark.parametrize("command", _guide_commands())
def test_every_guide_command_is_accepted_by_the_parser(command):
    """A flag in the wrong place - `doctor --connection x` - fails here."""
    assert _parses(command) == 0, command


def test_the_parser_check_catches_a_misplaced_connection_flag():
    assert _parses("doctor --connection x") == 2
    assert _parses("--connection x doctor") == 0


def test_the_help_gives_connection_before_the_verb_in_connect():
    help_text = coolify_module.__doc__ or ""
    assert "`coolify --connection <name> doctor`" in help_text
    assert "`coolify doctor --connection" not in help_text


def test_step_four_needs_root_ssh_alone():
    """Step 4 closes the direct ports on the server, serves HTTPS on an
    sslip.io name, and keeps one act outside the server: the fallback domain."""
    step = _steps()[4]
    assert "cloud provider" not in step and "Point a DNS A record" not in step
    assert "coolify.<ip-with-dashes>.sslip.io" in step
    assert "Let's Encrypt" in step
    # the fallback, and that it is the only thing outside the server
    assert "a domain the owner points at the server" in step
    assert "the one act in this guide that needs anything outside the server" in step


def test_step_four_closes_the_three_ports_in_docker_user():
    step = _steps()[4]
    assert "iptables -C DOCKER-USER -j COOLIFY-DIRECT 2>/dev/null || iptables -I DOCKER-USER 1 -j COOLIFY-DIRECT" in step
    drops = [line for line in step.splitlines()
             if line.startswith("iptables -A COOLIFY-DIRECT") and line.endswith("-j DROP")]
    assert len(drops) == 2
    for line in drops:
        assert '-i "$IF"' in line, "only the public interface"
        assert "--ctstate NEW" in line, "established and related traffic keeps passing"
        assert "--ctorigdstport" in line, "the published port, before Docker rewrites it"
    assert "--ctorigdstport 8000 " in drops[0] and "--ctorigdstport 6001:6002 " in drops[1]
    assert "iptables -A COOLIFY-DIRECT -j RETURN" in step
    v6_drops = [line.strip() for line in step.splitlines()
                if line.strip().startswith("ip6tables -A COOLIFY-DIRECT") and line.endswith("-j DROP")]
    assert [line.replace("ip6tables", "iptables") for line in v6_drops] == drops, "IPv6 gets the same drops"
    assert "ip6tables -C DOCKER-USER -j COOLIFY-DIRECT 2>/dev/null || ip6tables -I DOCKER-USER 1 -j COOLIFY-DIRECT" in step
    assert "ip6tables -C INPUT -j COOLIFY-DIRECT 2>/dev/null || ip6tables -I INPUT 1 -j COOLIFY-DIRECT" in step
    assert "ends in `INPUT` without passing `DOCKER-USER`" in step, "the guide says why INPUT is hooked for IPv6"


def test_step_four_persists_the_rules_through_a_unit_after_docker():
    step = _steps()[4]
    for line in ("After=docker.service", "PartOf=docker.service",
                 "WantedBy=docker.service",
                 "ExecStart=/usr/local/sbin/coolify-close-direct-ports",
                 "systemctl enable coolify-close-direct-ports.service",
                 "systemctl restart coolify-close-direct-ports.service"):
        assert line in step
    assert "`iptables-persistent` is not used" in step
    assert "reboot" in step


def test_step_four_checks_from_outside_and_through_the_proxy():
    step = _steps()[4]
    assert "for port in 8000 6001 6002; do curl" in step
    assert "http://[<server-ipv6>]:$port/" in step
    assert "curl -fsS https://<domain>/api/health" in step
    assert "curl -fsS http://127.0.0.1:8000/api/health" in step
    assert "openssl x509 -noout -issuer -dates" in step
    assert "coolify --connection <name> doctor" in step


def test_step_four_re_pairs_the_machine_connection_over_https():
    assert ("coolify connect <name> --url https://<domain> --token-env <KEY> "
            "--global --default") in _steps()[4]
