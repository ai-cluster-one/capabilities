# coolify — change log

## 2026-10-04 — A bootstrap guide from a fresh server to a paired instance

`coolify guide bootstrap` takes a setup session from a fresh server reachable as root over SSH to a Coolify instance this machine is paired with: installing Coolify with its root user, minting the API token and handing it to `coolify connect` on standard input, holding auto-update on, serving the instance over HTTPS with its direct ports closed, the git deploy source, a Postgres with TLS enforced ending in `capabilities store set`, application health checks on 127.0.0.1, and reading credentials files with a parser. Each step carries its check and the pitfall that bites there. It was proven against Coolify 4.3.23 and marks the steps that use Coolify internals, to be rechecked on another version. No verb changes.
