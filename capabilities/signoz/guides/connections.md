# SigNoz connections

SigNoz has two independent credential planes:

1. The management/query API at the workspace URL uses `SIGNOZ-API-KEY`.
   The capability resolves this as `SIGNOZ_API_KEY`.
2. OTLP ingestion sends telemetry to a collector endpoint. SigNoz Cloud
   requires `signoz-ingestion-key`; self-hosted Community normally does not.

An ingestion key cannot list dashboards, query logs, or create alerts. An API
key should never be shipped in application telemetry configuration.

## Self-hosted

```dotenv
SIGNOZ_URL=https://signoz.example.com
SIGNOZ_API_KEY=<PAT-or-service-account-key>
SIGNOZ_OTLP_ENDPOINT=https://collector.signoz.example.com:4318
# no SIGNOZ_INGESTION_KEY
```

Self-hosted defaults are OTLP/gRPC on `4317` and OTLP/HTTP on `4318`. A reverse
proxy may expose either on another port. The management URL is the browser
workspace root, without `/api` and without the collector port.

Self-hosted management authentication depends on the installed SigNoz version.
Older releases call the keys PATs and expose `/api/v1/pats`; newer releases
surface service accounts. Both are sent as `SIGNOZ-API-KEY`.

## SigNoz Cloud

```dotenv
SIGNOZ_URL=https://workspace.<region>.signoz.cloud
SIGNOZ_API_KEY=<service-account-api-key>
SIGNOZ_OTLP_ENDPOINT=https://ingest.<region>.signoz.cloud:443
SIGNOZ_INGESTION_KEY=<ingestion-key>
```

Cloud uses TLS on port `443` for both OTLP/gRPC and OTLP/HTTP. The API key and
ingestion key are distinct even when both are configured in one connection.

## Existing environment aliases

The capability accepts these permanent compatibility aliases:

```text
SIGNOZ_API_URL      -> SIGNOZ_URL
SIGNOZ_SERVICE_KEY  -> SIGNOZ_API_KEY
SIGNOZ_ENDPOINT     -> SIGNOZ_OTLP_ENDPOINT
```

New configuration should use the canonical names. `signoz connections` shows
which name and cascade tier resolved without exposing either secret.

## Connection registry

Every setup declares at least one connection in
`capabilities/signoz/connections.json` (or the user-level registry). Values may
remain in the environment cascade; an empty declared entry is enough to use the
canonical variables and compatibility aliases. A multi-deployment registry
looks like this:

```json
{
  "default": "production",
  "connections": {
    "production": {
      "url": "https://signoz.example.com",
      "api_key_env": "SIGNOZ_PROD_API_KEY",
      "otlp_endpoint": "https://collector.example.com:4318",
      "deployment": "self_hosted",
      "environment": "production",
      "dimensions": {
        "agent": "agent_id",
        "call": "call_id",
        "room": "room_id"
      },
      "allow_write": false
    },
    "cloud-reference": {
      "url": "https://workspace.eu.signoz.cloud",
      "api_key_env": "SIGNOZ_CLOUD_API_KEY",
      "otlp_endpoint": "https://ingest.eu.signoz.cloud:443",
      "ingestion_key_env": "SIGNOZ_CLOUD_INGESTION_KEY",
      "deployment": "cloud",
      "allow_write": false
    }
  }
}
```

Connection files hold endpoint wiring and names of secret environment
variables, never secret values. Writes through `signoz api POST|PUT|PATCH|DELETE`
also require `allow_write: true`.

### Environment scope

The optional `environment` property scopes telemetry queries to a logical
deployment environment. When set, the CLI automatically filters logs and traces
by `deployment.environment` or `deployment.environment.name` (both are checked
for compatibility with SigNoz 0.97.1 and current OpenTelemetry semantic
conventions).

```json
{
  "production": {
    "url": "https://signoz.example.com",
    "api_key_env": "SIGNOZ_PROD_API_KEY",
    "environment": "production"
  }
}
```

Commands that support environment scoping:
- `signoz logs search`
- `signoz traces search`
- `signoz call`
- `signoz agent`

Override behavior per query:
- `--environment dev` — query a specific environment
- `--all-environments` — query all environments (ignore connection scope)
- `--legacy-unscoped` — query historical data without deployment.environment

Workspace catalog commands (`services`, `metrics`, `fields`, `dashboards`,
`alerts`, `rules`, `views`, `channels`) and raw API escape hatches (`query`,
`api`) are never environment-scoped; they operate across the entire workspace.

The `signoz ingestion` command renders both `deployment.environment` and
`deployment.environment.name` in `OTEL_RESOURCE_ATTRIBUTES` when the connection
has an environment, compatible with current and legacy SigNoz versions.

### Custom scope filter

The optional `scope_filter` property provides a custom Query Builder v5 filter
expression for legacy or non-standard telemetry that cannot be cleanly
represented by environment alone. It is narrowly defined for migration scenarios.

```json
{
  "production": {
    "url": "https://signoz.example.com",
    "api_key_env": "SIGNOZ_PROD_API_KEY",
    "environment": "production",
    "scope_filter": "(deployment.environment = 'production' OR deployment.environment.name = 'production' OR service.name = 'production')"
  }
}
```

**Precedence (deterministic):**
1. CLI `--all-environments` or `--legacy-unscoped` → disables connection base scope
2. CLI `--environment <value>` → overrides both `scope_filter` and `environment`
3. Connection `scope_filter` → **replaces** environment-derived scope (not AND)
4. Connection `environment` → generates dual-field deployment.environment scope
5. Command `--filter` → always ANDs with the effective base scope

**When to use scope_filter:**
- Bridging legacy telemetry: match both new `deployment.environment` and legacy
  `service.name` attributes during migration
- Temporary workaround for non-standard telemetry until it can be fixed upstream
- Custom logical scoping that cannot be expressed by environment alone

**When NOT to use scope_filter:**
- If `environment` alone suffices (prefer the standard field)
- Permanent filtering (fix upstream telemetry instead)
- Complex multi-condition logic unrelated to deployment scope

The `scope_filter` is validated for schema type (string), non-empty/whitespace,
and reasonable size (max 10000 chars). It is not parsed or security-validated;
use trusted filter expressions only.

Like `environment`, `scope_filter` applies only to telemetry query commands
(`logs search`, `traces search`, `call`, `agent`). Workspace catalog commands
and raw API escape hatches are never scoped.

**Migration example:**
Set both `environment` and `scope_filter` during transition, then remove
`scope_filter` after legacy data ages out:

```json
{
  "production": {
    "environment": "production",
    "scope_filter": "(deployment.environment = 'production' OR service.name = 'production')"
  }
}
```

After retention window expires (typically 30-90 days), remove `scope_filter`:

```json
{
  "production": {
    "environment": "production"
  }
}
```

The `environment` property remains useful for connection declaration and
`signoz ingestion` output even when `scope_filter` controls read queries.

### Project-specific dimensions

The `agent`, `call`, and `room` dimensions power `--agent-id`, `--call-id`,
`--room-id`, `signoz agent`, and `signoz call`. Their defaults are `agent_id`,
`call_id`, and `room_id` for both logs and traces. Override a dimension with
one shared field name, as above, or use different fields per signal:

```json
{
  "dimensions": {
    "agent": "voice.agent_id",
    "call": {
      "logs": "voice.call_id",
      "traces": "conversation_id"
    },
    "room": "room_id"
  }
}
```

Mapped names must be simple attribute paths such as `call_id` or
`voice.call_id`. For more complex Query Builder expressions, keep the mapping
simple and add the expression with `--filter`.

For a reverse proxy that needs extra authentication, set
`SIGNOZ_CUSTOM_HEADERS` to a JSON object or use `custom_headers_env` in the
connection. The capability refuses overrides of `Host`, `Content-Length`, and
`SIGNOZ-API-KEY`.

Official references:

- [Self-hosted ingestion](https://signoz.io/docs/ingestion/self-hosted/overview/)
- [Cloud vs self-hosted ingestion](https://signoz.io/docs/ingestion/cloud-vs-self-hosted/)
- [Cloud ingestion keys](https://signoz.io/docs/ingestion/signoz-cloud/keys/)
