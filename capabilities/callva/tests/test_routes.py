#!/usr/bin/env python3
"""Tests that `callva routes` says what each verb actually calls.

Every domain verb is parsed from a real argv and run with its HTTP call captured,
so the declared method and path are held to the call the handler makes.

Run with: uv run --with httpx --with "capabilities-contract>=0.12,<1" python3 capabilities/callva/tests/test_routes.py
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx


CAPABILITY = Path(__file__).resolve().parents[1]
SCRIPT = next((path for path in (
    CAPABILITY / "bin" / "callva", CAPABILITY / "callva")
    if path.is_file()), CAPABILITY / "bin" / "callva")


def _load_module():
    name = "callva_routes_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


callva = _load_module()

DATA = ["--data", '{"a": 1}']
CONFIG = ["--config", '{"a": 1}']

# One real command line per verb; `{name}` is filled with a value per placeholder.
ARGV = {
    "agents list": ["agents", "list"],
    "agents default": ["agents", "default"],
    "agents get": ["agents", "get", "{id}"],
    "agents create": ["agents", "create"],
    "agents update": ["agents", "update", "{id}", "--name", "N"],
    "agents clone": ["agents", "clone", "{id}"],
    "agents delete": ["agents", "delete", "{id}"],
    "agents config": ["agents", "config", "AG"],
    "presets catalog": ["presets", "catalog"],
    "presets describe": ["presets", "describe", "P"],
    "presets list": ["presets", "list", "{id}"],
    "presets get": ["presets", "get", "{id}", "{preset}"],
    "presets set": ["presets", "set", "{id}", "{preset}", *CONFIG],
    "presets patch": ["presets", "patch", "{id}", "{preset}", *CONFIG],
    "presets activate": ["presets", "activate", "{id}", "{preset}"],
    "tools list": ["tools", "list", "{id}"],
    "tools get": ["tools", "get", "{id}", "{key}"],
    "tools set": ["tools", "set", "{id}", "{key}", *CONFIG],
    "tools patch": ["tools", "patch", "{id}", "{key}", "--position", "1"],
    "tools delete": ["tools", "delete", "{id}", "{key}"],
    "assets list": ["assets", "list"],
    "assets get": ["assets", "get", "{id}"],
    "assets create": ["assets", "create", "--name", "N", "--type", "prompt", "--content", "C"],
    "assets update": ["assets", "update", "{id}", "--name", "N"],
    "assets delete": ["assets", "delete", "{id}"],
    "calls list": ["calls", "list"],
    "calls get": ["calls", "get", "{call}"],
    "calls create": ["calls", "create", *DATA],
    "calls update": ["calls", "update", "{call}", *DATA],
    "calls delete": ["calls", "delete", "{call}"],
    "calls batch": ["calls", "batch", "C1", *DATA],
    "calls webhook-logs": ["calls", "webhook-logs", "{call}"],
    "calls automation-runs": ["calls", "automation-runs", "{call}"],
    "calls actions": ["calls", "actions", "{call}"],
    "calls run-action": ["calls", "run-action", "{call}", "{action}"],
    "calls action-runs": ["calls", "action-runs", "{call}"],
    "actions list": ["actions", "list"],
    "actions get": ["actions", "get", "{action}"],
    "actions create": ["actions", "create", *DATA],
    "actions update": ["actions", "update", "{action}", *DATA],
    "actions put": ["actions", "put", "{action}", *DATA],
    "actions delete": ["actions", "delete", "{action}"],
    "ui-views list": ["ui-views", "list"],
    "ui-views get": ["ui-views", "get", "{view}"],
    "ui-views create": ["ui-views", "create", *DATA],
    "ui-views update": ["ui-views", "update", "{view}", *DATA],
    "ui-views put": ["ui-views", "put", "{view}", *DATA],
    "ui-views delete": ["ui-views", "delete", "{view}"],
    "ui-views set-default": ["ui-views", "set-default", "{view}"],
    "issues list": ["issues", "list"],
    "issues get": ["issues", "get", "{issue}"],
    "issues create": ["issues", "create", *DATA],
    "issues update": ["issues", "update", "{issue}", *DATA],
    "issues put": ["issues", "put", "{issue}", *DATA],
    "issues delete": ["issues", "delete", "{issue}"],
    "knowledge list": ["knowledge", "list"],
    "knowledge get": ["knowledge", "get", "{knowledgeItem}"],
    "knowledge create": ["knowledge", "create", *DATA],
    "knowledge update": ["knowledge", "update", "{knowledgeItem}", *DATA],
    "knowledge put": ["knowledge", "put", "{knowledgeItem}", *DATA],
    "knowledge delete": ["knowledge", "delete", "{knowledgeItem}"],
    "transcripts get": ["transcripts", "get", "{call}"],
    "transcripts store": ["transcripts", "store", "{call}", *DATA],
    "transcripts delete": ["transcripts", "delete", "{call}"],
    "transcripts url": ["transcripts", "url", "{call}"],
    "call": ["call", "+15550000000", "--agent-id", "AG"],
    "stats aggregate": ["stats", "aggregate", "--op", "count"],
    "stats trends": ["stats", "trends", "--from", "2026-01-01", "--to", "2026-01-02"],
    "recordings stream": ["recordings", "stream", "{call}", "--url", "https://r.example/x", "--output", "/dev/null"],
    "recordings purge": ["recordings", "purge", "{call}"],
    "fields list": ["fields", "list"],
    "fields create": ["fields", "create", *DATA],
    "fields update": ["fields", "update", "{custom_field}", *DATA],
    "fields delete": ["fields", "delete", "{custom_field}"],
    "fields impact": ["fields", "impact", "{custom_field}"],
    "fields extraction-schema": ["fields", "extraction-schema"],
    "field-groups list": ["field-groups", "list"],
    "field-groups create": ["field-groups", "create", *DATA],
    "field-groups update": ["field-groups", "update", "{groupId}", *DATA],
    "field-groups delete": ["field-groups", "delete", "{groupId}"],
    "field-groups reorder": ["field-groups", "reorder", *DATA],
    "field-groups add-field": ["field-groups", "add-field", "{groupId}", "F"],
    "field-groups remove-field": ["field-groups", "remove-field", "{groupId}", "{fieldId}"],
    "field-groups reorder-fields": ["field-groups", "reorder-fields", "{groupId}", *DATA],
    "field-groups update-field": ["field-groups", "update-field", "{groupId}", "{fieldId}", *DATA],
    "schedules list": ["schedules", "list"],
    "schedules get": ["schedules", "get", "{id}"],
    "schedules create": ["schedules", "create", *DATA],
    "schedules update": ["schedules", "update", "{id}", *DATA],
    "schedules delete": ["schedules", "delete", "{id}"],
    "schedules preview": ["schedules", "preview", "{id}", "2026-01-01T00:00:00Z"],
    "schedules executions": ["schedules", "executions", "{id}"],
    "automations list": ["automations", "list"],
    "automations get": ["automations", "get", "{id}"],
    "automations create": ["automations", "create", "--name", "N"],
    "automations update": ["automations", "update", "{id}", "--name", "N"],
    "automations delete": ["automations", "delete", "{id}"],
    "automations code": ["automations", "code", "{id}"],
    "automations deploy": ["automations", "deploy", "{id}", "--code", "C"],
    "automations run": ["automations", "run", "{id}"],
    "automations runs": ["automations", "runs", "{id}"],
    "automations all-runs": ["automations", "all-runs"],
    "automations run-detail": ["automations", "run-detail", "{id}", "{jobId}"],
    "automations runtime-info": ["automations", "runtime-info"],
    "automations triggers": ["automations", "triggers", "{id}"],
    "automations add-trigger": ["automations", "add-trigger", "{id}", "--method", "get", "--mode", "async", "--auth", "none"],
    "automations update-trigger": ["automations", "update-trigger", "{id}", "{path}", "--method", "post"],
    "automations remove-trigger": ["automations", "remove-trigger", "{id}", "{path}"],
    "automations toggle-trigger": ["automations", "toggle-trigger", "{id}", "{path}", "--enabled", "true"],
    "webhook-logs list": ["webhook-logs", "list"],
    "webhook-logs get": ["webhook-logs", "get", "{webhookLog}"],
    "webhook-logs filters": ["webhook-logs", "filters"],
    "variables list": ["variables", "list"],
    "variables create": ["variables", "create", "--name", "N", "--value", "V"],
    "variables update": ["variables", "update", "{path}", "--value", "V"],
    "variables delete": ["variables", "delete", "{path}"],
    "settings list": ["settings", "list"],
    "settings get": ["settings", "get", "K"],
    "settings reveal": ["settings", "reveal", "{definitionId}"],
    "projects list": ["projects", "list"],
    "projects get": ["projects", "get", "{id}"],
    "projects create": ["projects", "create", "--name", "N"],
    "projects update": ["projects", "update", "{id}", "--name", "N"],
    "projects put": ["projects", "put", "{id}", "--name", "N"],
    "projects delete": ["projects", "delete", "{id}"],
    "phone-numbers list": ["phone-numbers", "list"],
    "phone-numbers get": ["phone-numbers", "get", "{id}"],
    "phone-numbers create": ["phone-numbers", "create", "--phone-number", "+15550000000", "--number-type", "manual"],
    "phone-numbers update": ["phone-numbers", "update", "{id}", "--label", "L"],
    "phone-numbers delete": ["phone-numbers", "delete", "{id}"],
    "phone-numbers bind": ["phone-numbers", "bind", "{id}", "AG"],
    "providers list": ["providers", "list"],
    "providers types": ["providers", "types"],
}

# The verbs that read an answer for a match, so a bare response would refuse them.
ANSWER = {
    "presets describe": {"data": [{"id": "P", "fields": {}}]},
    "settings get": {"data": {"settings": [{"key": "K"}]}},
    "settings reveal": {"data": {"effective_value": "v"}},
}


class _Values(dict):
    def __missing__(self, key):
        return f"val-{key}"


def _dispatched():
    for resource, handler in callva.DISPATCH.items():
        if callable(handler):
            yield resource
        else:
            yield from (f"{resource} {action}" for action in handler)


def _fill(argv):
    return [token.format_map(_Values()) if re.fullmatch(r"\{\w+\}", token) else token
            for token in argv]


def _handler(verb):
    resource, _, action = verb.partition(" ")
    handler = callva.DISPATCH[resource]
    return handler if callable(handler) else handler[action]


class RoutesTest(unittest.TestCase):
    def test_every_dispatched_verb_is_declared_and_exercised(self):
        verbs = set(_dispatched())
        self.assertEqual(verbs, set(callva.ROUTES))
        self.assertEqual(verbs, set(ARGV))

    def test_each_verb_makes_exactly_the_call_it_declares(self):
        for verb, (method, template) in callva.ROUTES.items():
            with self.subTest(verb=verb):
                calls = []

                def fake_api(m, path, data=None, query=None, allow_404=False):
                    calls.append((m, path))
                    return ANSWER.get(verb, {"data": []})

                def fake_download(path, output, query=None):
                    calls.append(("GET", path))

                args = callva.build_parser().parse_args(_fill(ARGV[verb]))
                with mock.patch.object(callva, "api", fake_api), \
                        mock.patch.object(callva, "api_download", fake_download), \
                        contextlib.redirect_stdout(io.StringIO()):
                    _handler(verb)(args)
                self.assertEqual(calls, [(method, template.format_map(_Values()))])

    def test_writes_is_the_write_gate_and_no_get_writes(self):
        report = callva._routes_report()["routes"]
        self.assertEqual({r["verb"] for r in report if r["writes"]}, set(callva.WRITE_VERBS))
        for route in report:
            if route["method"] == "GET":
                self.assertFalse(route["writes"], route["verb"])

    def test_only_reveal_is_marked_secret(self):
        report = callva._routes_report()["routes"]
        self.assertEqual([r["verb"] for r in report if r["secret"]], ["settings reveal"])
        self.assertEqual(callva.SECRET_READS, {"settings reveal"})


class _Main:
    """Runs main() as far as the HTTP transport, with the policy gate passed."""

    def __init__(self, allow_write=True, respond=None):
        self.requests = []
        self.allow_write = allow_write
        self.respond = respond or (lambda request: httpx.Response(200, json={"data": {}}))

    def run(self, *argv):
        def handler(request):
            self.requests.append(request)
            return self.respond(request)

        def client(api_key, base_url):
            return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))

        conn = {"id": "c", "allow_write": self.allow_write, "key_env": "K",
                "api_key": "k", "base": "https://api.example/api/v1"}
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(callva, "_gate", lambda: None), \
                mock.patch.object(callva, "_contract", lambda argv: None), \
                mock.patch.object(callva, "_connections_registry", lambda: (None, None)), \
                mock.patch.object(callva, "_select_connection", lambda reg, wanted: ("c", None)), \
                mock.patch.object(callva, "_build_conn", lambda *a: (conn, None)), \
                mock.patch.object(callva, "_client", client), \
                mock.patch.object(sys, "argv", ["callva", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                callva.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()


class RoutesVerbTest(unittest.TestCase):
    def test_routes_reads_no_connection_and_makes_no_call(self):
        def refuse(*_a, **_k):
            raise AssertionError("routes reached for a connection or the network")

        out = io.StringIO()
        with mock.patch.object(callva, "_gate", lambda: None), \
                mock.patch.object(callva, "_contract", lambda argv: None), \
                mock.patch.object(callva, "_connections_registry", refuse), \
                mock.patch.object(callva, "_client", refuse), \
                mock.patch.object(sys, "argv", ["callva", "routes"]), \
                contextlib.redirect_stdout(out):
            callva.main()
        routes = json.loads(out.getvalue())["routes"]
        self.assertEqual(len(routes), len(callva.ROUTES))
        self.assertIn({"verb": "calls run-action", "method": "POST",
                       "path": "/external/calls/{call}/actions/{action}",
                       "writes": True, "secret": False}, routes)


class WriteGateTest(unittest.TestCase):
    def test_new_mutating_verbs_refuse_on_a_read_only_connection(self):
        new = [verb for verb in callva.WRITE_VERBS
               if verb.split(" ")[0] in ("actions", "ui-views")
               or verb in ("recordings purge", "calls run-action")]
        self.assertEqual(len(new), 11)
        for verb in new:
            with self.subTest(verb=verb):
                main = _Main(allow_write=False)
                code, out, err = main.run(*_fill(ARGV[verb]))
                self.assertEqual(code, 4)
                self.assertEqual(json.loads(err)["error"]["code"], "read_only")
                self.assertEqual(main.requests, [])

    def test_provisioning_verbs_refuse_on_a_read_only_connection(self):
        new = [verb for verb in callva.WRITE_VERBS
               if verb.split(" ")[0] in ("issues", "knowledge", "projects", "phone-numbers")
               or verb == "agents create"]
        self.assertEqual(len(new), 17)
        for verb in new:
            with self.subTest(verb=verb):
                main = _Main(allow_write=False)
                code, out, err = main.run(*_fill(ARGV[verb]))
                self.assertEqual(code, 4)
                self.assertEqual(json.loads(err)["error"]["code"], "read_only")
                self.assertEqual(main.requests, [])


class RevealTest(unittest.TestCase):
    PATH = "/api/v1/external/settings/project/D1/reveal"

    def test_prints_the_value_raw_with_a_newline_and_nothing_else(self):
        main = _Main(respond=lambda r: httpx.Response(200, json={
            "data": {"key": "k", "is_secret": True, "effective_value": "s3cret-value"}}))
        code, out, err = main.run("settings", "reveal", "D1")
        self.assertEqual((code, out, err), (0, "s3cret-value\n", ""))
        self.assertEqual(main.requests[0].url.path, self.PATH)

    def test_full_does_not_widen_what_it_prints(self):
        main = _Main(respond=lambda r: httpx.Response(200, json={
            "data": {"key": "k", "effective_value": "s3cret-value"}}))
        self.assertEqual(main.run("settings", "reveal", "D1", "--full")[1], "s3cret-value\n")

    def test_no_value_exits_3_not_set_with_nothing_on_stdout(self):
        for data in ({"effective_value": None, "value": None}, {"effective_value": ""}, {}):
            with self.subTest(data=data):
                main = _Main(respond=lambda r: httpx.Response(200, json={"data": data}))
                code, out, err = main.run("settings", "reveal", "D1")
                self.assertEqual((code, out), (3, ""))
                self.assertEqual(json.loads(err)["error"]["code"], "not_set")

    def test_unknown_definition_exits_3_with_nothing_on_stdout(self):
        main = _Main(respond=lambda r: httpx.Response(404, json={"message": "Setting not found"}))
        code, out, _err = main.run("settings", "reveal", "D1")
        self.assertEqual((code, out), (3, ""))

    def test_read_only_connection_refuses_before_any_request(self):
        main = _Main(allow_write=False)
        code, out, err = main.run("settings", "reveal", "D1")
        self.assertEqual((code, out), (4, ""))
        self.assertEqual(json.loads(err)["error"]["code"], "read_only")
        self.assertEqual(main.requests, [])

    def test_read_only_switch_refuses_before_any_request(self):
        main = _Main()
        with mock.patch.dict(os.environ, {"CAPABILITIES_READ_ONLY": "1"}):
            code, out, err = main.run("settings", "reveal", "D1")
        self.assertEqual((code, out), (4, ""))
        self.assertEqual(json.loads(err)["error"]["code"], "read_only_switch")
        self.assertEqual(main.requests, [])


class PayloadTest(unittest.TestCase):
    def _sent(self, *argv):
        main = _Main()
        code, _out, err = main.run(*argv)
        self.assertEqual(code, 0, err)
        request = main.requests[0]
        return request.method, json.loads(request.content or b"null")

    def test_run_action_sends_inputs_under_their_key(self):
        self.assertEqual(self._sent("calls", "run-action", "C", "A", "--inputs", '{"x": 1}'),
                         ("POST", {"inputs": {"x": 1}}))
        self.assertEqual(self._sent("calls", "run-action", "C", "A"), ("POST", {}))

    def test_put_and_update_send_the_body_as_given(self):
        for verb, method in (("update", "PATCH"), ("put", "PUT")):
            for resource in ("actions", "ui-views"):
                self.assertEqual(self._sent(resource, verb, "X", "--data", '{"a": 1}'),
                                 (method, {"a": 1}))

    def test_a_write_with_no_payload_is_refused_before_any_request(self):
        for argv in (("actions", "create"), ("actions", "update", "X"),
                     ("ui-views", "create"), ("ui-views", "put", "X")):
            with self.subTest(argv=argv):
                main = _Main()
                code, _out, err = main.run(*argv)
                self.assertEqual(code, 6)
                self.assertEqual(main.requests, [])

    def test_agent_create_sends_no_fields(self):
        self.assertEqual(self._sent("agents", "create"), ("POST", None))

    def test_custom_webhook_events_replaces_the_list_and_empty_forwards_nothing(self):
        self.assertEqual(self._sent("agents", "update", "A", "--custom-webhook-events", "call.ended, call.started"),
                         ("PATCH", {"custom_webhook_events": ["call.ended", "call.started"]}))
        self.assertEqual(self._sent("agents", "update", "A", "--custom-webhook-events", ""),
                         ("PATCH", {"custom_webhook_events": []}))
        self.assertEqual(self._sent("agents", "update", "A", "--data", '{"custom_webhook_events": ["call.ended"]}'),
                         ("PATCH", {"custom_webhook_events": ["call.ended"]}))

    def test_project_and_phone_flags_write_over_data(self):
        self.assertEqual(self._sent("projects", "create", "--name", "N", "--description", "D", "--is-active", "false"),
                         ("POST", {"name": "N", "description": "D", "is_active": False}))
        self.assertEqual(self._sent("projects", "put", "X", "--data", '{"name": "A", "description": "D"}', "--name", "B"),
                         ("PUT", {"name": "B", "description": "D"}))
        self.assertEqual(self._sent("phone-numbers", "create", "--phone-number", "+15550000000",
                                    "--number-type", "provider", "--provider-credential-id", "C",
                                    "--provider-phone-id", "P"),
                         ("POST", {"phone_number": "+15550000000", "number_type": "provider",
                                   "provider_credential_id": "C", "provider_phone_id": "P"}))
        self.assertEqual(self._sent("phone-numbers", "update", "X", "--label", "L", "--is-active", "true", "--status", "active"),
                         ("PATCH", {"label": "L", "is_active": True, "status": "active"}))

    def test_bind_names_the_agent_or_unbinds_out_loud(self):
        self.assertEqual(self._sent("phone-numbers", "bind", "X", "AG"), ("PUT", {"agent_id": "AG"}))
        self.assertEqual(self._sent("phone-numbers", "bind", "X", "--unbind"), ("PUT", {"agent_id": None}))
        for argv in (("phone-numbers", "bind", "X"), ("phone-numbers", "bind", "X", "AG", "--unbind")):
            with self.subTest(argv=argv):
                main = _Main()
                code, _out, err = main.run(*argv)
                self.assertEqual(code, 6)
                self.assertEqual(json.loads(err)["error"]["code"], "bind_target")
                self.assertEqual(main.requests, [])

    def test_journal_list_filters_and_writes_need_a_payload(self):
        main = _Main(respond=lambda r: httpx.Response(200, json={"data": []}))
        code, _out, err = main.run("issues", "list", "-f", "status=open", "--per-page", "5")
        self.assertEqual(code, 0, err)
        self.assertEqual(dict(main.requests[0].url.params), {"status": "open", "per_page": "5"})
        for argv in (("issues", "create"), ("knowledge", "update", "X"), ("projects", "create"),
                     ("phone-numbers", "update", "X")):
            with self.subTest(argv=argv):
                main = _Main()
                code, _out, _err = main.run(*argv)
                self.assertEqual(code, 6)
                self.assertEqual(main.requests, [])

    def test_extraction_schema_passes_the_entity_type(self):
        main = _Main()
        code, _out, err = main.run("fields", "extraction-schema", "--entity-type", "lead")
        self.assertEqual(code, 0, err)
        self.assertEqual(dict(main.requests[0].url.params), {"entity_type": "lead"})


if __name__ == "__main__":
    unittest.main()
