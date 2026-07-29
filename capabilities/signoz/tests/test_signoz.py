import json
import types
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

CLI = Path(__file__).parents[1] / "bin" / "signoz"
module = types.ModuleType("signoz_capability")
module.__file__ = str(CLI)
exec(compile(CLI.read_text(), str(CLI), "exec"), module.__dict__)


def connection(**overrides):
    value = {
        "id": "test",
        "url": "https://signoz.example",
        "api_key": "api-secret",
        "otlp_endpoint": "https://collector.example:4318",
        "ingestion_key": None,
        "custom_headers": None,
        "deployment": "self_hosted",
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
        "query", "dashboards", "alerts", "rules", "views", "channels", "api",
    ]:
        assert f"signoz {command}" in help_text
    assert "SIGNOZ_API_KEY" in help_text
    assert "SIGNOZ_INGESTION_KEY" in help_text


def test_implicit_connection_accepts_existing_aliases(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "SIGNOZ_API_URL=https://legacy.example\n"
        "SIGNOZ_SERVICE_KEY=management-secret\n"
        "SIGNOZ_ENDPOINT=https://collector.example:4318\n"
    )
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
