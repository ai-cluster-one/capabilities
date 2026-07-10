# vapic - deviations

This file records `vapic`'s deliberate departures from the default shape. They
are choices made because `vapic` is a harness around the official Vapi CLI, not
a Vapi API client.

## Transport is a subprocess passthrough to `vapi`

vapic makes no Vapi API calls of its own except through the official `vapi`
binary. It resolves one capability connection, injects environment variables,
and runs `vapi` with inherited stdio. The official CLI owns streaming,
interactive prompts, formatting, command-specific output, and exit codes.

`doctor` uses one official read command, `vapi assistant list`, as the cheapest
available credential and reachability probe. A failed probe is mapped to
vapic's own auth/environment categories; ordinary forwarded commands preserve
the official CLI's exit code.

## Read-only gating is an allowlist

Vapi commands can place outbound calls and mutate phone numbers, assistants,
tools, webhooks, campaigns, and other paid or user-visible resources. For this
reason `WRITE_DEFAULT = False`.

Rather than trying to keep a complete blacklist of every mutating Vapi
subcommand, vapic treats a read-only connection conservatively: only known
read/status/help/log commands pass. Unknown forwarded commands require
`allow_write: true`. This is stricter than the Railway harness because the
blast radius of a missed mutator can include real phone calls.

## Ambient official login state is deliberately ignored

The official Vapi CLI supports a native login/config file. vapic does not fall
back to that state. A vapic command must resolve its API key through the
capability connection cascade and inject it into the child process as
`VAPI_API_KEY`.

This keeps one project invocation from silently using whichever account a human
last selected with the native CLI. Manual exploration remains available through
`vapi` directly; agent work goes through `vapic`.

## `bootstrap` is explicit, not automatic

vapic does not download or install the official CLI while forwarding ordinary
commands. Missing `vapi` is reported by `doctor` and by forwarded commands with
a hint to run `vapic bootstrap`.

`vapic bootstrap` without flags is a dry status/plan command. `vapic bootstrap
--yes` executes the official installer. This keeps normal command execution
predictable while still giving an agent one capability-owned way to repair the
missing dependency when the user allows it.
