# Deployment Targets

A target lives at `deployment/targets/<name>.json` and uses schema
`capabilities.deployment.target.v1`.

Required fields:

- `name` - target name, usually `production` or `staging`.
- `provider` - `coolify`, `dockerhost`, or `manual`.
- `connection` - provider connection id, or `null` for `manual`.
- `resource` - provider-facing handle data.

Coolify targets should store a resource identifier label, not a raw UUID:

```json
{
  "schema": "capabilities.deployment.target.v1",
  "name": "production",
  "provider": "coolify",
  "connection": "default",
  "environment": "production",
  "resource": {
    "type": "application",
    "identifier_label": "production_resource_uuid"
  }
}
```

The raw Coolify UUID belongs in the Coolify identifiers envelope:

```sh
coolify ids set production_resource_uuid <uuid> --note "production application"
```

Raw-server targets are reserved for a Docker-host adapter:

```json
{
  "schema": "capabilities.deployment.target.v1",
  "name": "production",
  "provider": "dockerhost",
  "connection": "production",
  "environment": "production",
  "resource": {
    "remote_path": "/opt/marvin",
    "compose_project": "marvin"
  }
}
```

`deployment plan` reports whether the provider capability is installed/enabled
locally, but it does not perform remote operations.
