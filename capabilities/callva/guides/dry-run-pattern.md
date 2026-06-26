# CallVA — dry-run pattern (preview before write)

Authoring a CallVA automation is writing a Windmill TypeScript script that the
`callva` tool deploys and runs against the live platform. This guide covers one
facet of that: making a mutating script previewable before it touches anything.
Any automation that writes a non-trivial number of records must support a
`dry_run` mode. The convention below is what makes dry-runs safe, useful, and
non-negotiable for destructive pipelines.

## The requirement

Every mutating automation's `main()` function should accept `dry_run: boolean = false` as a parameter. When `dry_run === true`, the script must:

1. Execute every phase up to the first destructive write
2. Return a structured result that looks like a real run's result
3. Include a `sample` field with a handful of items that *would* be written
4. Not call any `POST`, `PUT`, `PATCH`, or `DELETE` against CallVA or external systems

The operator should be able to trigger the automation against live source data, get a faithful preview of what it would do, and be 100% certain no side effects occurred.

## Pattern

```typescript
export async function main(
  target_date: string = "",
  dry_run: boolean = false,
) {
  // Phases 1-3: fetch data, filter, format.
  // All read-only operations — safe to execute in both modes.
  const formattedItems = await fetchAndFormat(/* ... */);

  if (dry_run) {
    log(`[DRY RUN] Stopping before writes. Would create ${formattedItems.length} records.`);
    return {
      status: "dry_run",
      target_date,
      appointments: {
        raw: rawCount,
        filtered: filteredCount,
        blocked: blockedCount,
        pending: formattedItems.length,
      },
      sample: formattedItems
        .filter((item) => item.status === "scheduled")
        .slice(0, 10),
      blocked_phones: blockedList,
      errors: [],
    };
  }

  // Phases 4-6: writes only run past this point
  // ...
}
```

The `if (dry_run)` branch lives at the **exact boundary** where destructive writes begin. Everything before it is read-only and runs in both modes. Everything after it is skipped in dry-run mode. This is the contract — no writes may sneak in before the branch, and no reads that need to happen for the preview may be skipped by it.

## What to include in the `sample`

A handful (5-10) of fully-formatted records as they would be sent to CallVA. Not summaries, not redacted — the actual JSON payload:

```typescript
sample: formattedItems
  .filter((item) => item.status === "scheduled") // focus on happy-path items
  .slice(0, 10),
```

The operator reading the run detail scrolls through the sample to verify:

- Timezone conversion looks right (dates in UTC match expected local times when back-converted)
- Field mapping looks right (names, phone numbers, IDs are all populated)
- Business rules fired correctly (blocked items got their `status_comment`)
- Nothing surprising (no empty fields, no malformed strings, no null bombs)

The sample is the single most valuable debugging artifact for a mutating pipeline. Invest in it.

## What `status` value to return

Use `"dry_run"` as a distinct status, not `"success"`. This makes the run clearly visible in run history as a preview, prevents it from being mistaken for a real run, and lets monitoring ignore dry-runs entirely:

```typescript
return {
  status: "dry_run", // not "success"
  // ...
};
```

## Default to `false`, never `true`

The default value on the parameter must be `false`. A scheduled cron trigger should always produce real effects. If you default `dry_run` to `true`, you will one day wonder why the pipeline hasn't actually written anything for three weeks.

The operator **opts in** to dry-run explicitly, via the `--args` flag on the CLI:

```bash
callva automations run <id> --args '{"dry_run":true}'
```

## Testing convention

When deploying a new version of a mutating automation:

1. Deploy the code
2. Trigger a dry-run first: `callva automations run <id> --args '{"dry_run":true, ... other params ...}'`
3. Fetch run detail, eyeball the `sample` field
4. If the sample looks correct, trigger a real run (omit `dry_run` from the args, or pass `false`)
5. If the sample reveals a bug, fix, redeploy, and repeat from step 2

Treat this as non-negotiable for any automation that creates more than ~10 records. The 10-second cost of the dry-run prevents hours of cleanup after a bad deploy.

## When this doesn't apply

- **Pure read scripts** (fetch data, return it, never write) don't need `dry_run` — there's nothing to preview because there's nothing to write.
- **Webhook handlers** processing a single event usually don't need it — the side effects are small and the real run IS the test. That said, you can still add it as a safety net if the webhook triggers expensive downstream side effects.
- **One-shot maintenance / backfill scripts** running once with human oversight don't strictly need it, but adding `dry_run` is cheap insurance and often easier than writing a separate "preview" script.

## Related

- `callva guide batch-processing` — the outcome counts you return in dry-run mode should match the shape you'd return in a real run. The only difference is `created` vs `pending` and the presence of `sample`.
- `callva guide idempotency` — dry-run is often used together with dedup verification: run dry first to see the `duplicates` count, then run live.
