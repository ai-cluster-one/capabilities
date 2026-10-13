#!/usr/bin/env python3
"""The service settings as a control panel reads them: the schema `service
schema` prints, the verdict `service validate` gives without writing, and the
document key each prompt file the settings name answers to."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
SERVICE_DIR = TESTS.parent / "service"
sys.path.insert(0, str(TESTS))
from test_outbound_actions import import_cli  # noqa: E402


def _schema_module():
    spec = importlib.util.spec_from_file_location(
        "telegram_settings_schema_test", SERVICE_DIR / "settings_schema.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


schema = _schema_module()


def rich_settings():
    """Every level a panel renders: direct users, groups with members and
    forum topics, roles, defaults and their voice block."""
    return {
        "connection": "main",
        "environment": "production",
        "assistant_name": "Helper",
        "direct_messages": {"mode": "allowed_users", "default_role": "direct_user"},
        "allowed_users": {
            "111": {"name": "Owner", "role": "supervisor", "worker_timeout": 600,
                    "profile": "telegram-claude", "context_file": "context/owner.md",
                    "delegation": {"mode": "allowed"},
                    "voice_agent": {"mode": "enabled", "provider": "gptlive",
                                    "tools": {"agent_task": True}}},
        },
        "allowed_groups": {
            "-100200": {
                "name": "Ops", "member_role": "group_member", "worker_timeout": 300,
                "context_file": "context/ops.md", "context_mode": "extend",
                "members": {"111": {"name": "Owner", "role": "supervisor",
                                    "kind": "human"},
                            "222": {"name": "Bot", "kind": "agent",
                                    "address_aliases": ["bot"]}},
                "topics": {"7": {"profile": "telegram-codex",
                                 "context_file": "context/ops_briefings.md",
                                 "context_mode": "exclusive"}},
                "authority": {"allowed_capabilities": {"tasks": {"verbs": ["list"]}}},
                "control": {"commands": ["status", "help"]},
                "triggers": ["username", "reply"],
                "gate": {"enabled": True, "mode": "shadow", "max_batch": 5,
                         "model": {"claude": "haiku"}},
            },
            "-100300": True,
        },
        "control": {"roles": {"supervisor": {"commands": "*"}}},
        "authority": {"default": {"allowed_capabilities": ["tasks"]},
                      "roles": {"supervisor": {"allowed_capabilities": "*"}}},
        "defaults": {"worker_timeout": 120, "profile": "stub", "max_parallel_jobs": 2,
                     "job_recovery": "requeue", "media_log_level": "info",
                     "voice_agent": {"profile": "stub", "history": 20,
                                     "session": {"mode": "carry"}}},
    }


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = self.root / "capabilities" / "telegram" / "service"
        self.service.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def validates(self, settings):
        schema.validate_settings(copy.deepcopy(settings), self.root, self.service)

    def test_the_schema_is_json_schema_and_takes_what_the_service_takes(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        document = schema.json_schema()
        jsonschema.Draft202012Validator.check_schema(document)
        checker = jsonschema.Draft202012Validator(document)
        seed = json.loads((SERVICE_DIR / "templates" / "settings.json").read_text())
        for settings in (seed, rich_settings()):
            self.validates(settings)
            self.assertEqual(list(checker.iter_errors(settings)), [])
        wrong = rich_settings()
        wrong["defaults"]["worker_timeout"] = 0
        with self.assertRaisesRegex(ValueError, r"^settings\.defaults\.worker_timeout: "):
            self.validates(wrong)
        self.assertFalse(checker.is_valid(wrong))
        stray = rich_settings()
        stray["allowed_groups"]["-100200"]["topics"]["7"]["role"] = "supervisor"
        with self.assertRaisesRegex(ValueError, r"topics\.7\.role: unsupported"):
            self.validates(stray)
        self.assertFalse(checker.is_valid(stray))

    def test_each_object_offers_exactly_the_keys_the_validator_accepts(self):
        document = schema.json_schema()
        defs = document["$defs"]
        self.assertEqual(set(document["properties"]), schema.TOP_LEVEL)
        self.assertEqual(set(defs["group"]["properties"]), schema.GROUP_KEYS)
        self.assertEqual(set(defs["topic"]["properties"]), schema.TOPIC_KEYS)
        self.assertEqual(set(defs["user"]["properties"]),
                         schema.PARTICIPANT_KEYS | schema.USER_KEYS)
        self.assertEqual(set(defs["member"]["properties"]),
                         schema.PARTICIPANT_KEYS | schema.MEMBER_KEYS)
        self.assertEqual(set(document["properties"]["defaults"]["properties"]),
                         schema.DEFAULTS_KEYS)
        timeout = document["properties"]["defaults"]["properties"]["worker_timeout"]
        self.assertEqual((timeout["minimum"], timeout["maximum"]),
                         schema.DEFAULTS_NUMBERS["worker_timeout"][:2])

    def test_the_hints_name_keying_inheritance_and_what_needs_a_restart(self):
        document = schema.json_schema()
        props, defs = document["properties"], document["$defs"]
        self.assertEqual(props["allowed_users"]["x-keyed-by"], "telegram_user_id")
        self.assertEqual(props["allowed_users"]["x-entry-label"], "name")
        self.assertEqual(props["allowed_groups"]["x-keyed-by"], "telegram_chat_id")
        self.assertEqual(defs["group"]["properties"]["topics"]["x-keyed-by"],
                         "forum_topic_id")
        self.assertEqual(defs["group"]["properties"]["members"]["x-keyed-by"],
                         "telegram_user_id")
        self.assertEqual(defs["group"]["properties"]["worker_timeout"]["x-inherits"][0],
                         "defaults.worker_timeout")
        self.assertEqual(defs["topic"]["properties"]["profile"]["x-inherits"][0],
                         "allowed_groups.<chat_id>.profile")
        self.assertEqual(defs["topic"]["x-rights-from"], "allowed_groups.<chat_id>")
        for name in ("control", "authority", "allowed_capabilities", "capabilities"):
            self.assertNotIn(name, defs["topic"]["properties"])
        self.assertEqual(defs["group"]["properties"]["triggers"]["items"]["enum"],
                         sorted(schema.GROUP_TRIGGERS))
        applies = {key: node["x-apply"] for key, node in props.items()}
        self.assertEqual({k for k, v in applies.items() if v == "restart"},
                         set(schema.RESTART_KEYS))
        self.assertEqual({k for k, v in applies.items() if v == "reload"},
                         schema.TOP_LEVEL - set(schema.RESTART_KEYS))
        self.assertIn("context", [d["key"] for d in document["x-documents"]])

    def test_the_manifest_declarations_agree_with_the_restart_rule(self):
        cli = import_cli()
        for decl in cli.SETTINGS["keys"]:
            top = decl["key"].split(".", 1)[0]
            want = "restart" if top in schema.RESTART_KEYS else "reload"
            self.assertEqual(decl["apply"], want, decl["key"])

    def test_a_prompt_file_answers_to_the_key_the_context_verbs_use(self):
        self.assertEqual(schema.document_key("context.md"), "context")
        self.assertEqual(schema.document_key("context/Ops_Briefings.md"),
                         "context.ops-briefings")
        self.assertEqual(schema.document_key("voice-agent.md"), "voice-agent")
        named = dict(schema.named_documents(rich_settings()))
        self.assertEqual(named, {
            "settings.allowed_users.111.context_file": "context/owner.md",
            "settings.allowed_groups.-100200.context_file": "context/ops.md",
            "settings.allowed_groups.-100200.topics.7.context_file":
                "context/ops_briefings.md",
        })


class ValidateTest(unittest.TestCase):
    """`service validate` against a real file-backed project envelope."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.envelope = self.root / "capabilities"
        self.service = self.envelope / "telegram" / "service"
        self.service.mkdir(parents=True)
        self.settings_file = self.service / "settings.json"
        self.settings_file.write_text(json.dumps(rich_settings(), indent=2) + "\n")
        self.cli = import_cli()
        self.cli._RECORDS = None

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.cli, "_gate", lambda: None), \
                mock.patch.object(self.cli, "_project_root", lambda: self.root), \
                mock.patch.object(self.cli, "_project_capabilities_dir",
                                  lambda root: self.envelope), \
                mock.patch.object(self.cli, "_CONFIG_HOME", Path(self.tmp.name) / "config"), \
                mock.patch.object(self.cli, "_state_dir",
                                  lambda: Path(self.tmp.name) / "state"), \
                mock.patch.object(sys, "argv", ["telegram", *argv]), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                self.cli.main()
                code = 0
            except SystemExit as stopped:
                code = stopped.code or 0
        self.cli._RECORDS = None
        return code, out.getvalue(), err.getvalue()

    def test_schema_prints_the_json_schema(self):
        code, out, _ = self.run_cli(["service", "schema"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), schema.json_schema())

    def test_a_wrong_worker_timeout_is_refused_with_its_path_and_nothing_changes(self):
        before = self.settings_file.read_bytes()
        proposed = rich_settings()
        proposed["defaults"]["worker_timeout"] = 0
        code, out, err = self.run_cli(["service", "validate"], json.dumps(proposed))
        self.assertEqual(code, 6)
        self.assertEqual(out, "")
        error = json.loads(err.splitlines()[-1])["error"]
        self.assertEqual(error["code"], "service_settings_invalid")
        self.assertEqual(error["message"],
                         "settings.defaults.worker_timeout: must be between 0.01 and 3600")
        code, _, err = self.run_cli(
            ["service", "validate", "--key", "allowed_groups.-100200.worker_timeout"], "0")
        self.assertEqual(code, 6)
        self.assertEqual(json.loads(err.splitlines()[-1])["error"]["message"],
                         "settings.allowed_groups.-100200.worker_timeout: "
                         "must be between 1 and 3600")
        self.assertEqual(self.settings_file.read_bytes(), before)

    def test_an_accepted_change_says_whether_a_reload_takes_it(self):
        before = self.settings_file.read_bytes()
        code, out, _ = self.run_cli(["service", "validate", "--key", "defaults"],
                                    json.dumps({"worker_timeout": 90}))
        self.assertEqual(code, 0)
        verdict = json.loads(out)
        self.assertEqual((verdict["ok"], verdict["changed"], verdict["apply"]),
                         (True, ["defaults"], "reload"))
        code, out, _ = self.run_cli(["service", "validate", "--key", "connection"],
                                    json.dumps("other"))
        verdict = json.loads(out)
        self.assertEqual((verdict["apply"], verdict["restart"]), ("restart", ["connection"]))
        code, out, _ = self.run_cli(["service", "validate"],
                                    self.settings_file.read_text())
        self.assertEqual(json.loads(out)["changed"], [])
        self.assertEqual(self.settings_file.read_bytes(), before)

    def test_each_named_prompt_file_comes_with_its_context_key(self):
        (self.service / "context").mkdir()
        (self.service / "context" / "ops.md").write_text("ops prose\n")
        code, out, _ = self.run_cli(["service", "validate"], self.settings_file.read_text())
        self.assertEqual(code, 0)
        documents = {d["setting"]: d for d in json.loads(out)["documents"]}
        group = documents["settings.allowed_groups.-100200.context_file"]
        self.assertEqual((group["key"], group["context_verbs"], group["exists"]),
                         ("context.ops", True, True))
        topic = documents["settings.allowed_groups.-100200.topics.7.context_file"]
        self.assertEqual((topic["key"], topic["exists"]), ("context.ops-briefings", False))

    def test_input_that_is_not_a_document_is_refused(self):
        code, _, err = self.run_cli(["service", "validate"], "[1, 2]")
        self.assertEqual(code, 6)
        self.assertEqual(json.loads(err.splitlines()[-1])["error"]["code"], "input")
        code, _, err = self.run_cli(["service", "validate"], "{nope")
        self.assertEqual(code, 6)


if __name__ == "__main__":
    unittest.main()
