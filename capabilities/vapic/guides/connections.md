# Vapi Connections

Use one `vapic` connection per Vapi API key. The connection registry selects
which key and Vapi environment a forwarded official CLI command receives.

Example project envelope:

```json
{
  "default": "production",
  "connections": {
    "production": {
      "secret_env": "VAPI_PRODUCTION_API_KEY",
      "environment": "production",
      "allow_write": false
    },
    "staging": {
      "secret_env": "VAPI_STAGING_API_KEY",
      "environment": "staging",
      "allow_write": true
    }
  }
}
```

Secrets live in `.env`, `.env.local`, user credentials, or process env:

```sh
VAPI_PRODUCTION_API_KEY=
VAPI_STAGING_API_KEY=
```

Optional non-secret fields:

- `environment`: `production`, `staging`, or `development`.
- `api_base_url`: explicit `VAPI_API_BASE_URL` override.
- `dashboard_url`: explicit `VAPI_DASHBOARD_URL` override.
- `allow_write`: set `true` only when the connection may place calls or mutate
  Vapi resources.

Run `vapic connections` to see where each value resolves and `vapic doctor` to
check the selected key through the official CLI.
