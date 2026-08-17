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

Prepare the candidate without hiding validation inside lifecycle commands:

```sh
./bin/capabilities source index <id> --staged
capabilities dev check <session>  # optional local feedback
git commit
capabilities dev finish <session>
```

`dev check` derives the manager/package scope from the recorded base and runs
each selected direct validator once. It is not a release prerequisite: the
authoritative validation is the GitHub check for the exact candidate commit.

`dev finish` uses the source-release transaction without repeating source
audit. A `release_pending` result preserves the session; run the same command
after the integrity gate settles.
Cleanup occurs after publication, checkout reconciliation, and local payload
reconciliation succeed.

Telegram live service testing uses the explicit `dev live` surface documented
by `capabilities help`.
