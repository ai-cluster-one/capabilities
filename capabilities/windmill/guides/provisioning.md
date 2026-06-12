# Windmill — provisioning the workspace

Standing up (or re-validating) a consuming project's Windmill workspace. The identity and isolation model these create is the `operational` guide; a project's fixed values live in its own identifiers envelope (`windmill ids list`).

Provisioning is **config-driven and file-less** — there is no committed provisioning manifest. `windmill provision` builds the workspace *skeleton* from config plus the scripts dir, idempotently and non-destructively:

- **folder** ← the namespace (`WINDMILL_FOLDER`, or `--folder`).
- **operator user + owners** ← `WINDMILL_OPERATOR` (+ `WINDMILL_OPERATOR_NAME`); owners derive from the operator.
- **scripts** ← every deployable under `--scripts-dir` (path `f/<namespace>/<stem>`; `example_*` skipped).

`windmill verify` is the read-only check of the same skeleton — run it before a work block; it reports per-item `ok` and overall `ready`. The method, flags, and idempotency rules are in `windmill help`.

## The one out-of-band step — the box↔Windmill SSH key

`provision` does **not** create the SSH var trio (`f/<namespace>/agent_ssh_{host,user,key}`). That trio is the box↔Windmill connection, owned by the consuming project's **box-wiring routine**: it mints the key on the box (keygen → `authorized_keys`), then `windmill var-set`s host/user and the private key into the folder. It can only run once the agent box exists, so until then the workspace is provisioned but the box leg is unwired — expected, not a failure. The private key never enters a file; it lives only as the folder-scoped Windmill variable.
