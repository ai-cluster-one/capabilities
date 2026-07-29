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

## Multiple deployments

Use `capabilities/signoz/connections.json` when a project talks to more than
one SigNoz deployment:

```json
{
  "default": "production",
  "connections": {
    "production": {
      "url": "https://signoz.example.com",
      "api_key_env": "SIGNOZ_PROD_API_KEY",
      "otlp_endpoint": "https://collector.example.com:4318",
      "deployment": "self_hosted",
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

For a reverse proxy that needs extra authentication, set
`SIGNOZ_CUSTOM_HEADERS` to a JSON object or use `custom_headers_env` in the
connection. The capability refuses overrides of `Host`, `Content-Length`, and
`SIGNOZ-API-KEY`.

Official references:

- [Self-hosted ingestion](https://signoz.io/docs/ingestion/self-hosted/overview/)
- [Cloud vs self-hosted ingestion](https://signoz.io/docs/ingestion/cloud-vs-self-hosted/)
- [Cloud ingestion keys](https://signoz.io/docs/ingestion/signoz-cloud/keys/)
