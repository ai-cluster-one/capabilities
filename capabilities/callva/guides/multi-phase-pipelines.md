# CallVA — multi-phase pipelines (3+ stage scripts)

A long automation moves through distinct stages — auth, fetch, transform, dedup, create, post-process — and reads more clearly when that structure is explicit in both code and logs. This guide is one facet of authoring Windmill TypeScript automations with the `callva` tool: how to give a multi-stage pipeline a phase skeleton an operator can navigate, grep, and trace. It is a structuring discipline, not a requirement — short single-purpose scripts have no phases to impose.

## When to use phases

Use phase structure when your automation:

- Runs through 3 or more logically distinct stages
- Takes long enough that you'd want to know which stage spent the time
- Has stages that can fail independently (auth can fail without breaking the fetch)
- Is worth grep-ing by stage in log output

Don't impose phases on short single-purpose scripts. A webhook handler that receives an event and posts a notification has no phases to structure, and forcing them adds noise.

## Phase headers in code

Mark phase boundaries with visually distinct comment blocks:

```typescript
// -------------------------------------------------------
// PHASE 1: Authenticate with external system
// -------------------------------------------------------
log(`[AUTH] Connecting to external API...`);
const tokenResult = await withRetry(/* ... */);
// ...

// -------------------------------------------------------
// PHASE 2: Fetch source records
// -------------------------------------------------------
log(`[FETCH] Loading records from external system...`);
// ...

// -------------------------------------------------------
// PHASE 3: Format and filter
// -------------------------------------------------------
log(`[FORMAT] Formatting ${records.length} records...`);
// ...
```

The comment blocks do three things:

- Visually partition `main()` into navigable sections
- Match phase numbers and names to log tags, so a reader can map a log line back to the relevant code block
- Serve as natural breakpoints for dry-run decisions (dry-run typically stops between the last read phase and the first write phase)

## Phase tags in logs

Every log line in a phase carries a stable `[TAG]` prefix that names the phase:

```
[0.2s] [INIT] Variables loaded
[0.5s] [AUTH] Connecting to external API...
[1.1s] [AUTH] Token obtained
[1.2s] [FETCH] Loading records from external system...
[2.8s] [FETCH] Received 150 records
[3.0s] [FORMAT] Formatting 150 records...
[10.4s] [FORMAT] 142 valid, 2 blocked, 6 filtered
[10.5s] [DEDUP] Loading existing records from CallVA...
[11.9s] [DEDUP] 120 existing records loaded
[12.0s] [CREATE] 22 to create, 120 duplicates, 2 blocked, 6 filtered
[38.2s] [CREATE] Done: 22 created, 0 failed
```

Properties of a good tag scheme:

- **One tag per phase** (`[AUTH]`, `[FETCH]`, `[FORMAT]`, `[DEDUP]`, `[CREATE]`, `[POST]`) — not per log-line type. Don't use `[INFO]`, `[WARN]`, `[DEBUG]` — severity is orthogonal to phase and adds no grep value.
- **Uppercase, short** — 4-8 characters keeps log lines readable.
- **Match code comments exactly** — `[FETCH]` in logs, `PHASE 2: Fetch source records` in the code comment above. A reader grep-ing `[FETCH]` should land next to the right code block.
- **Reuse across re-deploys** — don't rename phase tags lightly; operators rely on them in run history.

Use `[INIT]` for the pre-phase setup (variable loading, parameter resolution) and `[DONE]` or a summary line for the end. Use `[ERROR]`, `[WARN]`, `[RETRY]`, `[FATAL]` for cross-cutting concerns — these are the only non-phase tags worth having.

## Seconds-from-start prefix

For multi-phase pipelines, prefix every log line with the elapsed seconds since the run started. This makes it trivial to see where time is being spent:

```typescript
let _t0 = 0;
function log(msg: string) {
  const s = ((Date.now() - _t0) / 1000).toFixed(1);
  console.log(`[${s}s] ${msg}`);
}

export async function main() {
  _t0 = Date.now();
  log("=== Script started ===");
  // ...
}
```

A 40-second pipeline with `[0.2s]`, `[10.4s]`, `[38.2s]` timestamps tells you instantly which phase spent most of the time. A pipeline with per-step `(123ms)` suffixes forces you to mentally integrate across the whole log. For multi-phase pipelines, the seconds-from-start prefix wins decisively.

For short single-purpose scripts, per-step `elapsed()` is fine — the total is visible from the `callva automations run-detail` output anyway.

## Reporting phase counts in the return value

Structured return values should group counts by subsystem, roughly mirroring the phases:

```typescript
return {
  status: "success",
  source: { total: 100, excluded: 4, empty: 20 },
  records: { raw: 150, filtered: 6, blocked: 2, created: 22, duplicates: 120, create_failed: 0 },
  post_processing: { grouped: 5, failed: 0 },
  blocked_items: [...],
  errors: [],
};
```

The top-level keys (`source`, `records`, `post_processing`) mirror the phase names. An operator reading the result from `callva automations run-detail` sees which phase produced which numbers at a glance.

## When this doesn't apply

- **Single-purpose scripts** (webhook handlers, single-record mutations) have no phases.
- **Ad-hoc maintenance scripts** don't need the ceremony if they're never re-run.
- **Scripts with fewer than 3 logical stages** — phase headers for a 2-step script are over-ceremony and make the code feel heavier than it is.

## Related

- `callva guide resilience-and-retries` — the `phase` field on `RunError` should match the phase tag in the logs, so a failed item can be traced to its code block.
- `callva guide batch-processing` — the reconciliation line is typically logged at the end of the batch-processing phase.
- `callva guide dry-run-pattern` — the dry-run boundary typically sits between the last read phase and the first write phase.
