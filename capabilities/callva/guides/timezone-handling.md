# CallVA — timezone handling (UTC boundary discipline)

Authoring a CallVA automation means writing a Windmill TypeScript script that moves data between an external system and the CallVA API. This guide covers one facet of that craft: getting dates across the boundary intact. When an automation moves dates between external systems and CallVA, timezone handling is the single most common source of subtle, silent bugs. What follows are the patterns that hold.

## The core fact: CallVA normalizes to UTC

CallVA stores every date as **Zulu (UTC)** time. On write, it parses the incoming ISO string:

- If the string carries an explicit offset (`...+03:00` or `...Z`), CallVA converts to UTC correctly.
- If the string has no offset, **CallVA assumes it is already UTC** and stores the literal value — which silently shifts every date by the source timezone's offset (up to ±14 hours).

**Rule**: every date you write to CallVA must carry an explicit timezone offset. If the external source doesn't provide one, you must stamp it yourself before sending.

## Producer-boundary discipline

The script that *writes* a date to CallVA is the only place that should perform local → UTC conversion. Downstream consumers read the UTC string from CallVA and render back to local time for display if needed.

```
External source (local wall-clock)
        │
        │  ← PRODUCER performs local → UTC conversion here,
        │    using fromZonedTime() with the source timezone
        ▼
   CallVA (UTC)
        │
        │  ← CONSUMERS read UTC and format back to local
        │    using toZonedTime() / Intl.DateTimeFormat
        ▼
   SMS / voice / display
```

Never convert twice in the same pipeline. If you find yourself doing Local → UTC → Local inside a single script, you are doing ceremony for no reason and adding bug surface.

## Use `date-fns-tz`, not manual offset math

Windmill Deno supports `npm:` specifiers. The library handles the full IANA timezone database, DST transitions, historical offset changes, and ambiguous times (spring-forward gaps, fall-back overlaps) correctly.

```typescript
import { fromZonedTime, toZonedTime, format as formatTz } from "npm:date-fns-tz@3";

// Single source of truth for the whole script
const TIMEZONE = "America/Los_Angeles";

// Local wall-clock → UTC ISO (for writing to CallVA)
function localToUtcIso(dateStr: string, timeStr: string): string {
  const wallClock = `${dateStr}T${timeStr}:00`;
  return fromZonedTime(wallClock, TIMEZONE).toISOString();
}

// UTC ISO → local display string (for rendering to humans)
function renderLocal(isoStr: string): string {
  const zoned = toZonedTime(isoStr, TIMEZONE);
  return formatTz(zoned, "EEEE, MMMM dd, HH:mm", { timeZone: TIMEZONE });
}
```

**Do not write manual offset math.** Even the "compute the offset at noon to dodge the DST transition window" trick falls over on genuinely ambiguous cases the library handles for free. A manual approach is always more code and less correct.

## Single source of truth: module-level `TIMEZONE` constant

Never repeat the string literal `"America/Los_Angeles"` (or whatever your target is) throughout the script. Define it once at module scope:

```typescript
const TIMEZONE = "America/Los_Angeles";
```

and reference it everywhere else. If the organization ever expands to a second timezone, the change is one line, not a global find-and-replace.

## Hoist conversion out of hot loops

If the same derived value applies to every item processed in a run (for example, every appointment in a batch is for the same target date and needs the same `call_at` anchor), compute the conversion **once in `main()`** and pass the result through pure functions. Do not recompute per item.

```typescript
// Computed once at the top of main()
const callDate = addDays(targetDate, -1);
const callAt = localToUtcIso(callDate, "11:00"); // same value for every item this run

// Pass through a context object into the pure formatter
formatAppointment(appt, { callAt });
```

The reason: per-item `Intl.DateTimeFormat` / `fromZonedTime` allocation is wasted work for values that are invariant across the run, and hoisting makes the invariant visible in the code.

## Common anti-patterns to avoid

- **Treating local time as if it were UTC.** `new Date(dateStr + "T" + hour + ":" + minute + ":00").toISOString()` interprets the input as runtime-local (UTC on the Windmill worker), then outputs that as UTC. Every local date gets shifted by the source timezone's offset silently. This is an easy-to-miss class of bug that usually only surfaces when someone notices a call fired at the wrong hour — then requires a one-off migration script to fix historical records. Catch it at the producer.
- **Storing offset-less strings and hoping downstream guesses right.** Nothing good comes of this.
- **Converting twice in the same script.** Once the value is in CallVA-UTC form, leave it alone.
- **Using `.toLocaleString()` with no `timeZone` option.** That uses the Deno runtime's locale, which is UTC. Produces silently wrong strings.

## When this doesn't apply

A script that only moves data *within* CallVA (read records, transform, write back to the same records) never crosses a timezone boundary — CallVA is UTC on both sides. In that case there is nothing to convert. The producer-boundary discipline only applies when an external system is involved as the data source or sink.
