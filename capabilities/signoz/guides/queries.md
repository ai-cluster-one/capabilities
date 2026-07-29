# Querying SigNoz

Start with discovery rather than guessing workspace-specific field names:

```sh
signoz services --since 24h
signoz metrics --search http --since 24h
signoz fields keys --signal logs --context resource --search service
signoz fields values --signal logs --name service.name --context resource
```

All convenience query commands default to the last hour. Use `--since 30m`,
`--since 7d`, or an explicit millisecond window:

```sh
signoz logs search --start 1785300000000 --end 1785303600000
```

## Logs

```sh
signoz logs search --service checkout --severity ERROR --search timeout
signoz logs search --filter "deployment.environment = 'production'" --limit 200
```

`--service`, `--severity`, `--search`, and `--filter` combine with `AND`.
`--search` emits a Query Builder `body CONTAINS` predicate. Paginate with
`--limit` and `--offset`.

## Traces

```sh
signoz traces search --service checkout --error yes
signoz traces search --operation "POST /orders" --since 6h
signoz trace 0123456789abcdef0123456789abcdef --since 24h
```

Use `trace` after a search has produced a trace ID. Its default window is six
hours; widen it when inspecting an older trace.

## Raw Query Builder v5

When the convenience commands cannot express an aggregation, formula, PromQL,
or ClickHouse SQL query, pass the complete request:

```sh
signoz query query.json
generate-query | signoz query -
```

The body is sent unchanged to `POST /api/v5/query_range`. A minimal raw log
request has this shape:

```json
{
  "schemaVersion": "v1",
  "start": 1785300000000,
  "end": 1785303600000,
  "requestType": "raw",
  "compositeQuery": {
    "queries": [{
      "type": "builder_query",
      "spec": {
        "name": "A",
        "signal": "logs",
        "disabled": false,
        "filter": {"expression": "service.name = 'checkout'"},
        "limit": 100,
        "offset": 0,
        "order": [
          {"key": {"name": "timestamp"}, "direction": "desc"},
          {"key": {"name": "id"}, "direction": "desc"}
        ],
        "having": {"expression": ""}
      }
    }]
  },
  "formatOptions": {
    "formatTableResultForUI": false,
    "fillGaps": false
  },
  "variables": {}
}
```

The capability uses the server version to select legacy or current endpoints
for dashboards, alert rules, and the metric catalog. Query Builder v5 is the
stable data plane across supported versions.

Official references:

- [Logs API](https://signoz.io/docs/logs-management/logs-api/overview/)
- [Trace API](https://signoz.io/docs/traces-management/trace-api/overview/)
- [Metrics Query Range API](https://signoz.io/docs/metrics-management/query-range-api/)
