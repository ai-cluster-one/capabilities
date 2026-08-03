# Querying SigNoz

Start with discovery rather than guessing workspace-specific field names:

```sh
signoz services --since 24h
signoz metrics --search http --since 24h
signoz fields keys --signal logs --context resource --search service
signoz fields values --signal logs --name service.name --context resource
```

Log and trace searches default to the last hour; correlated `call` and `agent`
lookups default to 24 hours. Use `--since 30m`, `--since 7d`, or an explicit
millisecond window:

```sh
signoz logs search --start 1785300000000 --end 1785303600000
```

## Logs

```sh
signoz logs search --service checkout --severity ERROR --search timeout
signoz logs search --filter "status = 'failed'" --limit 200
signoz logs search --agent-id agent-42 --call-id call-123 --since 24h
signoz logs search --environment staging --service api
signoz logs search --all-environments --service api
signoz logs search --legacy-unscoped --service checkout
```

`--service`, `--agent-id`, `--call-id`, `--room-id`, `--severity`, `--search`,
`--filter`, and the connection's environment (if set) combine with `AND`.
`--search` emits a Query Builder `body CONTAINS` predicate. Paginate with
`--limit` and `--offset`.

A log row carries every attribute and resource of the record, in its
`attributes_string`, `attributes_number`, `attributes_bool` and
`resources_string` maps, so reading one attribute needs nothing extra.

When the connection has `environment: "production"`, queries automatically scope
to that environment. Override with `--environment dev` to query a different
environment, `--all-environments` to query across all environments, or
`--legacy-unscoped` to access historical data recorded before
`deployment.environment` attributes were added.

## Traces

```sh
signoz traces search --service checkout --error yes
signoz traces search --operation "POST /orders" --since 6h
signoz traces search --call-id call-123 --room-id room-7
signoz trace 0123456789abcdef0123456789abcdef --since 24h
```

Use `trace` after a search has produced a trace ID. Its default window is six
hours; widen it when inspecting an older trace.

A span row carries a fixed set of fields rather than the whole record, so an
attribute is read with `--select`:

```sh
signoz traces search --operation checkout --select http.route
signoz traces search --call-id call-123 --select http.route,http.status_code
signoz trace 0123456789abcdef0123456789abcdef --select http.route
```

The flag is repeatable and accepts a comma-separated list, and its names are
added to the default fields. Names resolve against the catalogue
`signoz fields keys` reports, which supplies each field's context and data
type. A name that exists in more than one context is rejected with the choices;
qualify it as `context:name`:

```sh
signoz fields keys --signal traces --search host.name
signoz traces search --service checkout --select resource:host.name
```

SigNoz before 0.117.0 splits a raw query's window into exponentially growing
time buckets and runs the statement once per bucket, while a `trace_id` filter
makes the traces statement builder replace each bucket's bounds with the
trace's own range. Every bucket then runs identical SQL and the rows are
concatenated, so each span is returned once per bucket — a duplication factor
that grows with the window (SigNoz issue 10449, fixed by pull request 10637).
Against such a server the capability narrows to a single bucket any raw traces
request pinned to one trace with `trace_id = '…'` — on `trace`,
`traces search --filter`, and `query` alike. That equality is what makes the
server derive the real range from the trace, so the span set is unaffected;
any other predicate keeps its window.

## Correlated calls and agents

Fetch logs and traces together when the domain identifier is more useful than
a telemetry trace ID:

```sh
signoz call call-123 --since 7d
signoz agent agent-42 --service voice-worker --since 24h
signoz call call-123 --signal traces --limit 50
signoz call call-123 --environment production
signoz agent agent-42 --all-environments
signoz call call-123 --select http.route,resource:host.name
```

`--select` means here what it means on `traces search`, and reaches the traces
half alone: a log row already carries every attribute, so the logs half is
returned untouched. Under `--signal logs` there is no traces half for it to
reach, and the flag is refused rather than quietly dropped.

The result includes the resolved time window, environment scope, the exact
filter used for each signal, and a `data` object containing `logs`, `traces`,
or both. Use `--signal logs|traces|both` to control the round trips. Project
connections can map logical dimensions to different workspace-specific fields
and set a default environment scope; see `signoz guide connections`.

## Migrating from legacy telemetry

When migrating from legacy `service.name`-based environments to standard
`deployment.environment` attributes, use `scope_filter` to bridge the transition.

### Stage A: Before environment fields exist (SigNoz 0.97.1 with no deployment.environment)

Use pure legacy scope_filter:

```json
{
  "connections": {
    "production": {
      "url": "https://signoz.example.com",
      "api_key_env": "SIGNOZ_PROD_API_KEY",
      "scope_filter": "service.name = 'production'"
    }
  }
}
```

Queries work with legacy data. No `environment` property yet because the fields
don't exist in SigNoz.

### Stage B: Transition period (after deployment.environment fields exist)

Once new telemetry includes `deployment.environment` attributes (verified via
`signoz fields keys --signal logs --search deployment`), add `environment` and
update `scope_filter` to match both:

```json
{
  "connections": {
    "production": {
      "url": "https://signoz.example.com",
      "api_key_env": "SIGNOZ_PROD_API_KEY",
      "environment": "production",
      "scope_filter": "(deployment.environment = 'production' OR deployment.environment.name = 'production' OR service.name = 'production')"
    }
  }
}
```

**Start sending new telemetry:**

```bash
signoz ingestion
# Outputs OTEL_RESOURCE_ATTRIBUTES with both deployment.environment fields
# Configure your application to include these in telemetry
```

**Query seamlessly matches both old and new records:**

```bash
# Uses scope_filter, matches legacy service.name=production AND new deployment.environment=production
signoz logs search --service myapp

# Query specific environment (overrides scope_filter)
signoz logs search --environment staging

# Query legacy data only
signoz logs search --legacy-unscoped --filter "service.name = 'production'"

# Query all environments
signoz logs search --all-environments
```

### Stage C: After legacy retention expires

Remove `scope_filter` after legacy `service.name` data ages out (typically 30-90 days):

```json
{
  "connections": {
    "production": {
      "url": "https://signoz.example.com",
      "api_key_env": "SIGNOZ_PROD_API_KEY",
      "environment": "production"
    }
  }
}
```

Now queries use standard discovered `deployment.environment` fields only. The
CLI discovers which environment fields exist per signal and builds the
appropriate filter automatically.

## Raw Query Builder v5

When the convenience commands cannot express an aggregation, formula, PromQL,
or ClickHouse SQL query, pass the complete request:

```sh
signoz query query.json
generate-query | signoz query -
```

The body is sent to `POST /api/v5/query_range` unchanged, apart from the
`trace_id` window narrowing described under Traces. A minimal raw log request
has this shape:

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
