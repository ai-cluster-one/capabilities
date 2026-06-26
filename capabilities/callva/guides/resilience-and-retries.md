# CallVA — resilience and retries (retry, Result, status taxonomy)

A Windmill automation script for CallVA calls external systems, and external systems fail: network blips, rate limits, 502s, connection resets, flaky DNS. A script that crashes on the first flake is fragile and hard to operate. This guide is one facet of authoring automations with the `callva` tool — how to retry and handle errors so a pipeline stays robust without swallowing real bugs. The principle: every external call is fallible, every failure is either fatal to the run or recoverable for one item, and the return value makes which one happened legible to whoever reads the run later.

## The `withRetry` helper

Wrap every external call in a retry helper that retries on failure with linear backoff and returns a tagged `Result`:

```typescript
type Result<T> = { ok: true; data: T } | { ok: false; error: string };

async function withRetry<T>(
  fn: () => Promise<T>,
  retries: number,
  label: string,
): Promise<Result<T>> {
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const data = await fn();
      return { ok: true, data };
    } catch (e: any) {
      const msg = e?.message ?? String(e);
      if (attempt < retries) {
        const delay = 1000 * (attempt + 1); // 1s, 2s, 3s...
        log(`[RETRY] ${label} — attempt ${attempt + 1} failed: ${msg}, retrying in ${delay}ms`);
        await new Promise((r) => setTimeout(r, delay));
      } else {
        return { ok: false, error: msg };
      }
    }
  }
  return { ok: false, error: "unreachable" };
}
```

Usage:

```typescript
const tokenResult = await withRetry(
  () => getAuthToken(baseUrl, username, apiKey),
  2,
  "External API login",
);
if (!tokenResult.ok) {
  log(`[FATAL] ${tokenResult.error}`);
  return { status: "failed", phase: "auth", error: tokenResult.error };
}
const token = tokenResult.data;
```

## Why `Result<T>` instead of throwing?

A discriminated union makes the failure branch **visible to the caller and mandatory to handle**. You cannot accidentally forget to catch. The type checker will complain if you try to read `.data` without first checking `.ok`.

Exceptions remain appropriate for unrecoverable programmer errors (invariant violations, schema mismatches, unreachable code). But for expected runtime failures (API flakes, auth expiry, rate limits), `Result<T>` is clearer and safer.

The pattern also composes well with accumulated errors (below) — you can push a failed `Result` into an errors array and continue processing, instead of short-circuiting the whole run.

## Recoverable vs fatal errors

Not every failure should kill the run. Distinguish the two cases explicitly:

### Fatal — return early with `status: "failed"`

Anything that makes the remainder of the pipeline impossible:

- Auth failure (can't get an API token → can't do anything)
- Missing required variable (no `CALLVA_API_KEY` → every subsequent call fails)
- Config error (unknown workspace, missing project)

```typescript
if (!tokenResult.ok) {
  return {
    status: "failed",
    phase: "auth",
    error: tokenResult.error,
    errors: [{ phase: "auth", context: "external CRM login", error: tokenResult.error }],
  };
}
```

### Recoverable — log, accumulate, continue

Anything that affects one item in a batch but doesn't prevent other items from being processed:

- Failure to fetch one account's appointments (other accounts still work)
- Failure to create one call record (other records still succeed)
- Failure to combine one multi-visit group (other groups still combine)

```typescript
const errors: RunError[] = [];

// ... inside a batch loop ...
const res = await withRetry(() => createCall(appt), 1, `Create ${appt.id}`);
if (!res.ok) {
  errors.push({
    phase: "create",
    context: `${appt.full_name} (${appt.phone})`,
    error: res.error,
  });
  log(`[ERROR] Failed to create ${appt.full_name}: ${res.error}`);
  continue;
}
```

At the end of the run, the errors array is included in the return value:

```typescript
return {
  status: errors.length > 0 ? "completed_with_errors" : "success",
  // ... counts ...
  errors, // full list for drill-down in the Windmill UI
};
```

## The `RunError` shape

Every accumulated error should be structured, not a bare string:

```typescript
interface RunError {
  phase: string;    // "auth", "fetch", "create", "combine" — matches log tags
  context: string;  // which item, which record, which identifier
  error: string;    // the raw error message
}
```

When a run lands in the Windmill UI with 5 errors, you want to see them as an inspectable list with enough context to reproduce, not a concatenated mush.

## Status taxonomy

Use three run-level status values in return objects:

- `"success"` — everything worked, `errors.length === 0`
- `"completed_with_errors"` — some items failed but the run reached the end
- `"failed"` — fatal error, the run could not complete

This lets alerting and monitoring distinguish "one flaky item" from "entire pipeline down" without parsing the errors array.

## Retry budget

- **External API calls**: 2 retries (3 attempts total) with 1s/2s linear backoff. Enough to paper over transient network issues without wasting the 60s Windmill execution budget.
- **Create operations**: 1 retry. Creates are cheap to retry and often succeed on the second try after a brief rate limit.
- **Auth operations**: 2 retries. If auth still fails after 3 attempts, something is actually broken — faster failure is better than prolonged retry.
- **Sub-job dispatch**: 0 retries. Windmill's own queue handles durability.

Do not retry indefinitely. The 60-second execution limit will bite you. If an operation has intrinsically slow recovery (e.g. a cold-start service that takes 30s to come back), design around it — increase the first retry's initial wait, or accept partial failure and let a later run catch up.

## Timeouts on external calls

Use `AbortSignal.timeout()` to bound individual fetches. A `fetch()` that hangs will consume the entire 60-second budget silently:

```typescript
const res = await fetch(url, {
  signal: AbortSignal.timeout(10_000), // 10 seconds
});
```

Rule of thumb: 10s for external APIs you trust, 15s for ones known to be slow, 5s for internal health checks.

## When this doesn't apply

Pure transforms with no external API calls don't need `withRetry`. A script that reads variables, does math, and returns a value has nothing to retry. Don't add ceremony for problems the script doesn't have.

## Related

- `callva guide batch-processing` — the errors array surfaces as one of the outcome categories in the reconciliation line.
- `callva guide multi-phase-pipelines` — the `phase` field on `RunError` should match phase tags in the logs.
