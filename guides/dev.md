# Isolated capability development

Use this guide to make source changes in a managed worktree, validate them in isolation, and finish through the capability-source release transaction.

Start where the defect was discovered, in the consuming project:

```sh
capabilities dev start <name>
```

From a source checkout pass `--project PATH` or `--no-project`; custom sources
use `--source ID-OR-PATH`. Edit only the returned source worktree.

`dev exec <session> -- <command>` is the hermetic lane with isolated
HOME/registry/XDG roots. Refresh capability payloads with `dev install
<session> <name>`. `dev run <session> <name> -- <args>` runs the exact session
payload against the attached project.

Before publication, derive checks from the recorded base:

```sh
capabilities dev check <session>
./bin/capabilities source index <id> --staged
git commit
capabilities dev check <session>
capabilities dev finish <session>
```

`dev finish` uses the source-release transaction. A `release_pending` result
preserves the session; run the same command after the integrity gate settles.
Cleanup occurs after publication, checkout reconciliation, and local payload
reconciliation succeed.

Telegram live service testing uses the explicit `dev live` surface documented
by `capabilities help`.
