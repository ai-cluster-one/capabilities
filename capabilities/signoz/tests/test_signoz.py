import json
import types
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

CAPABILITY = Path(__file__).resolve().parents[1]
CLI = next((path for path in (
    CAPABILITY / "bin" / "signoz", CAPABILITY / "signoz")
    if path.is_file()), CAPABILITY / "bin" / "signoz")
module = types.ModuleType("signoz_capability")
module.__file__ = str(CLI)
exec(compile(CLI.read_text(), str(CLI), "exec"), module.__dict__)


@pytest.fixture(autouse=True)
def isolate_records_adapter():
    """Each test supplies a different temporary project through the environment."""
    module._RECORDS = None
    yield
    if module._RECORDS is not None:
        module._RECORDS.close()
    module._RECORDS = None


def connection(**overrides):
    value = {
        "id": "test",
        "url": "https://signoz.example",
        "api_key": "api-secret",
        "otlp_endpoint": "https://collector.example:4318",
        "ingestion_key": None,
        "custom_headers": None,
        "deployment": "self_hosted",
        "dimensions": {
            "agent": {"logs": "agent_id", "traces": "agent_id"},
            "call": {"logs": "call_id", "traces": "call_id"},
            "room": {"logs": "room_id", "traces": "room_id"},
        },
        "verify_tls": True,
        "allow_write": False,
    }
    value.update(overrides)
    return value


def test_help_documents_every_domain_command():
    help_text = module.__doc__ or ""
    for command in [
        "doctor", "connections", "version", "ingestion", "services", "metrics",
        "fields keys", "fields values", "logs search", "traces search", "trace",
        "call", "agent", "query", "dashboards", "alerts", "rules", "views",
        "channels", "api",
    ]:
        assert f"signoz {command}" in help_text
    assert "SIGNOZ_API_KEY" in help_text
    assert "SIGNOZ_INGESTION_KEY" in help_text
    assert help_text.count("[--select FIELD]") == 3


def test_declared_connection_accepts_existing_aliases(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "SIGNOZ_API_URL=https://legacy.example\n"
        "SIGNOZ_SERVICE_KEY=management-secret\n"
        "SIGNOZ_ENDPOINT=https://collector.example:4318\n"
    )
    connection_dir = tmp_path / "capabilities" / "signoz"
    connection_dir.mkdir(parents=True)
    (connection_dir / "connections.json").write_text(json.dumps({
        "default": "default",
        "connections": {"default": {}},
    }))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    resolved = module._resolve_connection("default")
    assert resolved["url"] == "https://legacy.example"
    assert resolved["api_key"] == "management-secret"
    assert resolved["otlp_endpoint"] == "https://collector.example:4318"
    report = module._connection_report(resolved)
    by_key = {item["key"]: item for item in report["keys"]}
    assert by_key["SIGNOZ_URL"]["resolved_from_key"] == "SIGNOZ_API_URL"
    assert by_key["SIGNOZ_API_KEY"]["value"] != "management-secret"


def test_request_sends_management_header_and_never_ingestion_key():
    seen = {}

    def handler(request: httpx.Request):
        seen["headers"] = request.headers
        return httpx.Response(200, json={"status": "success", "data": []})

    original = httpx.Client

    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    with patch.object(module.httpx, "Client", side_effect=client):
        result = module._request(
            connection(ingestion_key="ingestion-secret"),
            "GET", "/api/v1/dashboards")

    assert result["status"] == "success"
    assert seen["headers"]["SIGNOZ-API-KEY"] == "api-secret"
    assert "ingestion-secret" not in str(dict(seen["headers"]))
    assert "signoz-ingestion-key" not in seen["headers"]


def test_legacy_server_uses_v1_dashboard_and_rules_routes():
    info = {"version": "0.97.1"}
    assert module._dashboard_path(info) == "/api/v1/dashboards"
    assert module._rules_path(info) == "/api/v1/rules"
    assert module._dashboard_path({"version": "0.135.0"}, "d1") == \
        "/api/v2/dashboards/d1"
    assert module._rules_path({"version": "0.120.0"}, "r1") == \
        "/api/v2/rules/r1"


def test_log_query_has_stable_pagination_order():
    payload = module._query_payload(
        "logs", 1, 2, "service.name = 'api'", 100, 20)
    spec = payload["compositeQuery"]["queries"][0]["spec"]
    assert payload["schemaVersion"] == "v1"
    assert payload["requestType"] == "raw"
    assert spec["signal"] == "logs"
    assert spec["offset"] == 20
    assert spec["order"] == [
        {"key": {"name": "timestamp"}, "direction": "desc"},
        {"key": {"name": "id"}, "direction": "desc"},
    ]


def test_old_metric_catalog_orders_by_supported_timeseries_column():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": "success", "data": {"metrics": []}}

    with (
        patch.object(module, "_server_info", return_value={"version": "0.97.1"}),
        patch.object(module, "_request", side_effect=request),
    ):
        result = module._cmd_metrics(
            connection(), ["--limit", "20", "--start", "1", "--end", "2"])

    assert result["status"] == "success"
    assert calls[0][0:2] == ("POST", "/api/v1/metrics")
    assert calls[0][2]["json_body"]["orderBy"] == {
        "columnName": "timeseries", "order": "desc"}


def test_v097_services_converts_milliseconds_to_nanoseconds():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return []

    with (
        patch.object(module, "_server_info", return_value={"version": "0.97.1"}),
        patch.object(module, "_request", side_effect=request),
    ):
        module._cmd_services(
            connection(), ["--start", "1000", "--end", "2000"])

    assert calls[0][0:2] == ("POST", "/api/v1/services")
    assert calls[0][2]["json_body"] == {
        "start": "1000000000", "end": "2000000000"}


def test_read_only_api_write_refuses_before_reading_body():
    with pytest.raises(SystemExit) as exc:
        module._cmd_api(
            connection(allow_write=False),
            ["POST", "/api/v1/rules", "--data", "/does/not/exist"])
    assert exc.value.code == 4


def test_ingestion_plan_is_secret_free_and_deployment_specific():
    cloud = module._ingestion_plan(connection(
        deployment="cloud",
        otlp_endpoint="https://ingest.eu.signoz.cloud:443",
        ingestion_key="cloud-ingestion-secret",
    ))
    self_hosted = module._ingestion_plan(connection(
        deployment="self_hosted",
        ingestion_key="ignored-placeholder",
    ))
    assert cloud["ingestion_auth"] == "signoz-ingestion-key"
    assert cloud["env"]["OTEL_EXPORTER_OTLP_HEADERS"] == \
        "signoz-ingestion-key=${SIGNOZ_INGESTION_KEY}"
    assert "cloud-ingestion-secret" not in json.dumps(cloud)
    assert self_hosted["ingestion_auth"] == "none"
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in self_hosted["env"]


def test_custom_headers_cannot_override_management_key():
    with pytest.raises(SystemExit) as exc:
        module._parse_custom_headers('{"SIGNOZ-API-KEY":"wrong"}')
    assert exc.value.code == 6


def test_dimension_mapping_supports_shared_and_per_signal_fields():
    dimensions = module._dimension_fields({
        "dimensions": {
            "agent": "voice.agent_id",
            "call": {
                "logs": "voice.call_id",
                "traces": "conversation_id",
            },
        },
    })
    assert dimensions["agent"] == {
        "logs": "voice.agent_id", "traces": "voice.agent_id"}
    assert dimensions["call"] == {
        "logs": "voice.call_id", "traces": "conversation_id"}
    assert dimensions["room"] == {
        "logs": "room_id", "traces": "room_id"}


def test_dimension_mapping_rejects_query_expression_injection():
    with pytest.raises(SystemExit) as exc:
        module._dimension_fields({
            "dimensions": {"call": "call_id OR true"},
        })
    assert exc.value.code == 6


def test_signal_shortcuts_combine_with_existing_filters():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": "success", "data": {}}

    conn = connection(dimensions={
        "agent": {"logs": "voice.agent_id", "traces": "span.agent_id"},
        "call": {"logs": "voice.call_id", "traces": "span.call_id"},
        "room": {"logs": "voice.room_id", "traces": "span.room_id"},
    })
    with patch.object(module, "_request", side_effect=request):
        module._cmd_signal_search(conn, "logs", [
            "search",
            "--service", "worker",
            "--agent-id", "agent-'one",
            "--call-id", "call-1",
            "--room-id", "room-1",
            "--filter", "environment = 'production'",
            "--start", "1",
            "--end", "2",
        ])

    spec = calls[0][2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    assert spec["filter"]["expression"] == (
        "environment = 'production' AND service.name = 'worker' AND "
        "voice.agent_id = 'agent-\\'one' AND voice.call_id = 'call-1' AND "
        "voice.room_id = 'room-1'")


def test_correlated_call_queries_logs_and_traces_with_mapped_fields():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": "success", "data": {}}

    conn = connection(dimensions={
        "agent": {"logs": "agent_id", "traces": "agent_id"},
        "call": {"logs": "log.call_id", "traces": "span.call_id"},
        "room": {"logs": "room_id", "traces": "room_id"},
    })
    with patch.object(module, "_request", side_effect=request):
        result = module._cmd_correlated(conn, "call", [
            "call-123",
            "--service", "worker",
            "--limit", "5",
            "--start", "1",
            "--end", "2",
        ])

    assert result["entity"] == "call"
    assert result["id"] == "call-123"
    assert set(result["data"]) == {"logs", "traces"}
    assert len(calls) == 2
    log_spec = calls[0][2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    trace_spec = calls[1][2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    assert log_spec["signal"] == "logs"
    assert log_spec["limit"] == 5
    assert log_spec["filter"]["expression"] == (
        "service.name = 'worker' AND log.call_id = 'call-123'")
    assert trace_spec["signal"] == "traces"
    assert trace_spec["filter"]["expression"] == (
        "service.name = 'worker' AND span.call_id = 'call-123'")


def test_correlated_agent_can_select_one_signal():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": "success", "data": {}}

    with patch.object(module, "_request", side_effect=request):
        result = module._cmd_correlated(connection(), "agent", [
            "agent-1", "--signal", "traces", "--start", "1", "--end", "2"])

    assert set(result["data"]) == {"traces"}
    assert set(result["filters"]) == {"traces"}
    assert len(calls) == 1


def test_environment_validation_rejects_empty_and_invalid_chars():
    with pytest.raises(SystemExit) as exc:
        module._validate_environment("")
    assert exc.value.code == 6
    with pytest.raises(SystemExit) as exc:
        module._validate_environment("prod space")
    assert exc.value.code == 6
    with pytest.raises(SystemExit) as exc:
        module._validate_environment("prod'OR'1")
    assert exc.value.code == 6
    assert module._validate_environment("production") == "production"
    assert module._validate_environment("staging-v2") == "staging-v2"
    assert module._validate_environment("dev.local") == "dev.local"


def test_connection_with_environment_includes_it_in_report(tmp_path, monkeypatch):
    (tmp_path / "capabilities").mkdir()
    (tmp_path / "capabilities" / "settings.json").write_text("{}")
    (tmp_path / "capabilities" / "signoz").mkdir()
    (tmp_path / "capabilities" / "signoz" / "connections.json").write_text(
        json.dumps({
            "connections": {
                "prod": {
                    "url": "https://signoz.example",
                    "api_key_env": "SIGNOZ_KEY",
                    "environment": "production",
                }
            }
        })
    )
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("SIGNOZ_KEY", "secret")
    resolved = module._resolve_connection("prod")
    assert resolved["environment"] == "production"
    report = module._connection_report(resolved)
    assert report["environment"] == "production"


def test_connection_without_environment_omits_from_report(tmp_path, monkeypatch):
    (tmp_path / "capabilities").mkdir()
    (tmp_path / "capabilities" / "settings.json").write_text("{}")
    (tmp_path / "capabilities" / "signoz").mkdir()
    (tmp_path / "capabilities" / "signoz" / "connections.json").write_text(
        json.dumps({
            "connections": {
                "prod": {
                    "url": "https://signoz.example",
                    "api_key_env": "SIGNOZ_KEY",
                }
            }
        })
    )
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("SIGNOZ_KEY", "secret")
    resolved = module._resolve_connection("prod")
    assert resolved.get("environment") is None
    report = module._connection_report(resolved)
    assert "environment" not in report


def test_environment_filter_checks_both_field_variants():
    def request(_conn, method, path, **kwargs):
        return {"data": {"keys": [
            {"name": "deployment.environment"},
            {"name": "deployment.environment.name"},
        ]}}
    
    conn = connection()
    with patch.object(module, "_request", side_effect=request):
        filters = module._environment_filter(conn, "logs", "production")
    
    assert len(filters) == 1
    assert "deployment.environment = 'production'" in filters[0]
    assert "deployment.environment.name = 'production'" in filters[0]
    assert " OR " in filters[0]


def test_environment_filter_escapes_special_chars():
    def request(_conn, method, path, **kwargs):
        return {"data": {"keys": [{"name": "deployment.environment"}]}}
    
    conn = connection()
    with patch.object(module, "_request", side_effect=request):
        filters = module._environment_filter(conn, "logs", "prod'test")
    
    assert "\\'test" in filters[0]


def test_log_search_with_connection_environment_adds_automatic_filter():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/v1/fields/keys":
            return {"data": {"keys": [
                {"name": "deployment.environment"},
                {"name": "deployment.environment.name"},
            ]}}
        return {"status": "success", "data": {}}

    conn = connection(environment="production")
    with patch.object(module, "_request", side_effect=request):
        module._cmd_signal_search(conn, "logs", [
            "search", "--service", "api", "--start", "1", "--end", "2"])

    query_call = [c for c in calls if c[1] == "/api/v5/query_range"][0]
    spec = query_call[2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    expr = spec["filter"]["expression"]
    assert "deployment.environment = 'production'" in expr
    assert "deployment.environment.name = 'production'" in expr
    assert "service.name = 'api'" in expr


def test_log_search_environment_override_replaces_connection_scope():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/v1/fields/keys":
            return {"data": {"keys": [{"name": "deployment.environment"}]}}
        return {"status": "success", "data": {}}

    conn = connection(environment="production")
    with patch.object(module, "_request", side_effect=request):
        module._cmd_signal_search(conn, "logs", [
            "search", "--environment", "staging", "--start", "1", "--end", "2"])

    query_call = [c for c in calls if c[1] == "/api/v5/query_range"][0]
    spec = query_call[2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    expr = spec["filter"]["expression"]
    assert "staging" in expr
    assert "production" not in expr


def test_log_search_all_environments_removes_scope():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": "success", "data": {}}

    conn = connection(environment="production")
    with patch.object(module, "_request", side_effect=request):
        module._cmd_signal_search(conn, "logs", [
            "search", "--all-environments", "--start", "1", "--end", "2"])

    spec = calls[0][2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    expr = spec["filter"]["expression"]
    assert "deployment.environment" not in expr
    assert expr == ""


def test_log_search_legacy_unscoped_removes_environment_filter():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": "success", "data": {}}

    conn = connection(environment="production")
    with patch.object(module, "_request", side_effect=request):
        module._cmd_signal_search(conn, "logs", [
            "search", "--legacy-unscoped", "--service", "api",
            "--start", "1", "--end", "2"])

    spec = calls[0][2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    expr = spec["filter"]["expression"]
    assert "deployment.environment" not in expr
    assert "service.name = 'api'" in expr


def test_all_environments_and_environment_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        module._cmd_signal_search(connection(), "logs", [
            "search", "--all-environments", "--environment", "dev",
            "--start", "1", "--end", "2"])
    assert exc.value.code == 6


def test_legacy_unscoped_and_environment_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        module._cmd_signal_search(connection(), "logs", [
            "search", "--legacy-unscoped", "--environment", "dev",
            "--start", "1", "--end", "2"])
    assert exc.value.code == 6


def test_correlated_call_applies_environment_to_both_signals():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/v1/fields/keys":
            return {"data": {"keys": [{"name": "deployment.environment"}]}}
        return {"status": "success", "data": {}}

    conn = connection(environment="production")
    with patch.object(module, "_request", side_effect=request):
        result = module._cmd_correlated(conn, "call", [
            "call-1", "--start", "1", "--end", "2"])

    assert result["environment_scope"] == "production"
    query_calls = [c for c in calls if c[1] == "/api/v5/query_range"]
    assert len(query_calls) == 2
    for call in query_calls:
        spec = call[2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
        expr = spec["filter"]["expression"]
        assert "deployment.environment = 'production'" in expr
        assert "call_id = 'call-1'" in expr


def test_correlated_call_respects_all_environments_flag():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": "success", "data": {}}

    conn = connection(environment="production")
    with patch.object(module, "_request", side_effect=request):
        result = module._cmd_correlated(conn, "call", [
            "call-1", "--all-environments", "--start", "1", "--end", "2"])

    assert "environment_scope" not in result
    assert "scope_filter" not in result
    for call in calls:
        spec = call[2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
        expr = spec["filter"]["expression"]
        assert "deployment.environment" not in expr


def test_ingestion_plan_without_environment_has_no_resource_attributes():
    plan = module._ingestion_plan(connection())
    assert "OTEL_RESOURCE_ATTRIBUTES" not in plan["env"]
    assert "environment" not in plan


def test_ingestion_plan_with_environment_sets_both_fields():
    plan = module._ingestion_plan(connection(
        environment="production",
        otlp_endpoint="https://collector:4318"))
    assert plan["environment"] == "production"
    attrs = plan["env"]["OTEL_RESOURCE_ATTRIBUTES"]
    assert "deployment.environment=production" in attrs
    assert "deployment.environment.name=production" in attrs
    assert "environment_note" in plan


def test_ingestion_plan_does_not_leak_environment_value():
    plan = module._ingestion_plan(connection(environment="secret-env"))
    plan_json = json.dumps(plan)
    assert "secret-env" in plan_json
    assert plan["environment"] == "secret-env"


def test_scope_filter_validation_rejects_empty_and_whitespace():
    with pytest.raises(SystemExit) as exc:
        module._validate_scope_filter("")
    assert exc.value.code == 6
    with pytest.raises(SystemExit) as exc:
        module._validate_scope_filter("   ")
    assert exc.value.code == 6
    assert module._validate_scope_filter("service.name = 'prod'") == "service.name = 'prod'"
    assert module._validate_scope_filter("  filter  ") == "filter"


def test_scope_filter_validation_rejects_excessive_length():
    long_filter = "x" * 10_001
    with pytest.raises(SystemExit) as exc:
        module._validate_scope_filter(long_filter)
    assert exc.value.code == 6
    reasonable_filter = "x" * 10_000
    assert module._validate_scope_filter(reasonable_filter) == reasonable_filter


def test_connection_with_scope_filter_includes_in_report(tmp_path, monkeypatch):
    (tmp_path / "capabilities").mkdir()
    (tmp_path / "capabilities" / "settings.json").write_text("{}")
    (tmp_path / "capabilities" / "signoz").mkdir()
    (tmp_path / "capabilities" / "signoz" / "connections.json").write_text(
        json.dumps({
            "connections": {
                "prod": {
                    "url": "https://signoz.example",
                    "api_key_env": "SIGNOZ_KEY",
                    "scope_filter": "service.name = 'production'",
                }
            }
        })
    )
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("SIGNOZ_KEY", "secret")
    resolved = module._resolve_connection("prod")
    assert resolved["scope_filter"] == "service.name = 'production'"
    report = module._connection_report(resolved)
    assert report["scope_filter"] == "service.name = 'production'"


def test_connection_without_scope_filter_omits_from_report(tmp_path, monkeypatch):
    (tmp_path / "capabilities").mkdir()
    (tmp_path / "capabilities" / "settings.json").write_text("{}")
    (tmp_path / "capabilities" / "signoz").mkdir()
    (tmp_path / "capabilities" / "signoz" / "connections.json").write_text(
        json.dumps({
            "connections": {
                "prod": {
                    "url": "https://signoz.example",
                    "api_key_env": "SIGNOZ_KEY",
                }
            }
        })
    )
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("SIGNOZ_KEY", "secret")
    resolved = module._resolve_connection("prod")
    assert resolved.get("scope_filter") is None
    report = module._connection_report(resolved)
    assert "scope_filter" not in report


def test_cli_environment_overrides_scope_filter():
    conn = connection(
        environment="production",
        scope_filter="service.name = 'production'"
    )
    
    class Args:
        environment = "staging"
        all_environments = False
        legacy_unscoped = False
    
    scope_type, scope_value = module._resolve_effective_scope(conn, Args())
    assert scope_type == "environment"
    assert scope_value == "staging"


def test_scope_filter_replaces_environment_when_both_present():
    conn = connection(
        environment="production",
        scope_filter="(deployment.environment = 'production' OR service.name = 'production')"
    )
    
    class Args:
        environment = None
        all_environments = False
        legacy_unscoped = False
    
    scope_type, scope_value = module._resolve_effective_scope(conn, Args())
    assert scope_type == "filter"
    assert scope_value == "(deployment.environment = 'production' OR service.name = 'production')"


def test_scope_filter_used_when_no_environment():
    conn = connection(scope_filter="custom.field = 'value'")
    
    class Args:
        environment = None
        all_environments = False
        legacy_unscoped = False
    
    scope_type, scope_value = module._resolve_effective_scope(conn, Args())
    assert scope_type == "filter"
    assert scope_value == "custom.field = 'value'"


def test_all_environments_overrides_scope_filter():
    conn = connection(scope_filter="service.name = 'production'")
    
    class Args:
        environment = None
        all_environments = True
        legacy_unscoped = False
    
    scope_type, scope_value = module._resolve_effective_scope(conn, Args())
    assert scope_type is None
    assert scope_value is None


def test_legacy_unscoped_overrides_scope_filter():
    conn = connection(scope_filter="service.name = 'production'")
    
    class Args:
        environment = None
        all_environments = False
        legacy_unscoped = True
    
    scope_type, scope_value = module._resolve_effective_scope(conn, Args())
    assert scope_type is None
    assert scope_value is None


def test_discover_environment_fields_both_exist():
    def request(_conn, method, path, **kwargs):
        return {"data": {"keys": [
            {"name": "deployment.environment"},
            {"name": "deployment.environment.name"},
            {"name": "other.field"},
        ]}}
    
    conn = connection()
    with patch.object(module, "_request", side_effect=request):
        fields = module._discover_environment_fields(conn, "logs")
    
    assert set(fields) == {"deployment.environment", "deployment.environment.name"}
    assert "_discovered_env_fields_logs" in conn


def test_discover_environment_fields_only_old():
    def request(_conn, method, path, **kwargs):
        return {"data": {"keys": [
            {"name": "deployment.environment"},
        ]}}
    
    conn = connection()
    with patch.object(module, "_request", side_effect=request):
        fields = module._discover_environment_fields(conn, "logs")
    
    assert fields == ["deployment.environment"]


def test_discover_environment_fields_only_new():
    def request(_conn, method, path, **kwargs):
        return {"data": {"keys": [
            {"name": "deployment.environment.name"},
        ]}}
    
    conn = connection()
    with patch.object(module, "_request", side_effect=request):
        fields = module._discover_environment_fields(conn, "logs")
    
    assert fields == ["deployment.environment.name"]


def test_discover_environment_fields_none_exist():
    def request(_conn, method, path, **kwargs):
        return {"data": {"keys": [{"name": "other.field"}]}}
    
    conn = connection()
    with patch.object(module, "_request", side_effect=request):
        fields = module._discover_environment_fields(conn, "logs")
    
    assert fields == []


def test_discover_environment_fields_cached():
    call_count = [0]
    
    def request(_conn, method, path, **kwargs):
        call_count[0] += 1
        return {"data": {"keys": [{"name": "deployment.environment"}]}}
    
    conn = connection()
    with patch.object(module, "_request", side_effect=request):
        fields1 = module._discover_environment_fields(conn, "logs")
        fields2 = module._discover_environment_fields(conn, "logs")
    
    assert fields1 == fields2 == ["deployment.environment"]
    assert call_count[0] == 1


def test_build_scope_filter_for_environment_both_fields():
    def request(_conn, method, path, **kwargs):
        return {"data": {"keys": [
            {"name": "deployment.environment"},
            {"name": "deployment.environment.name"},
        ]}}
    
    conn = connection()
    with patch.object(module, "_request", side_effect=request):
        filters = module._build_scope_filter(conn, "logs", "environment", "production")
    
    assert len(filters) == 1
    assert "deployment.environment = 'production'" in filters[0]
    assert "deployment.environment.name = 'production'" in filters[0]
    assert " OR " in filters[0]


def test_build_scope_filter_for_environment_one_field():
    def request(_conn, method, path, **kwargs):
        return {"data": {"keys": [{"name": "deployment.environment"}]}}
    
    conn = connection()
    with patch.object(module, "_request", side_effect=request):
        filters = module._build_scope_filter(conn, "logs", "environment", "production")
    
    assert len(filters) == 1
    assert filters[0] == "deployment.environment = 'production'"
    assert " OR " not in filters[0]


def test_build_scope_filter_for_environment_no_fields_fails():
    def request(_conn, method, path, **kwargs):
        return {"data": {"keys": []}}
    
    conn = connection()
    with patch.object(module, "_request", side_effect=request):
        with pytest.raises(SystemExit) as exc:
            module._build_scope_filter(conn, "logs", "environment", "production")
    
    assert exc.value.code == 6


def test_build_scope_filter_for_filter_type():
    custom_filter = "service.name = 'prod' AND region = 'us-west'"
    filters = module._build_scope_filter(connection(), "logs", "filter", custom_filter)
    assert len(filters) == 1
    assert filters[0] == custom_filter


def test_build_scope_filter_for_none_type():
    filters = module._build_scope_filter(connection(), "logs", None, None)
    assert filters == []


def test_log_search_uses_scope_filter():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": "success", "data": {}}

    conn = connection(scope_filter="(service.name = 'production' OR env = 'prod')")
    with patch.object(module, "_request", side_effect=request):
        module._cmd_signal_search(conn, "logs", [
            "search", "--service", "api", "--start", "1", "--end", "2"])

    spec = calls[0][2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    expr = spec["filter"]["expression"]
    assert "(service.name = 'production' OR env = 'prod')" in expr
    assert "service.name = 'api'" in expr


def test_trace_search_uses_scope_filter():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": "success", "data": {}}

    conn = connection(scope_filter="custom_field = 'value'")
    with patch.object(module, "_request", side_effect=request):
        module._cmd_signal_search(conn, "traces", [
            "search", "--operation", "POST /api", "--start", "1", "--end", "2"])

    spec = calls[0][2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    expr = spec["filter"]["expression"]
    assert "custom_field = 'value'" in expr
    assert "name = 'POST /api'" in expr


def test_scope_filter_ands_with_user_filter():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": "success", "data": {}}

    conn = connection(scope_filter="env = 'prod'")
    with patch.object(module, "_request", side_effect=request):
        module._cmd_signal_search(conn, "logs", [
            "search", "--filter", "status = 'error'", "--start", "1", "--end", "2"])

    spec = calls[0][2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    expr = spec["filter"]["expression"]
    assert "status = 'error'" in expr
    assert "env = 'prod'" in expr
    assert " AND " in expr


def test_correlated_call_with_scope_filter():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": "success", "data": {}}

    conn = connection(
        scope_filter="(deployment.environment = 'prod' OR service.name = 'production')"
    )
    with patch.object(module, "_request", side_effect=request):
        result = module._cmd_correlated(conn, "call", [
            "call-1", "--start", "1", "--end", "2"])

    assert result.get("scope_filter") == "(deployment.environment = 'prod' OR service.name = 'production')"
    assert "environment_scope" not in result
    assert len(calls) == 2
    for call in calls:
        spec = call[2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
        expr = spec["filter"]["expression"]
        assert "(deployment.environment = 'prod' OR service.name = 'production')" in expr


def test_correlated_call_with_environment_not_scope_filter():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/v1/fields/keys":
            return {"data": {"keys": [{"name": "deployment.environment"}]}}
        return {"status": "success", "data": {}}

    conn = connection(environment="production")
    with patch.object(module, "_request", side_effect=request):
        result = module._cmd_correlated(conn, "call", [
            "call-1", "--start", "1", "--end", "2"])

    assert result.get("environment_scope") == "production"
    assert "scope_filter" not in result


def test_precedence_cli_environment_beats_both():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/v1/fields/keys":
            return {"data": {"keys": [{"name": "deployment.environment"}]}}
        return {"status": "success", "data": {}}

    conn = connection(
        environment="production",
        scope_filter="service.name = 'production'"
    )
    with patch.object(module, "_request", side_effect=request):
        module._cmd_signal_search(conn, "logs", [
            "search", "--environment", "staging", "--start", "1", "--end", "2"])

    query_call = [c for c in calls if c[1] == "/api/v5/query_range"][0]
    spec = query_call[2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    expr = spec["filter"]["expression"]
    assert "deployment.environment = 'staging'" in expr
    assert "service.name = 'production'" not in expr


def test_scope_filter_with_legacy_unscoped_no_scope():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": "success", "data": {}}

    conn = connection(scope_filter="env = 'prod'")
    with patch.object(module, "_request", side_effect=request):
        module._cmd_signal_search(conn, "logs", [
            "search", "--legacy-unscoped", "--start", "1", "--end", "2"])

    spec = calls[0][2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    expr = spec["filter"]["expression"]
    assert expr == ""


def test_correlated_call_per_signal_field_discovery():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/v1/fields/keys":
            signal = kwargs["params"]["signal"]
            if signal == "logs":
                return {"data": {"keys": [{"name": "deployment.environment"}]}}
            else:
                return {"data": {"keys": [
                    {"name": "deployment.environment"},
                    {"name": "deployment.environment.name"},
                ]}}
        return {"status": "success", "data": {}}

    conn = connection(environment="production")
    with patch.object(module, "_request", side_effect=request):
        result = module._cmd_correlated(conn, "call", [
            "call-1", "--start", "1", "--end", "2"])

    query_calls = [c for c in calls if c[1] == "/api/v5/query_range"]
    assert len(query_calls) == 2
    
    log_call = query_calls[0]
    log_spec = log_call[2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    log_expr = log_spec["filter"]["expression"]
    assert "deployment.environment = 'production'" in log_expr
    assert " OR " not in log_expr
    
    trace_call = query_calls[1]
    trace_spec = trace_call[2]["json_body"]["compositeQuery"]["queries"][0]["spec"]
    trace_expr = trace_spec["filter"]["expression"]
    assert "deployment.environment = 'production'" in trace_expr
    assert "deployment.environment.name = 'production'" in trace_expr
    assert " OR " in trace_expr


def test_environment_scope_fails_when_no_fields_exist():
    def request(_conn, method, path, **kwargs):
        if path == "/api/v1/fields/keys":
            return {"data": {"keys": []}}
        return {"status": "success", "data": {}}

    conn = connection(environment="production")
    with patch.object(module, "_request", side_effect=request):
        with pytest.raises(SystemExit) as exc:
            module._cmd_signal_search(conn, "logs", [
                "search", "--start", "1", "--end", "2"])
    
    assert exc.value.code == 6


def _fields_keys(entries):
    """The catalogue arrives keyed by field name, each name carrying one entry
    per context — the shape a live server returns."""
    def request(_conn, method, path, **kwargs):
        if path == "/api/v1/fields/keys":
            search = kwargs["params"]["searchText"]
            matched = [e for e in entries if e["name"] == search]
            return {"data": {"keys": {search: matched} if matched else {}}}
        return {"status": "success", "data": {}}
    return request


def test_select_appends_resolved_fields_to_trace_defaults():
    calls = []
    entries = [{"name": "http.route", "fieldContext": "span",
                "fieldDataType": "string"}]
    inner = _fields_keys(entries)

    def request(conn, method, path, **kwargs):
        calls.append((path, kwargs))
        return inner(conn, method, path, **kwargs)

    with patch.object(module, "_request", side_effect=request):
        module._cmd_signal_search(connection(), "traces", [
            "search", "--select", "http.route", "--start", "1", "--end", "2"])

    body = [c for c in calls if c[0] == "/api/v5/query_range"][0][1]["json_body"]
    fields = body["compositeQuery"]["queries"][0]["spec"]["selectFields"]
    assert fields[0]["name"] == "trace_id"
    assert fields[-1] == {"name": "http.route", "fieldDataType": "string",
                          "signal": "traces", "fieldContext": "span"}


def test_select_accepts_comma_separated_names():
    calls = []
    entries = [
        {"name": "http.route", "fieldContext": "span",
         "fieldDataType": "string"},
        {"name": "http.status_code", "fieldContext": "span",
         "fieldDataType": "int64"},
    ]
    inner = _fields_keys(entries)

    def request(conn, method, path, **kwargs):
        calls.append((path, kwargs))
        return inner(conn, method, path, **kwargs)

    with patch.object(module, "_request", side_effect=request):
        module._cmd_signal_search(connection(), "traces", [
            "search", "--select", "http.route,http.status_code",
            "--start", "1", "--end", "2"])

    body = [c for c in calls if c[0] == "/api/v5/query_range"][0][1]["json_body"]
    fields = body["compositeQuery"]["queries"][0]["spec"]["selectFields"]
    assert [f["name"] for f in fields[-2:]] == ["http.route", "http.status_code"]
    assert fields[-1]["fieldDataType"] == "int64"


def test_logs_search_has_no_select_because_rows_carry_every_attribute():
    """Naming selectFields on a logs query replaces the whole record rather
    than adding to it, and a log row already carries every attribute."""
    with pytest.raises(SystemExit) as exc:
        module._cmd_signal_search(connection(), "logs", [
            "search", "--select", "http.route", "--start", "1", "--end", "2"])
    assert exc.value.code == 6


def test_select_rejects_an_unknown_field():
    with patch.object(module, "_request", side_effect=_fields_keys([])):
        with pytest.raises(SystemExit) as exc:
            module._cmd_signal_search(connection(), "traces", [
                "search", "--select", "nope", "--start", "1", "--end", "2"])
    assert exc.value.code == 3


def test_select_rejects_an_ambiguous_name_until_qualified():
    entries = [
        {"name": "host.name", "fieldContext": "resource",
         "fieldDataType": "string"},
        {"name": "host.name", "fieldContext": "span",
         "fieldDataType": "string"},
    ]
    with patch.object(module, "_request", side_effect=_fields_keys(entries)):
        with pytest.raises(SystemExit) as exc:
            module._cmd_signal_search(connection(), "traces", [
                "search", "--select", "host.name", "--start", "1", "--end", "2"])
        assert exc.value.code == 6
        assert module._select_field(
            connection(), "traces", "resource:host.name")["fieldContext"] == "resource"


def test_correlated_select_reaches_the_traces_half_only():
    calls = []
    entries = [{"name": "http.route", "fieldContext": "span",
                "fieldDataType": "string"}]
    inner = _fields_keys(entries)

    def request(conn, method, path, **kwargs):
        calls.append((path, kwargs))
        return inner(conn, method, path, **kwargs)

    with patch.object(module, "_request", side_effect=request):
        module._cmd_correlated(connection(), "call", [
            "call-123", "--select", "http.route",
            "--start", "1", "--end", "2"])

    bodies = [c[1]["json_body"] for c in calls if c[0] == "/api/v5/query_range"]
    specs = {b["compositeQuery"]["queries"][0]["spec"]["signal"]:
             b["compositeQuery"]["queries"][0]["spec"] for b in bodies}
    assert specs["traces"]["selectFields"][-1] == {
        "name": "http.route", "fieldDataType": "string",
        "signal": "traces", "fieldContext": "span"}
    assert "selectFields" not in specs["logs"]


def test_correlated_select_is_refused_when_no_traces_half_is_queried():
    with pytest.raises(SystemExit) as exc:
        module._cmd_correlated(connection(), "agent", [
            "agent-1", "--signal", "logs", "--select", "http.route",
            "--start", "1", "--end", "2"])
    assert exc.value.code == 6


def test_correlated_select_rejects_an_unknown_field():
    with patch.object(module, "_request", side_effect=_fields_keys([])):
        with pytest.raises(SystemExit) as exc:
            module._cmd_correlated(connection(), "call", [
                "call-123", "--select", "nope", "--start", "1", "--end", "2"])
    assert exc.value.code == 3


def test_trace_id_query_collapses_to_one_bucket_on_replaying_servers():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((path, kwargs))
        return {"status": "success", "data": {}}

    with (
        patch.object(module, "_server_info", return_value={"version": "0.97.1"}),
        patch.object(module, "_request", side_effect=request),
    ):
        module._cmd_trace(connection(), ["abc123", "--since", "12h"])

    body = calls[0][1]["json_body"]
    assert body["end"] - body["start"] == module._BUCKET_MS


def test_trace_id_query_keeps_its_window_on_fixed_servers():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((path, kwargs))
        return {"status": "success", "data": {}}

    with (
        patch.object(module, "_server_info", return_value={"version": "0.117.0"}),
        patch.object(module, "_request", side_effect=request),
    ):
        module._cmd_trace(connection(), ["abc123", "--since", "12h"])

    body = calls[0][1]["json_body"]
    assert body["end"] - body["start"] == 12 * module._BUCKET_MS


def test_raw_query_without_a_trace_id_filter_is_sent_unchanged():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((path, kwargs))
        return {"status": "success", "data": {}}

    payload = module._query_payload(
        "traces", 1_000, 1_000 + 12 * module._BUCKET_MS,
        "name = 'checkout'", 100, 0)
    with patch.object(module, "_request", side_effect=request):
        module._query_range(connection(), payload)

    assert calls[0][1]["json_body"] is payload


def test_a_negated_trace_id_filter_keeps_its_window():
    calls = []

    def request(_conn, method, path, **kwargs):
        calls.append((path, kwargs))
        return {"status": "success", "data": {}}

    payload = module._query_payload(
        "traces", 1_000, 1_000 + 12 * module._BUCKET_MS,
        "trace_id != 'abc123'", 100, 0)
    with (
        patch.object(module, "_server_info", return_value={"version": "0.97.1"}),
        patch.object(module, "_request", side_effect=request),
    ):
        module._query_range(connection(), payload)

    assert calls[0][1]["json_body"] is payload
