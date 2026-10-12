#!/usr/bin/env python3
"""Harness profiles: the bundled two, the prefix, the store and the bundle as
layers, fit, the harness table beside the register's, and who may save.

The store cases read TELEGRAM_TEST_DSN, a throwaway database's URL, and skip
without one; each binds a schema of its own. Every case that reads or saves a
profile needs callva-harness-runner importable, as `service/profiles.py` pins
it, and skips without it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import CLI_PATH  # noqa: E402
from test_job_register import STORE_DSN, jobs, project_id, store_setting  # noqa: E402
from test_outbound_actions import import_cli  # noqa: E402

SERVICE_DIR = Path(__file__).resolve().parents[1] / "service"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profiles = _module("telegram_service_profiles_test", SERVICE_DIR / "profiles.py")

try:
    from callva import harness_runner as lib
except ImportError:
    lib = None

needs_runner = unittest.skipUnless(lib is not None, "callva-harness-runner is not importable")
needs_store = unittest.skipUnless(STORE_DSN, "TELEGRAM_TEST_DSN is unset")

CLAUDE = {"harness": "claude", "model": "opus", "permission_mode": "bypassPermissions",
          "setting_sources": ["user", "project", "local"], "timeout_seconds": 86400}
CODEX = {"harness": "codex", "model": "sol", "approval_policy": "never",
         "codex_config": {"sandbox_mode": "danger-full-access"},
         "bypass_hook_trust": True, "timeout_seconds": 86400}


def toml(spec: dict) -> str:
    return lib.profile_text(spec)


class Bundled(unittest.TestCase):

    @needs_runner
    def test_the_bundled_profiles_carry_exactly_their_knobs(self):
        for name, knobs in (("telegram-claude", CLAUDE), ("telegram-codex", CODEX)):
            path = profiles.bundled_file(name)
            found = lib.ProfileFile(name, "shipped", str(path), path.read_text()).knobs()
            self.assertEqual(found, knobs)
            lib.Profile.from_dict(found)

    def test_a_job_on_a_bundled_profile_has_a_one_day_deadline(self):
        self.assertEqual({CLAUDE["timeout_seconds"], CODEX["timeout_seconds"]}, {86400})

    def test_both_bundled_profiles_fit_with_no_warning(self):
        for knobs in (CLAUDE, CODEX):
            self.assertEqual(profiles.fit_problems(knobs), [])
            self.assertEqual(profiles.fit_warnings(knobs), [])

    def test_the_runner_pin_is_one(self):
        header = (SERVICE_DIR / "profiles.py").read_text().split("# ///")[1]
        self.assertIn(f'"{profiles.RUNNER_PIN}"', header)
        cli_header = Path(CLI_PATH).read_text().split("# ///")[1]
        self.assertNotIn("callva-harness-runner", cli_header)


class Fit(unittest.TestCase):
    """Each rule refuses its misfit naming the knob."""

    def assertRefuses(self, knobs: dict, knob: str):
        problems = profiles.fit_problems(knobs)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(knob, problems[0])

    def test_a_harness_other_than_claude_or_codex(self):
        self.assertRefuses({**CLAUDE, "harness": "gemini"}, "harness")

    def test_an_output_schema(self):
        self.assertRefuses({**CLAUDE, "output_schema": {"type": "object"}}, "output_schema")

    def test_claude_without_bypass_permissions(self):
        self.assertRefuses({**CLAUDE, "permission_mode": "acceptEdits"}, "permission_mode")

    def test_codex_asking_for_approval(self):
        self.assertRefuses({**CODEX, "approval_policy": "on-request"}, "approval_policy")

    def test_no_timeout(self):
        for knobs in (CLAUDE, CODEX):
            self.assertRefuses({k: v for k, v in knobs.items() if k != "timeout_seconds"},
                               "timeout_seconds")

    def test_whatsapps_rules_are_warnings_here(self):
        for knobs, knob in (({**CLAUDE, "tools": ["Read"]}, "tools"),
                            ({**CLAUDE, "disallowed_tools": ["Bash"]}, "disallowed_tools"),
                            ({**CLAUDE, "settings": {"sandbox": {"enabled": True}}},
                             "sandbox"),
                            ({**CODEX, "codex_config": {"sandbox_mode": "read-only"}},
                             "sandbox_mode"),
                            ({**CODEX, "codex_config": {"sandbox_mode": "danger-full-access",
                                                        "permission_profile": "x"}},
                             "permissions profile")):
            self.assertEqual(profiles.fit_problems(knobs), [])
            warnings = profiles.fit_warnings(knobs)
            self.assertEqual(len(warnings), 1, warnings)
            self.assertIn(knob, warnings[0])


@needs_runner
class Implicit(unittest.TestCase):
    """Today's worker settings laid over `telegram-<worker>`: one function,
    which the migration that later names profiles maps them with."""

    def knobs(self, worker, cfg):
        return profiles.implicit(worker, cfg, [], None)["knobs"]

    def test_nothing_set_runs_the_bundled_profile_as_it_is(self):
        self.assertEqual(self.knobs("claude", {}), CLAUDE)
        self.assertEqual(self.knobs("codex", {"model": None, "reasoning_effort": "default",
                                              "service_tier": None}), CODEX)

    def test_a_model(self):
        self.assertEqual(self.knobs("claude", {"model": "sonnet"}), {**CLAUDE, "model": "sonnet"})
        self.assertEqual(self.knobs("codex", {"model": "gpt-5.5"}), {**CODEX, "model": "gpt-5.5"})

    def test_claude_effort(self):
        self.assertEqual(self.knobs("claude", {"effort": "high"}), {**CLAUDE, "effort": "high"})

    def test_codex_reasoning_effort_is_effort(self):
        self.assertEqual(self.knobs("codex", {"reasoning_effort": "xhigh"}),
                         {**CODEX, "effort": "xhigh"})

    def test_codex_service_tier_fast_means_priority(self):
        self.assertEqual(self.knobs("codex", {"service_tier": "fast"}),
                         {**CODEX, "service_tier": "priority"})
        self.assertEqual(self.knobs("codex", {"service_tier": "priority"}),
                         {**CODEX, "service_tier": "priority"})

    def test_claude_context_tools(self):
        self.assertEqual(self.knobs("claude", {"context": {"tools": ["Read", "Grep"]}}),
                         {**CLAUDE, "tools": ["Read", "Grep"]})
        self.assertEqual(self.knobs("claude", {"context": {"tools": []}}),
                         {**CLAUDE, "tools": []})

    def test_claude_context_without_mcp(self):
        self.assertEqual(self.knobs("claude", {"context": {"mcp": False}}),
                         {**CLAUDE, "strict_mcp": True, "mcp_config": {"mcpServers": {}}})
        self.assertEqual(self.knobs("claude", {"context": {"mcp": True}}), CLAUDE)

    def test_a_job_row_runs_its_engine_with_its_model(self):
        found = profiles.implicit("codex", {"model": "gpt-pinned"}, [], None)
        self.assertEqual((found["name"], found["layer"], found["knobs"]["model"]),
                         ("telegram-codex", "bundled", "gpt-pinned"))
        self.assertEqual(found["overlay"], {"model": "gpt-pinned"})

    def test_a_worker_that_is_not_a_harness_has_no_profile(self):
        with self.assertRaises(profiles.Refusal) as caught:
            profiles.implicit("stub", {}, [], None)
        self.assertEqual(caught.exception.code, "profile_name_refused")

    def test_the_positions_settings_reach(self):
        settings = {
            "defaults": {"profile": "telegram-claude",
                         "voice_agent": {"profile": "telegram-voice"}},
            "allowed_users": {"11": {"profile": "telegram-direct"}, "12": {}},
            "allowed_groups": {"-21": {"profile": "telegram-group",
                                       "topics": {"5": {"profile": "telegram-topic"},
                                                  "6": {}}},
                               "-22": True}}
        self.assertEqual([(r["position"], r["profile"])
                          for r in profiles.reached(settings, {})],
                         [("settings.defaults.profile", "telegram-claude"),
                          ("settings.defaults.voice_agent.profile", "telegram-voice"),
                          ("settings.allowed_users.11.profile", "telegram-direct"),
                          ("settings.allowed_groups.-21.profile", "telegram-group"),
                          ("settings.allowed_groups.-21.topics.5.profile",
                           "telegram-topic")])
        # The environment's default wins over the settings', as the daemon reads it.
        self.assertEqual(profiles.reached(settings, {"TELEGRAM_SERVICE_PROFILE":
                                                     "telegram-codex"})[0],
                         {"position": "TELEGRAM_SERVICE_PROFILE",
                          "profile": "telegram-codex"})
        self.assertEqual(profiles.reached({"defaults": {"profile": "stub"}}, {}),
                         [{"position": "settings.defaults.profile", "profile": "stub"}])
        self.assertEqual(profiles.reached({}, {}), [])

    def test_the_default_is_the_environments_then_the_settings(self):
        self.assertEqual(profiles.default_profile({}, {}), (None, "default"))
        self.assertEqual(profiles.default_profile({"defaults": {"profile": "telegram-x"}}, {}),
                         ("telegram-x", "settings.defaults.profile"))
        self.assertEqual(profiles.default_profile({"defaults": {"profile": "telegram-x"}},
                                                  {"TELEGRAM_SERVICE_PROFILE": "stub"}),
                         ("stub", "TELEGRAM_SERVICE_PROFILE"))

    @needs_runner
    def test_the_stub_resolves_to_nothing_and_a_name_without_the_prefix_is_named(self):
        self.assertIsNone(profiles.turn("stub", [], None))
        [problem] = profiles.check({"allowed_users": {"11": {"profile": "claude-act"}}},
                                   {}, [], None)
        self.assertEqual((problem["position"], problem["code"]),
                         ("settings.allowed_users.11.profile", "profile_name_refused"))
        self.assertIn("settings.allowed_users.11.profile names claude-act",
                      profiles.problems_sentence([problem]))

    def test_a_check_of_the_seeded_settings_finds_nothing(self):
        seed = json.loads((SERVICE_DIR / "templates" / "settings.json").read_text())
        self.assertEqual(profiles.check(seed, {}, [], None), [])


class Probe(unittest.TestCase):
    """The doctor's verdict for each pair, read off the runner's Result."""

    @needs_runner
    def test_each_ending_is_the_verdict_the_doctor_names(self):
        def failing(kind, message):
            def run(prompt, profile, cwd, **_kwargs):
                return lib.Result(ok=False, harness=profile.harness,
                                  failure=lib.Failure(lib.FailureKind(kind), message))
            return run

        for kind, verdict in (("model_refused", "model_refused"),
                              ("quota", "quota_exhausted"),
                              ("not_authenticated", "not_authenticated"),
                              ("binary_missing", "binary_missing"),
                              ("timeout", "timed_out"),
                              ("incompatible_harness", "incompatible_harness"),
                              ("error", "failed"), ("crash", "failed")):
            with self.subTest(kind), tempfile.TemporaryDirectory() as tmp:
                [row] = profiles.probe([{"profile": "telegram-codex",
                                         "declared_at": ["settings.defaults.profile"]}],
                                       2, [], None, tmp, run=failing(kind, f"said {kind}"))
                self.assertEqual((row["ok"], row["verdict"], row["reason"]),
                                 (False, verdict, f"said {kind}"))
                self.assertEqual((row["profile"], row["layer"], row["worker"], row["model"],
                                  row["declared_at"]),
                                 ("telegram-codex", "bundled", "codex", "sol",
                                  ["settings.defaults.profile"]))
                self.assertEqual(row["notice"] is not None, verdict == "model_refused")

    @needs_runner
    def test_a_probe_is_held_to_the_timeout_it_is_given(self):
        seen = []

        def run(prompt, profile, cwd, **_kwargs):
            seen.append(profile.timeout_seconds)
            return lib.Result(ok=True, harness=profile.harness, answer="OK")

        with tempfile.TemporaryDirectory() as tmp:
            rows = profiles.probe([{"profile": "telegram-claude"}, {"profile": "stub"},
                                   {"profile": "telegram-nowhere"}], 7, [], None, tmp,
                                  run=run)
        self.assertEqual(seen, [7.0])
        self.assertEqual([r["verdict"] for r in rows], ["accepted", "in_process", "failed"])
        self.assertIn("'telegram-nowhere'", rows[2]["reason"])


@needs_runner
@needs_store
class Store(unittest.TestCase):
    """Resolution and saving against the store the register tests use."""

    def setUp(self):
        from capabilities_contract.db import connect, migrate
        self.setting = store_setting()
        self.conn = connect(application_name="telegram", setting=self.setting)
        self.addCleanup(self.conn.close)
        steps = profiles.store_steps()
        migrate(self.conn, steps["owner"], [tuple(s) for s in steps["steps"]],
                major=steps["major"], minor=steps["minor"])
        self.project = project_id()

    def save(self, name, spec, scope="project", scope_id=None):
        version, _created = lib.save_profile(
            self.conn, name, spec, scope=scope, schema=self.setting.schema,
            scope_id=(scope_id or self.project) if scope == "project" else None)
        self.conn.commit()
        return version

    def active(self):
        """What a lookup reads, the read then ended, as each command's own
        connection ends it."""
        found = profiles.stored(self.conn, self.project, self.setting.schema)
        self.conn.rollback()
        return found

    def resolve(self, name, **kwargs):
        return profiles.resolve(name, self.active(), self.project, **kwargs)

    def refusal(self, call, *args, **kwargs) -> "profiles.Refusal":
        with self.assertRaises(profiles.Refusal) as caught:
            call(*args, **kwargs)
        return caught.exception

    # -- resolution -----------------------------------------------------------

    def test_project_wins_over_global_which_wins_over_the_bundle(self):
        self.assertEqual(self.resolve("telegram-claude")["layer"], "bundled")
        held = self.save("telegram-claude", {**CLAUDE, "model": "sonnet"}, scope="global")
        found = self.resolve("telegram-claude")
        self.assertEqual((found["layer"], found["version"], found["knobs"]["model"]),
                         ("global", held.id, "sonnet"))
        self.assertEqual(found["shadows"], ["bundled"])
        mine = self.save("telegram-claude", {**CLAUDE, "model": "haiku"})
        found = self.resolve("telegram-claude")
        self.assertEqual((found["layer"], found["version"], found["knobs"]["model"]),
                         ("project", mine.id, "haiku"))
        self.assertEqual(found["shadows"], ["global", "bundled"])

    def test_another_projects_version_is_not_reached(self):
        self.save("telegram-claude", {**CLAUDE, "model": "haiku"}, scope_id=project_id())
        self.assertEqual(self.resolve("telegram-claude")["layer"], "bundled")

    def test_a_name_without_the_prefix_is_refused_naming_the_rule(self):
        for name in ("claude-act", "agentrooms-claude", "telegram-", "stub"):
            refused = self.refusal(self.resolve, name)
            self.assertEqual(refused.code, "profile_name_refused")
            self.assertIn("telegram-<something>", refused.message)

    def test_a_name_in_no_layer_is_refused_naming_it(self):
        refused = self.refusal(self.resolve, "telegram-nowhere")
        self.assertEqual((refused.exit_code, refused.code), (3, "profile_not_found"))
        self.assertIn("'telegram-nowhere'", refused.message)

    def test_a_refused_version_is_an_error_and_the_search_stops_there(self):
        with self.conn.cursor() as cur:
            cur.execute(f'insert into "{self.setting.schema}".harness_profiles '
                        "(scope, scope_id, name, spec, active) values "
                        "('project', %s, 'telegram-claude', %s::jsonb, true) returning id",
                        (self.project, json.dumps({**CLAUDE, "not_a_knob": 1})))
            version = str(cur.fetchone()[0])
        self.conn.commit()
        refused = self.refusal(self.resolve, "telegram-claude")
        self.assertEqual(refused.code, "profile_invalid")
        for part in ("'telegram-claude'", "layer project", version):
            self.assertIn(part, refused.message)

    def test_an_unfit_version_resolves_for_listing_and_is_refused_for_a_turn(self):
        self.save("telegram-slow", {**CLAUDE, "permission_mode": "acceptEdits"})
        self.assertEqual(self.resolve("telegram-slow", fit=False)["layer"], "project")
        refused = self.refusal(self.resolve, "telegram-slow")
        self.assertEqual(refused.code, "profile_unfit")
        self.assertIn("permission_mode", refused.message)

    def test_a_store_without_the_table_holds_no_profiles(self):
        from capabilities_contract.db import connect
        import dataclasses
        bare = dataclasses.replace(self.setting, schema=f"{self.setting.schema}_bare")
        conn = connect(application_name="telegram", setting=bare)
        self.addCleanup(conn.close)
        self.assertEqual(profiles.stored(conn, self.project, bare.schema), [])

    def test_a_turn_runs_on_the_project_version_with_its_settings_laid_over(self):
        mine = self.save("telegram-codex", {**CODEX, "model": "gpt-5.5"}, scope="global")
        found = profiles.implicit("codex", {"reasoning_effort": "high"}, self.active(),
                                  self.project)
        self.assertEqual((found["layer"], found["version"]), ("global", mine.id))
        self.assertEqual((found["knobs"]["model"], found["knobs"]["effort"]),
                         ("gpt-5.5", "high"))

    def test_a_check_names_the_position_whose_profile_does_not_fit(self):
        self.save("telegram-codex", {**CODEX, "approval_policy": "auto_review"},
                  scope="global")
        settings = {"defaults": {"profile": "telegram-codex"}}
        [problem] = profiles.check(settings, {}, self.active(), self.project)
        self.assertEqual((problem["position"], problem["profile"], problem["code"]),
                         ("settings.defaults.profile", "telegram-codex", "profile_unfit"))
        self.assertIn("approval_policy", problem["message"])
        self.assertIn("settings.defaults.profile names telegram-codex",
                      profiles.problems_sentence([problem]))

    # -- the migration's profiles ---------------------------------------------

    def test_each_knob_moves_to_a_profile_named_from_its_values(self):
        combos = [
            {"worker": "claude", "settings": {}},
            {"worker": "claude", "settings": {"model": "sonnet", "effort": "high"}},
            {"worker": "claude", "settings": {"context": {"tools": ["Read", "Bash"],
                                                          "mcp": False}}},
            {"worker": "codex", "settings": {"model": "gpt-5.5", "reasoning_effort": "high",
                                             "service_tier": "fast"}},
            {"worker": "stub", "settings": {"model": None}},
        ]
        dry = profiles.materialize(self.conn, combos, self.project, apply=False,
                                   schema=self.setting.schema)
        self.assertEqual([(one["profile"], one["created"]) for one in dry], [
            ("telegram-claude", False),
            ("telegram-claude-sonnet-high", True),
            ("telegram-claude-tools-read-bash-no-mcp", True),
            ("telegram-codex-gpt-5.5-high-priority", True),
            ("stub", False)])
        self.assertEqual([one.name for one in self.active()], [])
        done = profiles.materialize(self.conn, combos, self.project, apply=True,
                                    created_by="migration", schema=self.setting.schema)
        self.assertEqual([one["created"] for one in done], [False, True, True, True, False])
        knobs = {found["name"]: found["knobs"] for found in
                 (self.resolve(one["profile"]) for one in done[1:4])}
        self.assertEqual(knobs["telegram-claude-sonnet-high"],
                         {**CLAUDE, "model": "sonnet", "effort": "high"})
        self.assertEqual(knobs["telegram-claude-tools-read-bash-no-mcp"],
                         {**CLAUDE, "tools": ["Read", "Bash"], "strict_mcp": True,
                          "mcp_config": {"mcpServers": {}}})
        self.assertEqual(knobs["telegram-codex-gpt-5.5-high-priority"],
                         {**CODEX, "model": "gpt-5.5", "effort": "high",
                          "service_tier": "priority"})
        # A rerun finds every one as it left it and creates nothing.
        again = profiles.materialize(self.conn, combos, self.project, apply=True,
                                     schema=self.setting.schema)
        self.assertEqual([(one["profile"], one["created"]) for one in again],
                         [(one["profile"], False) for one in done])

    def test_a_derived_name_held_by_another_spec_takes_the_digest_form(self):
        self.save("telegram-claude-haiku", {**CLAUDE, "model": "haiku", "effort": "low"})
        [one] = profiles.materialize(self.conn, [{"worker": "claude",
                                                  "settings": {"model": "haiku"}}],
                                     self.project, apply=True, schema=self.setting.schema)
        self.assertRegex(one["profile"], r"^telegram-claude-[0-9a-f]{10}$")
        self.assertEqual(self.resolve("telegram-claude-haiku")["knobs"]["effort"], "low")
        self.assertEqual(self.resolve(one["profile"])["knobs"]["model"], "haiku")

    def test_a_combination_that_cannot_make_a_profile_is_refused_by_name(self):
        self.save("telegram-codex", {**CODEX, "approval_policy": "auto_review"},
                  scope="global")
        [one] = profiles.materialize(self.conn, [{"worker": "codex",
                                                  "settings": {"model": "gpt-5.5"}}],
                                     self.project, apply=True, schema=self.setting.schema)
        self.assertIsNone(one["profile"])
        self.assertIn("does not fit a telegram turn", one["error"])
        self.assertEqual([one.name for one in self.active() if one.scope == "project"], [])

    # -- list and show ----------------------------------------------------------

    def test_list_shows_every_telegram_profile_and_never_an_env_value(self):
        self.save("telegram-quiet", {**CODEX, "env_set": {"TOKEN": "s3cret"},
                                     "codex_config": {"sandbox_mode": "read-only"}})
        self.save("whatsapp-claude", CLAUDE)
        rows = profiles.listing(self.active(), self.project)
        self.assertEqual([r["name"] for r in rows],
                         ["telegram-claude", "telegram-codex", "telegram-quiet"])
        quiet = rows[2]
        self.assertEqual((quiet["layer"], quiet["harness"], quiet["timeout_seconds"],
                          quiet["ok"]), ("project", "codex", 86400, True))
        self.assertIn("sandbox_mode read-only", quiet["rights"])
        self.assertEqual(len(quiet["warnings"]), 1)
        self.assertNotIn("s3cret", json.dumps(rows))

    # -- saving -----------------------------------------------------------------

    def save_text(self, name, text, **kwargs):
        return profiles.save(self.conn, name, text, "stdin", self.project,
                             schema=self.setting.schema, **kwargs)

    def test_a_save_lands_at_the_project_scope_and_names_who_saved_it(self):
        answer = self.save_text("telegram-fast", toml({**CODEX, "effort": "low"}),
                                created_by="telegram:42")
        self.assertEqual((answer["scope"], answer["created"], answer["created_by"]),
                         ("project", True, "telegram:42"))
        found = self.resolve("telegram-fast")
        self.assertEqual((found["layer"], found["version"]), ("project", answer["version"]))
        again = self.save_text("telegram-fast", toml({**CODEX, "effort": "low"}))
        self.assertEqual((again["created"], again["version"]), (False, answer["version"]))

    def test_a_save_over_a_version_no_longer_active_is_a_conflict(self):
        first = self.save_text("telegram-fast", toml(CODEX))
        self.save_text("telegram-fast", toml({**CODEX, "effort": "low"}),
                       base=first["version"])
        refused = self.refusal(self.save_text, "telegram-fast",
                               toml({**CODEX, "effort": "high"}), base=first["version"])
        self.assertEqual((refused.exit_code, refused.code), (6, "conflict"))

    def test_a_save_is_refused_for_each_case_named(self):
        self.save("telegram-shared", CLAUDE, scope="global")
        for name, text, code in (
                ("claude-act", toml(CLAUDE), "profile_name_refused"),
                ("telegram-claude", toml(CLAUDE), "profile_reserved"),
                ("telegram-codex", toml(CODEX), "profile_reserved"),
                ("telegram-shared", toml(CLAUDE), "profile_reserved"),
                ("telegram-odd", toml(CLAUDE) + "not_a_knob = 1\n", "profile_invalid"),
                ("telegram-odd", toml({**CLAUDE, "env_set": {"K": "${HOME}"}}),
                 "profile_invalid"),
                ("telegram-odd", toml({k: v for k, v in CLAUDE.items()
                                       if k != "timeout_seconds"}), "profile_unfit")):
            with self.subTest(name=name, code=code):
                refused = self.refusal(self.save_text, name, text)
                self.assertEqual((refused.exit_code, refused.code), (6, code))
        self.assertEqual([one.name for one in self.active()], ["telegram-shared"])

    def test_the_command_face_answers_json_and_exits_with_the_refusals_code(self):
        env = {"AGENTKIT_DB_URL": STORE_DSN}
        jobs_module = mock.Mock(store_setting=lambda _root: self.setting,
                                JobError=jobs.JobError)
        run = {}

        def face(*argv, text=""):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(profiles, "_jobs_module", return_value=jobs_module), \
                    mock.patch.dict(os.environ, env), \
                    mock.patch.object(sys, "stdin", io.StringIO(text)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = profiles.main(list(argv))
            run.update(code=code, out=out.getvalue(), err=err.getvalue())
            return code

        common = ["--project-id", self.project]
        self.assertEqual(face("save", "telegram-fast", *common, "--created-by", "user@host",
                              text=toml(CODEX)), 0)
        saved = json.loads(run["out"])
        self.assertEqual(face("list", *common), 0)
        self.assertIn("telegram-fast", [r["name"] for r in json.loads(run["out"])["profiles"]])
        self.assertEqual(face("show", "telegram-fast", *common), 0)
        shown = json.loads(run["out"])
        self.assertEqual((shown["version"], shown["layer"]), (saved["version"], "project"))
        self.assertIn('harness = "codex"', shown["text"])
        self.assertEqual(face("show", "telegram-claude", *common), 0)
        self.assertIn("Profile \"telegram-claude\"", json.loads(run["out"])["text"])
        self.assertEqual(face("save", "telegram-codex", *common, text=toml(CODEX)), 6)
        self.assertEqual(json.loads(run["err"])["error"]["code"], "profile_reserved")
        self.assertEqual(face("show", "telegram-nowhere", *common), 3)


@needs_store
class Tables(unittest.TestCase):
    """The runner's profile table is applied before the register's own."""

    def test_the_daemons_register_open_applies_the_harness_ledger_first(self):
        from capabilities_contract import db
        order = []
        real = db.migrate

        def recording(conn, owner, *args, **kwargs):
            order.append(owner)
            return real(conn, owner, *args, **kwargs)

        steps = {"owner": "harness", "steps": [["0001-profiles", "select 1"]],
                 "major": 1, "minor": 0}
        if lib is not None:
            steps = profiles.store_steps()
        with tempfile.TemporaryDirectory() as tmp:
            envelope = Path(tmp) / "capabilities"
            envelope.mkdir()
            (envelope / "project.json").write_text(json.dumps({"id": project_id()}))
            with mock.patch.object(db, "migrate", recording), \
                    mock.patch.object(jobs, "_PROFILE_STORE_STEPS", steps):
                store, _reg = jobs.open_register(envelope, "development",
                                                 setting=store_setting())
                store.close()
                order_daemon = list(order)
                order.clear()
                store, _reg = jobs.open_register(envelope, "development",
                                                 setting=store_setting(),
                                                 profile_tables=False)
                store.close()
        self.assertEqual(order_daemon, ["harness", jobs.STORE_OWNER])
        self.assertEqual(order, [jobs.STORE_OWNER])

    def test_without_the_runner_the_steps_come_from_the_profiles_module(self):
        steps = {"owner": "harness", "steps": [["0001-profiles", "select 1"]],
                 "major": 1, "minor": 0}
        done = subprocess.CompletedProcess([], 0, json.dumps(steps), "")
        with mock.patch.object(jobs, "_PROFILE_STORE_STEPS", None), \
                mock.patch.dict(sys.modules, {"callva": None}), \
                mock.patch.object(jobs.subprocess, "run", return_value=done) as run:
            self.assertEqual(jobs.profile_store_steps(), steps)
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], [str(SERVICE_DIR / "profiles.py"), "store-steps"])

    def test_a_runner_that_cannot_be_resolved_is_named(self):
        done = subprocess.CompletedProcess([], 1, "", "no network")
        with mock.patch.object(jobs, "_PROFILE_STORE_STEPS", None), \
                mock.patch.dict(sys.modules, {"callva": None}), \
                mock.patch.object(jobs.subprocess, "run", return_value=done):
            with self.assertRaises(jobs.JobError) as caught:
                jobs.profile_store_steps()
        self.assertEqual(caught.exception.slug, "harness_runner_unavailable")
        self.assertIn("no network", caught.exception.message)


class Migrate(unittest.TestCase):
    """`telegram migrate [status]` reports the runner's ledger beside its own."""

    def run_migrate(self, argv):
        cli = import_cli()
        steps = ("harness", [("0001-profiles", "select 1")], 1, 0)
        seen = []
        result = mock.Mock(owner="harness", schema="agentkit", applied=["0001-profiles"],
                           skipped=[], major=1, minor=0, warnings=[], state="current",
                           stored_major=1, stored_minor=0, pending=[], changed=[])
        fake_db = mock.Mock(DbError=Exception)
        fake_db.migrate.side_effect = lambda *a, **k: seen.append(("migrate", a[1])) or result
        fake_db.status.side_effect = lambda *a, **k: seen.append(("status", a[1])) or result

        def contract(args):
            seen.append(("contract", tuple(args)))
            cli._emit({"owner": "telegram", "applied": []})

        import capabilities_contract
        out = io.StringIO()
        with mock.patch.object(cli, "_profile_tables", return_value=steps), \
                mock.patch.object(capabilities_contract, "db", fake_db), \
                mock.patch.object(cli, "_cmd_migrate", side_effect=contract), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli._migrate_with_profile_tables(argv)
        return seen, json.loads(out.getvalue())

    def test_migrate_applies_the_harness_ledger_before_the_registers(self):
        seen, report = self.run_migrate([])
        self.assertEqual(seen, [("migrate", "harness"), ("contract", ())])
        self.assertEqual(report["owner"], "telegram")
        self.assertEqual(report["harness"]["owner"], "harness")

    def test_migrate_status_reports_both(self):
        seen, report = self.run_migrate(["status"])
        self.assertEqual(seen, [("status", "harness"), ("contract", ("status",))])
        self.assertEqual((report["owner"], report["harness"]["state"]),
                         ("telegram", "current"))


@needs_runner
@needs_store
class MigrateProfiles(unittest.TestCase):
    """`telegram service migrate-profiles [--apply]` moves every old worker
    setting - in settings, on the channel register and on open job rows - to a
    profile name, writing the profiles it needs at the project's scope."""

    SETTINGS = {
        "connection": "probe",
        "defaults": {
            "tail_size": 40,
            "worker": "codex",
            "workers": {
                "codex": {"model": "gpt-5.5", "reasoning_effort": "high",
                          "service_tier": "fast"},
                "claude": {"model": "sonnet", "effort": None,
                           "context": {"tools": ["Read"], "mcp": False}},
                "stub": {"model": None}},
            "voice_agent": {"worker": "claude", "voice": "Calm",
                            "workers": {"claude": {"effort": "low"}}}},
        "allowed_users": {"11": {"name": "A caller",
                                 "voice_agent": {"mode": "enabled",
                                                 "workers": {"codex": {"model": "m"}}}}},
        "allowed_groups": {"-100": {"name": "A room", "members": {
            "12": {"voice_agent": {"worker": "claude"}}}}},
    }
    REGISTER = {
        "11": {"last_processed_message_id": 5, "settings": {"worker": "claude"}},
        "-100": {"settings": {"tail_size": 9},
                 "workers": {"codex": {"model": "gpt-5.4"}}},
        "12": {"settings": {"worker": "claude", "model": "opus"}},
        "13": {"settings": {"debounce": 2}},
        "14": {"settings": {"worker": "stub"}},
    }

    def setUp(self):
        from capabilities_contract.db import connect
        self.setting = store_setting()
        self.project = project_id()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.envelope = self.root / "capabilities"
        self.envelope.mkdir()
        (self.envelope / "project.json").write_text(json.dumps(
            {"id": self.project, "slug": "testproject"}))
        self.register_path = self.root / "state" / "register.json"
        self.register_path.parent.mkdir()
        self.register_path.write_text(json.dumps(self.REGISTER))
        self.settings = json.loads(json.dumps(self.SETTINGS))
        self.cli = import_cli()
        self.jobs = self.cli._jobs_module()
        self.store, self.reg = self.jobs.open_register(
            self.envelope, "production", project_id=self.project, setting=self.setting,
            profile_tables=False)
        self.addCleanup(self.store.close)
        self.conn = connect(application_name="telegram", setting=self.setting)
        self.addCleanup(self.conn.close)
        waiting = self.reg.submit(self.reg.register(
            channel_key="11", requested_by="11", description="waiting", engine="claude",
            model="sonnet")["id"])
        quota = self.reg.register(channel_key="11", requested_by="11",
                                  description="paused", engine="codex")
        self.reg.update(quota["id"], state="stopped", outcome="quota")
        done = self.reg.register(channel_key="11", requested_by="11",
                                 description="finished", engine="claude")
        self.reg.update(done["id"], state="stopped", outcome="succeeded")
        self.ids = {"waiting": waiting["id"], "quota": quota["id"], "done": done["id"]}

    def migrate(self, apply=False, environ=None, running=False):
        cli, test = self.cli, self
        profiles_module = cli._profiles_module()

        class Records:
            def set(self, capability, collection, key, value, actor=None):
                test.assertEqual((capability, collection), ("telegram", "setting"))
                test.settings[key] = value

        def profiles_answer(argv, text=""):
            from capabilities_contract.db import connect
            out, err = io.StringIO(), io.StringIO()
            conn = connect(application_name="telegram", setting=self.setting)
            with mock.patch.object(sys, "stdin", io.StringIO(text)), \
                    mock.patch.object(profiles_module, "_connect",
                                      lambda root: (conn, self.setting.schema)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = profiles_module.main(argv)
            if code:
                error = json.loads(err.getvalue().splitlines()[-1])["error"]
                cli._die(code, error["code"], error["message"])
            return json.loads(out.getvalue())

        def open_register(root, write=True):
            return self.jobs, self.jobs.open_register(
                self.envelope, "production", project_id=self.project,
                setting=self.setting, profile_tables=False)[1]

        clean = {k: v for k, v in os.environ.items()
                 if k not in ("TG_WORKER", "TELEGRAM_SERVICE_WORKER", "CAPABILITIES_READ_ONLY")}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {**clean, **(environ or {})}, clear=True), \
                mock.patch.object(cli, "_service_project_root", lambda: self.root), \
                mock.patch.object(cli, "_require_service_envelope", lambda root: None), \
                mock.patch.object(cli, "_service_settings",
                                  lambda root: json.loads(json.dumps(self.settings))), \
                mock.patch.object(cli, "_service_state_dir",
                                  lambda root, c, s: ("probe", {"register": self.register_path})), \
                mock.patch.object(cli, "cmd_service_status",
                                  lambda c, s: {"running": running}), \
                mock.patch.object(cli, "_open_job_register", open_register), \
                mock.patch.object(cli, "_project_id", lambda write=False: self.project), \
                mock.patch.object(cli, "_profiles_answer", profiles_answer), \
                mock.patch.object(cli, "_records", lambda: Records()), \
                contextlib.redirect_stderr(err):
            try:
                return 0, cli.cmd_service_migrate_profiles(None, None, apply)
            except SystemExit as stopped:
                return stopped.code, json.loads(err.getvalue().splitlines()[-1])["error"]

    def stored_names(self):
        found = profiles.stored(self.conn, self.project, self.setting.schema)
        self.conn.rollback()
        return sorted(one.name for one in found if one.scope == "project")

    def test_a_dry_run_reports_every_move_and_changes_nothing(self):
        code, report = self.migrate(environ={"TG_WORKER": "claude"})
        self.assertEqual(code, 0, report)
        self.assertFalse(report["applied"])
        self.assertTrue(report["changed"])
        self.assertEqual(self.settings, self.SETTINGS)
        self.assertEqual(json.loads(self.register_path.read_text()), self.REGISTER)
        self.assertEqual(self.stored_names(), [])
        self.assertIsNone(self.reg.get(self.ids["waiting"])["profile"])
        names = {row["profile"]: row["would_create"] for row in report["profiles"]}
        self.assertEqual(names, {
            "telegram-codex-gpt-5.5-high-priority": True,
            "telegram-claude-sonnet-low-tools-read-no-mcp": True,
            "telegram-claude-sonnet-tools-read-no-mcp": True,
            "telegram-claude-opus-tools-read-no-mcp": True,
            "telegram-claude-sonnet": True,
            "telegram-codex": False})
        # Under the environment's worker a row without its own `/set worker`
        # ran on claude, so its codex block is dropped rather than moved.
        moved = {move["channel"]: move["profile"] for move in report["register"]["moves"]}
        self.assertEqual(moved, {"11": "telegram-claude-sonnet-tools-read-no-mcp",
                                 "-100": None,
                                 "12": "telegram-claude-opus-tools-read-no-mcp",
                                 "14": "stub"})
        self.assertEqual({row["id"]: row["profile"] for row in report["jobs"]},
                         {self.ids["waiting"]: "telegram-claude-sonnet",
                          self.ids["quota"]: "telegram-codex"})
        self.assertEqual(report["environment"]["set"], ["TG_WORKER"])
        self.assertEqual(report["environment"]["replace_with"],
                         {"TELEGRAM_SERVICE_PROFILE": "telegram-claude-sonnet-tools-read-no-mcp"})
        self.assertEqual(sorted(report["settings"]["removed"]), [
            "settings.allowed_groups.-100.members.12.voice_agent.worker",
            "settings.allowed_users.11.voice_agent.workers"])

    def test_apply_writes_every_move_and_a_rerun_changes_nothing(self):
        code, report = self.migrate(apply=True)
        self.assertEqual(code, 0, report)
        self.assertTrue(report["applied"])
        defaults = self.settings["defaults"]
        self.assertEqual(defaults["profile"], "telegram-codex-gpt-5.5-high-priority")
        self.assertEqual(defaults["voice_agent"],
                         {"voice": "Calm",
                          "profile": "telegram-claude-sonnet-low-tools-read-no-mcp"})
        self.assertNotIn("worker", defaults)
        self.assertNotIn("workers", defaults)
        self.assertEqual(defaults["tail_size"], 40)
        self.assertEqual(self.settings["allowed_users"]["11"]["voice_agent"],
                         {"mode": "enabled"})
        self.assertEqual(self.settings["allowed_groups"]["-100"]["members"]["12"],
                         {"voice_agent": {}})
        schema = self.cli._settings_schema_module()
        self.assertEqual(schema.old_worker_keys(self.settings), [])
        register = json.loads(self.register_path.read_text())
        self.assertEqual(schema.old_register_fields(register), [])
        self.assertEqual(register["11"], {"last_processed_message_id": 5, "settings": {
            "profile": "telegram-claude-sonnet-tools-read-no-mcp"}})
        self.assertEqual(register["-100"]["settings"],
                         {"tail_size": 9, "profile": "telegram-codex-gpt-5.4-high-priority"})
        self.assertEqual(register["12"]["settings"],
                         {"profile": "telegram-claude-opus-tools-read-no-mcp"})
        self.assertEqual(register["13"], self.REGISTER["13"])
        self.assertEqual(register["14"]["settings"], {"profile": "stub"})
        self.assertEqual(self.reg.get(self.ids["waiting"])["profile"], "telegram-claude-sonnet")
        self.assertEqual(self.reg.get(self.ids["quota"])["profile"], "telegram-codex")
        self.assertIsNone(self.reg.get(self.ids["done"])["profile"])
        self.assertEqual(self.stored_names(), sorted([
            "telegram-codex-gpt-5.5-high-priority",
            "telegram-claude-sonnet-low-tools-read-no-mcp",
            "telegram-claude-sonnet-tools-read-no-mcp",
            "telegram-codex-gpt-5.4-high-priority",
            "telegram-claude-opus-tools-read-no-mcp", "telegram-claude-sonnet"]))
        # Each profile is the bundled one with the old values laid over it,
        # and every one fits a turn.
        active = profiles.stored(self.conn, self.project, self.setting.schema)
        self.conn.rollback()
        for name in self.stored_names():
            profiles.resolve(name, active, self.project)
        self.assertEqual(profiles.resolve("telegram-codex-gpt-5.5-high-priority", active,
                                          self.project)["knobs"],
                         {**CODEX, "model": "gpt-5.5", "effort": "high",
                          "service_tier": "priority"})
        self.assertEqual(profiles.resolve("telegram-claude-sonnet-low-tools-read-no-mcp",
                                          active, self.project)["knobs"],
                         {**CLAUDE, "model": "sonnet", "effort": "low", "tools": ["Read"],
                          "strict_mcp": True, "mcp_config": {"mcpServers": {}}})

        settings_after = json.loads(json.dumps(self.settings))
        register_after = self.register_path.read_text()
        code, again = self.migrate(apply=True)
        self.assertEqual(code, 0, again)
        self.assertFalse(again["changed"])
        self.assertEqual(again["profiles"], [])
        self.assertEqual(again["settings"]["keys_written"], [])
        self.assertEqual(again["jobs"], [])
        self.assertEqual(self.settings, settings_after)
        self.assertEqual(self.register_path.read_text(), register_after)

    def test_apply_is_refused_while_the_service_runs(self):
        code, error = self.migrate(apply=True, running=True)
        self.assertEqual((code, error["code"]), (6, "service_running"))
        self.assertEqual(self.settings, self.SETTINGS)
        self.assertEqual(self.stored_names(), [])
        # A dry run is not.
        self.assertEqual(self.migrate(running=True)[0], 0)


class WhoMaySave(unittest.TestCase):
    """A worker saves only for a requester whose role may /set."""

    def saver(self, env, settings=None):
        cli = import_cli()
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith("TELEGRAM_AUTHORIZED_") and k != "TELEGRAM_DAEMON_CHILD"}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True), \
                mock.patch.object(cli, "_service_settings", return_value=settings or {}):
            return cli._profile_saver(Path("/nowhere"))

    def assertDenied(self, env, settings=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            self.saver(env, settings)
        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(json.loads(err.getvalue())["error"]["code"], "profile_save_denied")

    def child(self, role=None):
        env = {"TELEGRAM_DAEMON_CHILD": "1", "TELEGRAM_AUTHORIZED_REQUESTER_ID": "42"}
        if role:
            env["TELEGRAM_AUTHORIZED_REQUESTER_ROLE"] = role
        return env

    def test_a_terminal_saves_as_its_user_at_its_host(self):
        self.assertRegex(self.saver({}), r"^[^@]+@.+$")

    def test_a_worker_saves_for_a_role_that_may_set(self):
        for role in ("supervisor", "channel_admin"):
            self.assertEqual(self.saver(self.child(role)), "telegram:42")

    def test_a_worker_is_refused_for_a_role_that_may_not_set(self):
        for role in ("direct_user", "group_member", "unknown"):
            self.assertDenied(self.child(role))

    def test_a_worker_without_a_stated_role_is_refused(self):
        self.assertDenied(self.child())

    def test_the_settings_control_decides_as_it_does_for_set(self):
        grant = {"control": {"roles": {"direct_user": {"commands": ["status", "set"]}}}}
        self.assertEqual(self.saver(self.child("direct_user"), grant), "telegram:42")
        revoke = {"control": {"roles": {"channel_admin": {"commands": ["status"]}}}}
        self.assertDenied(self.child("channel_admin"), revoke)

    def test_the_read_only_switch_refuses_a_save(self):
        cli = import_cli()
        args = mock.Mock(profiles_cmd="save", file="-", base=None)
        args.name = "telegram-x"
        err = io.StringIO()
        with mock.patch.object(cli, "_service_project_root", return_value=Path("/p")), \
                mock.patch.object(cli, "_project_id", return_value="prj_x"), \
                mock.patch.object(cli, "read_only_switch", return_value=True), \
                mock.patch.object(cli, "_profiles_answer") as answer, \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            cli.cmd_profiles(args)
        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(json.loads(err.getvalue())["error"]["code"], "read_only_switch")
        answer.assert_not_called()

    def test_a_save_hands_the_profile_and_who_saved_it_to_the_profiles_module(self):
        cli = import_cli()
        args = mock.Mock(profiles_cmd="save", file="-", base="v1")
        args.name = "telegram-x"
        with mock.patch.object(cli, "_service_project_root", return_value=Path("/p")), \
                mock.patch.object(cli, "_project_id", return_value="prj_x"), \
                mock.patch.object(cli, "read_only_switch", return_value=False), \
                mock.patch.object(cli, "_profile_saver", return_value="telegram:42"), \
                mock.patch.object(sys, "stdin", io.StringIO("harness = 'codex'\n")), \
                mock.patch.object(cli, "_profiles_answer", return_value={"ok": 1}) as answer:
            self.assertEqual(cli.cmd_profiles(args), {"ok": 1})
        argv, text = answer.call_args.args
        self.assertEqual(argv, ["save", "telegram-x", "--project-id", "prj_x",
                                "--project-root", "/p", "--created-by", "telegram:42",
                                "--where", "stdin", "--base", "v1"])
        self.assertEqual(text, "harness = 'codex'\n")


if __name__ == "__main__":
    unittest.main()
