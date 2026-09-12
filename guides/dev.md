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

`dev check` derives the manager/package scope from the recorded base and runs each selected direct validator once. Running it by hand is optional feedback and the authoritative validation is the GitHub check for the exact candidate commit, but a failing check does stop a release: `dev finish` runs the same validators itself whenever it brings a session forward.

`dev finish` releases through the source-release transaction, which repeats no source audit of its own.

Any release moves the tracked branch under every other open session, so `dev finish` brings an overtaken session forward rather than refusing it. It replays the prepared commit onto the current tip of the tracked ref with `git rebase --onto`, re-records the session base as that tip, regenerates the staged catalogue and the manager release manifest against it, folds whatever that regeneration staged into the prepared commit with `git commit --amend`, and re-runs the validators `dev check` runs. The published candidate is therefore the replayed commit rather than the commit the author wrote, and the finish result carries a `rebase` object naming the previous base, the previous head, the commit it rebased onto, the regenerated paths and the new candidate. A session that already contains the tip skips the replay; the re-record, the regeneration and the check still run.

A session is left alone when its recorded base is still the tip, when its head is still that base, when its prepared commit already reached the tracked branch, when a release for that ref is mid-transaction, or when its recorded base is not an ancestor of the tip. It is left alone too when its own record gives the replay nothing to work from: no base recorded, or a tracked ref that resolves to no commit. A session whose head is still its base holds no prepared commit, and finish refuses it with `dev_no_change` once the bring-forward has declined it.

A check that fails on the new base refuses the release with `dev_forward_validation_failed` and names the failures. The session stays rebased: its commit is the replayed one and its recorded base is the new tip, so the repair happens on the current tip rather than on the base the session started from.

`dev_rebase_conflict` refuses a replay that conflicts. The replay is aborted before the refusal, so the session is exactly as it was: the branch still points at the prepared commit, the worktree is clean, no rebase is in progress, and the recorded base is unchanged. The refusal names the conflicting paths and the branch, worktree and target commit to bring it forward by hand; resolve, commit, and finish again.

A `release_pending` result preserves the session; run the same command after the integrity gate settles.
Cleanup occurs after publication, checkout reconciliation, and local payload reconciliation succeed.

Telegram live service testing uses the explicit `dev live` surface documented
by `capabilities help`.
