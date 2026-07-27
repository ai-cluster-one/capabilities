# Headless application setup and maintenance

This runbook creates and maintains a Coolify application without opening the
Coolify UI. Run `coolify doctor` first. Application, environment, and lifecycle
writes require the selected connection to have `allow_write: true`; a policy
refusal exits 4 and must not be bypassed.

## Discover infrastructure and source UUIDs

```sh
coolify projects
coolify servers
coolify sources
```

`coolify sources` returns two safe inventories:

- `github_apps[].uuid` is accepted by `app create --github-app`.
- `private_deploy_keys[].uuid` is accepted by
  `app create --private-deploy-key`.

The private-key inventory intentionally omits private and public key material.
Coolify 4.1.x exposes both source types with UUIDs, so numeric `source_id`
fallbacks are not needed.

## Create a private monorepo application

GitHub App example for code in `mcp-server/`:

```sh
coolify app create \
  --project <project-uuid> \
  --server <server-uuid> \
  --environment production \
  --github-app <github-app-uuid> \
  --git-repository owner/repository \
  --git-branch main \
  --build-pack dockerfile \
  --base-directory /mcp-server \
  --dockerfile-location /Dockerfile \
  --ports-exposes 8000 \
  --domains https://mcp.example.com \
  --health-check-enabled \
  --health-check-path /health \
  --health-check-port 8000 \
  --health-check-scheme http \
  --health-check-return-code 200 \
  --health-check-interval 30 \
  --health-check-timeout 5 \
  --health-check-retries 3 \
  --health-check-start-period 10
```

For an SSH deploy key, replace `--github-app` with
`--private-deploy-key <uuid>` and use the repository URL expected by that key,
for example `git@github.com:owner/repository.git`.

`base_directory` is the repository build context. Locations such as
`/Dockerfile` are interpreted relative to it, so the example builds
`mcp-server/Dockerfile` and makes `COPY pyproject.toml ./` resolve inside
`mcp-server/`.

The create result contains the application UUID used by all following
commands.

## Apply environment variables in one request

`env bulk` reads dotenv-style `KEY=VALUE` lines and sends one PATCH. Blank
lines and comments are ignored; values may contain additional `=` characters.

```sh
printf '%s\n' \
  'DATABASE_URL=postgresql://example' \
  'SHARED_SECRET=replace-me' \
  'LOG_LEVEL=info' |
  coolify env bulk <application-uuid>
```

Do not put secrets directly on argv. Generate or read the dotenv body through
stdin.

`coolify env list <application-uuid>` adds `value_hidden` to every row. When it
is true, Coolify withheld the value; it is not evidence that the variable is
empty. `coolify doctor` reports the effective `read:sensitive` result when it
can infer it from a private key or environment row. Coolify does not expose a
token-introspection endpoint, so the result is `null` when the team has no
secret-bearing row to inspect.

## Change an existing application

`app update` has PATCH semantics: only supplied flags are sent. For example,
this changes routing and health checks without touching the repository,
branch, build pack, or build context:

```sh
coolify app update <application-uuid> \
  --ports-exposes 8000 \
  --domains https://mcp.example.com \
  --health-check-enabled \
  --health-check-path /health \
  --health-check-port 8000
```

Disable a health check with `--no-health-check-enabled`. Clear domains by
passing an explicit empty value:

```sh
coolify app update <application-uuid> --domains ''
```

Repository/build changes use the same field names:

```sh
coolify app update <application-uuid> \
  --git-branch release \
  --base-directory /mcp-server \
  --dockerfile-location /Dockerfile \
  --build-pack dockerfile
```

## Deploy and inspect

```sh
coolify deploy <application-uuid>
coolify deployments
coolify logs <application-uuid> --lines 200
coolify applications <application-uuid>
```

Use `deploy --force` only when a cache-free rebuild is intentional. The final
application read should show the requested `ports_exposes`, domains,
`base_directory`, Dockerfile location, and health-check settings.
