# Writing a monitor

Use this guide when an automation's job is to watch something and speak only when it matters - an upstream, a market, a page that will announce a date, a system nobody is looking at. A monitor is the most common automation and the easiest to get quietly wrong, because a broken one and a calm one produce the same output: nothing.

The general rules in `automations guide authoring` still hold - profiles, the agent call, and the mode fence. This guide is what a monitor adds.

## Two shapes, and how to choose

**Agent-first.** The script decides whether to run, and the turn does everything else: goes and looks, judges what it found against a brief it maintains, sends the message itself, and leaves the brief better than it found it. The script never sees the findings. This is the right default for a monitor somebody asked for in conversation, for anything watching a surface that is written for humans, and for anything running once or twice a day.

**Script-first.** The script gathers, compares against a stored snapshot, and spawns a turn only when something moved, using the turn for judgement and doing the delivery itself. This is right when the surface is a stable machine interface, when runs are frequent enough that a turn per run is real money, or when a later step needs a specific value rather than a message.

The pull toward script-first is a developer's instinct and it is usually wrong for a monitor. A field comparison is only as good as the fields somebody thought of, and the finding that matters is regularly the one nobody anticipated - a sale opening through a supporter programme, an announcement that moved to a mailing list, a page that now lives somewhere else. An agent reading the same surface finds those and then writes them into its own brief; a matcher reports that nothing changed.

Cost rarely decides it. One turn a day is nothing next to what a missed signal costs, and a monitor is worth building precisely because somebody cares about the thing. Judgement per run stops being reasonable around the frequency where a person would also stop re-reading; below a few times a day, take the agent.

Do not split the difference by having the script pre-digest the surface into fields and then ask the turn to bless them. That spends the turn and keeps the blindness.

## Where a gate still belongs in code

Whatever shape you pick, cadence is the script's. A monitor should declare its schedule at the finest interval it will ever need and narrow it in code - weekly now, daily in the month that matters, hourly once a date is known. A run that is not due exits in milliseconds, and the cadence stays readable next to the logic that governs it rather than encoded in a cron expression nobody re-reads.

A precise, cheap precondition is worth a gate too: a file that has not changed, a queue that is empty, a season that has not started. Gate on it and skip the turn entirely. This is not the same as detection - it is not asking whether the answer changed, only whether asking is worth anything at all.

## Failure has to be louder than silence

This is the one thing that stays in code no matter how much moves under the agent. A worker that died cannot report that it died, and from the outside a dead monitor is indistinguishable from a quiet one.

Count consecutive failures and alert on a threshold matched to the cadence rather than a number that sounds right: a weekly monitor alerting on the second consecutive failure is silent for a fortnight, while at daily cadence the same rule is a day. Say when it recovers, so a fixed source is known to be fixed.

Never let a monitor end silently. If it has a window, closing that window is itself an event worth a message - a hard end date sitting in a constant is how a watch dies unnoticed, and the moment it was built for is usually just past it.

## The brief is the monitor

An agent-first monitor keeps its memory in a Markdown file beside the script, and that file is most of the design: what is being watched and why, what counts as a finding and what is noise, the sources, what nobody has explored yet, the history, and a ledger of what has already been reported.

The turn reads it first and edits it last. That is what stops a monitor decaying: sources it checked get annotated, surfaces it discovered get added, the rules sharpen when the watched thing states its own, and closed items leave. A monitor that cannot edit its own instructions is answering the question somebody asked on day one for as long as it runs.

The ledger is also how an agent-first monitor deduplicates. It is judgement rather than a hash, and it holds because the turn is told plainly that something already in the ledger is not a finding unless it changed - and then the change is the finding. Keep the ledger honest: a line saying something was reported must mean the person actually received it, or the next run will stay quiet about the thing it never sent.

Where the script genuinely needs one value out of all this - a date that decides the cadence, a version that decides an upgrade - do not reach for a schema. Agree on one line in the brief, in a fixed shape, and parse that. The agent maintains it as part of the upkeep it is already doing, and anything unparseable is read as absent rather than guessed at.

## Telling the agent how to speak

A turn that delivers its own message needs the standards a person would be given: which chat, which language, how long, what to lead with, and that a link belongs in it so the reader can check the claim.

Say what silence means. A quiet run reported as quiet is a correct result, and padding trains the reader to stop reading - which breaks the monitor as surely as a crash. One message per run, at most.

Be explicit about evidence. Require the source's own words behind any claim, and forbid reporting an inferred date, price or deadline as a stated one. This is where a monitor is most tempting to a model: the whole job is to come back with the thing, and a plausible version is always available. The reader will plan around whatever arrives, so an invention here is worse than no monitor at all.

## Schemas, and when they earn their place

`--schema` is for a script that branches on what came back. A monitor whose output is a message to a person is not that, and the schema then buys structure nobody consumes while adding a way to fail.

When you do use one, note that a strict engine requires every property to be listed in `required`; express an optional field as a nullable type rather than by leaving it out, or the turn fails before it starts.

## Proving it

Run the thing once by hand before trusting it to a schedule, and read what it actually did rather than its exit code. The first real run is also the most informative one the monitor will ever have: it is the run that discovers which sources are unreachable, which of them lied about being relevant, and what the brief should have said.

Watch for the failure that looks like success - a turn that came back healthy having reached nothing, because its sandbox refused the network quietly. If a monitor's job is to look outward, its profile has to be one that can, and the way you learn that it is not is by reading the first run rather than the schedule's.
