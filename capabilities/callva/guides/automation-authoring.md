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
- Outbound `fetch()` is allowed; there is no host filesystem and no reach
  outside the workspace — but another script *in the same workspace* is
  importable by path, and that import is an ordinary TypeScript one (see
  "Crossing an automation boundary").
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

## Crossing an automation boundary

Arguments reach an automation two different ways, and the two do not behave the
same.

- **By name.** Over HTTP and over sub-job dispatch the payload is a JSON object
  matched to `main`'s parameters by name — `callva automations run --args`, the
  voice runtime's `http_request` body, and the internal job API below all take
  that form. Key order in the object is insignificant, and a parameter the
  payload omits takes its default.
- **By position.** A direct TypeScript import of another automation is an
  ordinary JavaScript call: arguments bind by position and nothing compares the
  two sides. Reordering, inserting, or removing a parameter in the callee shifts
  every later value into the wrong parameter — no error at the call site, no
  error at deploy, and a run that succeeds while writing wrong data.

The import itself is by path, carrying the `.ts` extension the Deno runtime
requires: `./sibling.ts` or `../other/folder/script.ts` relative to the
importing script, or absolute as `/f/<namespace>/<script>.ts`. The worker
rewrites those paths onto the workspace's raw-script endpoint and fetches them
uncached on every run, so an import always resolves to the callee's *latest
deployed* version rather than to the copy that existed when the caller was
deployed. Only scripts in the same workspace are reachable.

Windmill runs Deno with type checking off, so nothing catches a mismatched call
at deploy or at run. Three shapes are therefore forbidden across this boundary,
each because it ties the caller to the callee's parameter order:

- **Importing another automation's `main`.** `main` is the platform's entry
  point; its parameter list is an argument form for Windmill, not a contract for
  a caller to hold.
- **`Parameters<typeof main>`.** It re-derives from the callee, so it agrees
  with whatever the callee becomes and can never disagree with it — swap two
  same-typed parameters and the tuple stays valid while every value moves.
- **Spreading a positional tuple** into the call (`fn(...args)`), which hides
  the order and the arity from every reader, the author included.

Parameter discipline below makes all three worse: every `main()` parameter
carries a default, so every argument is optional and dropping one is not even an
arity error.

### The shape to use

Export the request as a named interface and a function taking one object of that
shape, and keep `main` a thin Windmill adapter over it that holds no logic:

```typescript
// f/<namespace>/enrich_call.ts
export interface EnrichCallRequest {
  call_id: string;
  target_date?: string;
  dry_run?: boolean;
}

export async function enrichCall(req: EnrichCallRequest) {
  const { call_id, target_date = "", dry_run = false } = req;
  // the real body
}

// Windmill's entry point — argument form only.
export async function main(
  call_id: string = "", target_date: string = "", dry_run: boolean = false,
) {
  return await enrichCall({ call_id, target_date, dry_run });
}
```

A caller imports the function and its interface, never `main`:

```typescript
import { enrichCall, type EnrichCallRequest } from "/f/<namespace>/enrich_call.ts";

const req: EnrichCallRequest = { call_id: id, dry_run: true };
const result = await enrichCall(req);
```

Every argument is named on both sides now. Adding a field is additive, and a
field the caller does not send arrives as `undefined` at the line that uses it
instead of arriving as some other field's value — a loud failure where the
positional shape gave a silent one.

The rule stops at the automation boundary. A pure helper inside one script —
`formatRecord(record, tz)`, `localToUtcIso(stamp, tz)` — keeps ordinary
positional arguments; both sides move together in one file and one deploy, so
they cannot disagree. The context-object bullet under Structure is a readability
call about long signatures and stays scoped to those helpers, while this rule
governs two separately deployed scripts however short the signature.

### Reshaping a callee a caller already depends on

Windmill deploys one script at a time, and a deployed caller picks up the
callee's newest version on its next run, so a new shape is live for every caller
the instant the callee is deployed. There is no atomic two-script deploy and no
window to schedule around; the only safe order is one in which the callee never
stops accepting a shape some caller still sends.

1. **Widen the callee.** Add the new field to the request interface as optional
   and accept both the old and the new shape. Deploy it — nothing a caller
   currently sends has changed meaning, so every caller keeps working.
2. **Move the callers.** Deploy them onto the new shape one at a time, letting
   each run once and reading `callva automations runs <id>` before the next.
3. **Narrow the callee.** Delete the old field and the compatibility branch, and
   deploy that last, once no caller sends the old shape.

Deploying a callee also queues a lock recomputation for every script importing
it, so give the workspace the same few seconds any deploy wants before running a
caller. Never reshape in place: a callee deployed with a renamed or reordered
parameter is already live for callers nobody has touched, and rolling it back is
another deploy (see the deploy-and-test loop) that lands after the wrong runs.

## Parameter discipline

Every `main()` parameter has a sensible default so a cron trigger runs with no
arguments; parameters are *overrides* for ad-hoc runs, not required inputs.
`target_date: string = ""` means "derive from today"; `dry_run: boolean = false`
so scheduled runs are real. When you find yourself wanting a required parameter,
stop and ask whether the value can be derived, loaded from a variable, or
defaulted — a scheduled automation that refuses to run without arguments is a
design bug.

An automation the voice runtime invokes mid-conversation takes the call as a
nested `call` object parameter — that is the argument shape the prepared
per-call run view matches (`callva help`), so its runs stay discoverable under
the call they belong to.

Ask before adding it: does one run of this automation belong to exactly one
conversation? Only then does the parameter mean anything. For a scheduler that
creates hundreds of calls in one run, a sweep or batch over a queue, a
maintenance pass that re-queues stuck records, or a helper that touches no call
at all, the honest answer is "several" or "none" — there is nothing to
correlate, so no `call` parameter. Leaving it out there is the correct and
common outcome, not an omission to fix later.

That parameter is only half the contract; the other half lives in the agent
config, and correlation needs both. The runtime reaches the automation through
an `http_request` tool the agent declares, and a run carries exactly what that
declaration's request body sends — so the body sets the key `call` to
`{"id": "{{context.call_id}}"}`, where `{{context.call_id}}` is the token the
voice runtime substitutes with the live call id. A script whose `main()` takes
`call` faultlessly still produces runs nobody can find under their call when the
declaration sends a flat `call_id`, or sends no call reference at all.

Prove the chain rather than the shape: with the script deployed and the tool
declared, invoke the automation once against a real call id and confirm that
`callva calls automation-runs <that id>` grew by one — that single run is the
evidence both halves hold.

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
