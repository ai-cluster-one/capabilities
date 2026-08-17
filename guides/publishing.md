# Publishing a capability source

Use this guide to turn an intentionally prepared Git commit into a verified, published capability-source release.

The release unit is an immutable Git commit. Stage the intended source changes,
then run `capabilities source index <id> --staged`; the generated catalogue is
derived from and added to that same Git index without running authoring audit.
Commit intentionally. `source verify <id> --ref HEAD [--base REF]` is the direct
immutable-tree validator used by CI and by sources without a remote integrity gate.

`capabilities source release <id> --ref HEAD` is the publication boundary. It
accepts a verified fast-forward from a clean integration checkout, publishes a
temporary candidate ref for the remote integrity check, advances the protected
source branch, synchronizes that checkout, and reconciles locally installed
changed payloads.

The operation is idempotent. A pending or interrupted release resumes the same
target commit; a different target waits until that release is resolved. The
manager leaves the commit scope and message to author judgment.

`capabilities dev finish <session>` calls the same transaction with the
session's recorded base; it does not rerun source validation. A `release_pending`
result preserves the session. Run the same finish command after the integrity
gate settles.

Consumers attach the published repository with `capabilities source add`,
inspect it with `capabilities search --source <id>`, and install explicitly with
`capabilities install <name> --source <id>`.
