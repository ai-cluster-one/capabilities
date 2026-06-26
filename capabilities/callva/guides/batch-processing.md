# CallVA — batch processing (parallelism, outcomes, reconciliation)

A Windmill automation that fetches or processes collections of records lives or dies by how it structures the batch. This guide is one facet of authoring automations with the `callva` tool: it covers three tightly-related disciplines that belong together — **batch parallelism**, **outcome categorization**, and **reconciliation logging** — as a single coherent pattern. Get them right and an operator can read a run's outcome at a glance; get them wrong and a failed run at 3 AM tells you nothing.

## Batch parallelism with `Promise.all`

The two default shapes most people reach for are both wrong:

- **Serial `for ... await`** — one request at a time. Slow. Wastes 95% of available time on RTT.
- **Unbounded `Promise.all(everything)`** — hundreds of concurrent requests. Rate-limits you out of the API and often hits Windmill's 60s execution budget fighting backpressure.

The sweet spot is **bounded concurrent batches**: process N items in parallel, wait for the batch, then move to the next:

```typescript
const BATCH_SIZE = 10;

for (let i = 0; i < items.length; i += BATCH_SIZE) {
  const batch = items.slice(i, i + BATCH_SIZE);
  const results = await Promise.all(
    batch.map(async (item) => {
      const res = await withRetry(() => processItem(item), 2, `Item ${item.id}`);
      return { item, res };
    }),
  );

  for (const { item, res } of results) {
    // handle success/failure per item synchronously
  }
}
```

Typical batch sizes:

- **10** for external HTTP APIs — enough parallelism to be fast, few enough to avoid rate limits
- **50** for CallVA bulk operations — CallVA tolerates higher concurrency
- **1** (serial) for operations with strict ordering requirements (rare — usually a sign the design needs rethinking)

Pick a size once at the top of the script as a named constant (`APPT_FETCH_BATCH`, `CALL_CREATE_BATCH`). Don't scatter magic numbers.

## Outcome categorization: filtered / blocked / duplicate / failed

When processing a batch, every item falls into one of a small number of categories:

| Category | Meaning | What happens to the item |
|---|---|---|
| **Created** (happy path) | Item passed all checks and wrote successfully | Record exists in CallVA with happy-path status |
| **Filtered** | Structurally skipped by a business rule — not an error | Never reached CallVA; no record created |
| **Blocked** | Business rule violation that needs human attention | Record IS created in CallVA with `status: "blocked"` and `status_comment` explaining why |
| **Duplicate** | Already exists in CallVA from a prior run | Not created, see `callva guide idempotency` |
| **Failed** | Runtime error during create | Accumulated in `errors[]`, see `callva guide resilience-and-retries` |

The distinction between **filtered** and **blocked** is important and non-obvious:

- **Filtered** items don't need human follow-up. They're intentionally skipped by a rule everyone knows about (e.g. "skip records with no patient record attached"). Don't clutter CallVA with them.
- **Blocked** items DO need human follow-up. They represent data quality issues in the source system that should eventually be fixed (e.g. "phone number doesn't start with 5, which means it isn't a mobile number, which means the clinic has a bad record"). You want them visible in CallVA so a human can audit and fix them upstream, which is why they get created as records with `status: "blocked"` and a `status_comment`.

If you only have one "skip" bucket, you lose this distinction and every skip becomes invisible.

## The reconciliation log line

At the end of the batch phase, print a single line that reconciles the input count to the sum of all outcome categories:

```
Appointments: 339 raw = 327 created + 0 duplicates + 2 blocked + 10 filtered + 0 failed
```

The left side is the count of items pulled from the source. The right side is the sum of all outcome categories. **They must match.** If they don't, an item fell through a crack in your logic — typically a branch that forgot to `continue` or forgot to increment a counter.

Make this a habit. It catches a class of bug that unit tests never notice and that is invisible in aggregate metrics.

## Structured counters in the return value

Mirror the categories as structured counters in the return value, not a single bare "count":

```typescript
return {
  status: "success",
  appointments: {
    raw: 339,
    filtered: 10,
    blocked: 2,
    duplicates: 0,
    created: 327,
    create_failed: 0,
  },
  // ... other subsystem counts ...
};
```

The Windmill UI renders this as collapsible JSON. An operator looking at run history sees the shape at a glance and can drill into whichever number surprises them.

Avoid:

- **Bare totals**: `{ count: 327 }` — useless for debugging, can't distinguish "nothing to do" from "all failed"
- **String-formatted summaries**: `{ summary: "327 created, 2 blocked..." }` — can't query, can't chart, can't alert on
- **Ambiguous field names**: `{ total: 339, success: 327 }` — is "success" the created count? the non-failed count? unclear

## Drill-down lists for non-happy-path items

For every non-happy-path category, include the actual list of affected items in the return value as a sibling of the counts:

```typescript
return {
  appointments: { /* counts */ },
  blocked_phones: [
    { patient: "Patient A", phone: "555-0100", doctor: "Dr. Smith", apptId: "..." },
    { patient: "Patient B", phone: "555-0199", doctor: "Dr. Jones", apptId: "..." },
  ],
  errors: [
    { phase: "create", context: "...", error: "..." },
  ],
};
```

When someone opens a failed run at 3 AM, the drill-down lists are the difference between "I can fix this in 2 minutes" and "I need to re-deploy with debug logging and wait for tomorrow's cron".

Keep drill-down lists bounded — for very high-volume runs, cap at the first 50 items of each category and add a `blocked_phones_truncated: true` marker if you hit the cap.

## When this doesn't apply

- **Single-item scripts** (webhook handlers processing one event, runners dispatching one call) have no batches to structure. They still benefit from the outcome categorization idea, but as a single-item result shape, not a batch shape.
- **Pure transforms** over small datasets that fit in memory have nothing to batch — a single `.map()` is fine.

## Related

- `callva guide idempotency` — the `duplicates` category is implemented by the dedup pattern.
- `callva guide resilience-and-retries` — the `failed` category is fed by accumulated errors from retried operations.
- `callva guide multi-phase-pipelines` — the reconciliation line is typically logged at the end of the batch-processing phase.
