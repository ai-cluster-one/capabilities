# CallVA — automation authoring (Windmill scripts)

CallVA automations are TypeScript scripts that run on Windmill, CallVA's
embedded workflow engine, integrating the platform with external services — a
voice provider, webhooks, a CRM, an SMS gateway. The `callva` CLI is the
authoring loop's I/O: it creates the automation record, deploys script code,
runs it, and reads run history and logs (`callva automations …`; run
`callva help` for exact flags). **The script itself is authored by the agent** —
this guide is how to write one that is debuggable, idempotent, and safe to
schedule. The per-concern facets are their own topics; orchestration (which
automation, what cadence) belongs to a routine in the consuming project.

This guide orients — reach for the focused topic when the task touches it:

- `callva guide idempotency` — dedup so re-runs don't duplicate data.
- `callva guide timezone-handling` — write dates CallVA stores correctly.
- `callva guide resilience-and-retries` — retries, the `Result` type, status taxonomy.
- `callva guide batch-processing` — the structured return shape for batch scripts.
- `callva guide dry-run-pattern` — preview a mutating run before it writes.
- `callva guide multi-phase-pipelines` — structure for 3+ stage scripts.

## Runtime reality

Scripts execute as Deno TypeScript in Windmill's sandboxed workers (nsjail
isolation):

- Deno with `npm:` specifiers (e.g. `import * as wmill from "npm:windmill-client@1"`).
- Outbound `fetch()` is allowed; there is no host filesystem, no cross-project
  access, no local imports.
- One job runs one step to completion with no mid-execution yielding — pick a
  deploy timeout that fits the job's real worst case rather than polling and
  waiting inside a job.
- Deploy as `deno` explicitly; never rely on the engine default.

## Secrets and config — never hardcode

Read every key, URL, and credential at runtime from a Windmill variable; never
inline a secret or a base URL in source:

```typescript
import * as wmill from "npm:windmill-client@1";
const apiKey = await wmill.getVariable("f/<namespace>/CALLVA_API_KEY");
const baseUrl = await wmill.getVariable("f/<namespace>/CALLVA_API_URL");
```

Variable paths are folder-scoped (`f/<namespace>/<NAME>`); discover the
namespace by listing variables (`callva variables list`). At minimum a project
carries `CALLVA_API_KEY` (secret) and `CALLVA_API_URL` (not secret).

## Logging — verbose, tagged, timed

Logs are the primary debugging surface; every script logs with phase tags and
timing. Two conventions by script shape.

Short single-purpose scripts (webhook handlers, single-record mutations) — a
per-step elapsed suffix:

```typescript
function elapsed(start: number): string { return `${Date.now() - start}ms`; }
const t0 = Date.now();
console.log("=== Script Name started ===");
console.log(`[FETCH] Response: ${resp.status} (${elapsed(tFetch)})`);
```

Multi-phase pipelines (3+ stages) — a seconds-from-start prefix on every line,
so the slow stage is visible at a glance:

```typescript
let _t0 = 0;
function log(msg: string) {
  const s = ((Date.now() - _t0) / 1000).toFixed(1);
  console.log(`[${s}s] ${msg}`);
}
```

Prefer per-phase tags (`[INIT]`, `[FETCH]`, `[AUTH]`, `[FORMAT]`, `[DEDUP]`,
`[CREATE]`, `[UPDATE]`, `[DISPATCH]`, `[ERROR]`, `[RETRY]`, `[DONE]`) over
severity tags — `[FETCH]` greps better than `[INFO]`. The full multi-phase
convention is in `callva guide multi-phase-pipelines`.

## Structure — orchestrator, pure transforms, effectful wrappers

```typescript
import * as wmill from "npm:windmill-client@1";

let _t0 = 0;
function log(msg: string) {
  const s = ((Date.now() - _t0) / 1000).toFixed(1);
  console.log(`[${s}s] ${msg}`);
}

// Parameters carry defaults so cron can invoke main() with no arguments.
export async function main(target_date: string = "", dry_run: boolean = false) {
  _t0 = Date.now();
  const apiKey = await wmill.getVariable("f/<namespace>/CALLVA_API_KEY");
  // fetch → transform → persist, in phases
  return { status: "success" /* structured counts — see callva guide batch-processing */ };
}
```

Keep effects and computation apart:

- **Effectful wrappers** — each external call is its own named function
  (`fetchRecords`, `createCall`, `sendSms`); never inline `fetch()` in `main()`.
- **Pure transforms** — each data shaping is its own side-effect-free function
  (`formatRecord`, `localToUtcIso`); no network, no logging, no external mutation.
- **Context objects** — when a pure function needs several shared values, pass a
  typed context object rather than lengthening the signature.

`main()` orchestrates: wrappers fetch, pure functions compute, wrappers persist.
Everything hard to test lives in wrappers; everything interesting lives in pure
functions.

## Parameter discipline

Every `main()` parameter has a sensible default so a cron trigger runs with no
arguments; parameters are *overrides* for ad-hoc runs, not required inputs.
`target_date: string = ""` means "derive from today"; `dry_run: boolean = false`
so scheduled runs are real. When you find yourself wanting a required parameter,
stop and ask whether the value can be derived, loaded from a variable, or
defaulted — a scheduled automation that refuses to run without arguments is a
design bug.

## Return value

Return an object with at least a `status` and enough structure to debug a failed
run without redeploying — never a bare string, a single total, or
`{ status: "success" }` alone. The full outcome-count and drill-down shape, and
the status taxonomy (`success` / `completed_with_errors` / `failed` / `dry_run`),
are in `callva guide batch-processing` and `callva guide resilience-and-retries`.

## Error handling

- **Fatal** — return early with `status: "failed"` and the error in the output.
- **Recoverable** — log, continue, accumulate failures in an errors array, and
  surface them in the return value.
- **Call-status integrity** — never leave a call stuck in a transient state
  (`starting`, `in_progress`); reset it to `scheduled` or `error` on failure.

The full `withRetry` helper, the `Result<T>` discriminated union, and retry
budgets are in `callva guide resilience-and-retries`.

## Sub-job dispatch

When one script triggers another (a runner dispatching processors), POST to the
Windmill internal job API using the worker-injected env:

```typescript
const workspace = Deno.env.get("WM_WORKSPACE") ?? "";
const token = Deno.env.get("WM_TOKEN") ?? "";
const internalUrl = Deno.env.get("BASE_INTERNAL_URL") ?? "";

const resp = await fetch(`${internalUrl}/api/w/${workspace}/jobs/run/p/${scriptPath}`, {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
  body: JSON.stringify({ call }), // keys match the target main() params
});
const jobId = (await resp.text()).replace(/"/g, "");
```

## The deploy-and-test loop

Write code to a local file, then drive it through the CLI (run `callva help` for
flags):

1. Save the script locally.
2. `callva automations deploy <id> --file <path>` — each deploy is a new
   immutable version; roll back by redeploying prior code. A freshly deployed
   script is not runnable until Windmill computes its dependency lock (seconds).
3. For any mutating script, dry-run first:
   `callva automations run <id> --args '{"dry_run":true}'`, then read
   `callva automations run-detail <id> <job_id>` and eyeball the sample (see
   `callva guide dry-run-pattern`).
4. When the preview is right, run live, then check `callva automations runs <id>`
   and the run detail.
5. `callva automations code <id>` fetches the deployed code when you need to
   start from what is live.

Confirm the engine version and timeout with `callva automations runtime-info`
before writing.

## Common shapes

- **Runner (dispatcher)** — polls scheduled calls, locks each with a transient
  status, dispatches to processor scripts.
- **Processor** — takes one call, builds the provider config (voice, prompt,
  transcriber), initiates the outbound call.
- **Webhook handler** — processes an external event, extracts the result,
  updates the call record, stores the transcript.
- **Scheduled job** — runs on a CallVA schedule (`target_type: "automation"`).
