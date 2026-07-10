# vapic - deviations

This file records `vapic`'s deliberate departures from the default shape. They
are choices made because `vapic` is a harness around the official Vapi CLI, not
a Vapi API client.

## Transport is a subprocess passthrough to `vapi`

vapic resolves one capability connection, injects environment variables, and
runs `vapi` with inherited stdio. The official CLI owns streaming, interactive
prompts, formatting, command-specific output, and exit codes.

`doctor` uses one official read command, `vapi assistant list`, as the cheapest
available credential and reachability probe, with the resilience exception
below. A failed probe is mapped to vapic's own auth/environment categories;
ordinary forwarded commands preserve the official CLI's exit code.

## REST fallback for known Vapi CLI decode failures

Official Vapi CLI v0.2.1 can fail before printing list results when its pinned
Go SDK cannot decode API resources. The tracked upstream reports are
VapiAI/cli [issue 15](https://github.com/VapiAI/cli/issues/15) for assistant
listing and VapiAI/server-sdk-go
[issue 1](https://github.com/VapiAI/server-sdk-go/issues/1),
[issue 6](https://github.com/VapiAI/server-sdk-go/issues/6), and
[issue 8](https://github.com/VapiAI/server-sdk-go/issues/8) for the same
marshal/list class around calls and assistants.

vapic deliberately adds one narrow REST escape hatch for that class of upstream
bug. The trigger is exact: only `vapic assistant list` or `vapic call list`,
after the official `vapi` subprocess exits nonzero with a recognized JSON
unmarshal or deserialization message. Native success, unrelated failures,
commands with additional flags, all other reads, and every write command stay
on the subprocess path.

The fallback calls the official read-only endpoint for the same resource:
`GET /assistant` or `GET /call`, using the connection's `api_base_url` when set
and `https://api.vapi.ai` otherwise. The already-resolved API key is sent only
as an in-process `Authorization: Bearer ...` header. It is never added to argv,
stdout, stderr, or a diagnostic envelope. The project gate and read-only
connection gate run before the fallback can be reached.

On fallback success, vapic prints the raw REST JSON response body to stdout.
On fallback auth failure, vapic exits 2; on fallback HTTP or network failure,
it exits 5. The original CLI decode error is not reprinted after the fallback
path takes ownership of the result. `doctor` uses the same resilient assistant
probe so a valid account with schema-incompatible assistants still passes.

Remove this exception once the official CLI version used by vapic no longer has
the pinned server-sdk-go decode failure and native `vapi assistant list` plus
`vapi call list` are reliable against resources that contain the reported
schema variants.

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
