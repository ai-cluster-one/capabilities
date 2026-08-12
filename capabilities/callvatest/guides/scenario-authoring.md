# Authoring a scenario

A scenario is evidence about production behaviour: the agent under test loads
its production configuration and calls its production tools, and the run places
a real, billable call. That cost sets the standard — a scenario earns its place
by failing when the agent is wrong and staying quiet otherwise.

Run `callvatest help` for the scenario keys and the command surface. This guide
is the method for choosing what to assert.

## Probe before you assert

`probe` places one call with no assertions and prints everything observed: the
transcript with timings, the tool runs with their arguments, and the results
the agent answered from. Read that output first and write the scenario against
what the agent actually does, rather than against what the prompt says it
should do. The same command is the first thing to reach for when a scenario
starts failing for reasons its report does not explain.

## Assert the decision, not the answer

The agent decides which tool to call and with which arguments; the tool decides
what comes back. A scenario owns the first and never the second.

So a tool call, its arguments, its count, and the order of calls are fair
assertions — they are the agent's own choices. A concrete time, date, price, or
free slot is not: it changes with the data behind the tool, and pinning it as a
literal buys a failure every time the calendar moves. Assert instead that the
agent answered *from* the result it received, which is what the semantic claim
is for.

## One scenario, one behaviour

Name the behaviour in the scenario id and let the turns do only enough to reach
it. A scenario that walks a caller through an entire booking to check one
routing decision fails in six places for one bug, and each failure costs a call
to reproduce.

Turns are cheap inside one scenario; scenarios are expensive. Prefer more turns
in one call over more calls.

## Negative expectations carry half the value

`tools_not_called` and `reply_not_matches` catch the failures that a positive
assertion never sees: the agent that reaches for a lookup before it has the
argument, or the one that opens with a refusal. An agent that regresses
usually does something extra, not something less.

## The call-level block is about the call, not the turn

Checks that only make sense once the call has settled belong in the call-level
`expect`: that audio was actually produced, that the agent spoke again after a
tool returned, that no internal id leaked into speech, how many times a tool ran
across the whole call, and what the platform stored on the call record.

`post_call` compares each named field as a string against the stored record, so
it asserts the tenant's own schema — its outcome fields and any custom field
defined on a call — without this harness knowing that schema in advance.

## Let a failure be readable

The report carries the transcript, every tool run with its arguments, and the
shape of each result; the results themselves stay out because they carry
whatever personal data the caller gave. Pass `--with-results` while debugging a
specific mismatch, and `--report FILE` when the run needs to be read later.

A check named after the behaviour it protects reads better in a failure line
than one named after the mechanism, so prefer a scenario id like
`declines-before-identifying-caller` over `test-3`.

## Group the suite by what varies

Scenarios sit under a project's own tests directory, split by agent and by
language. An agent that speaks several languages is several agents' worth of
behaviour: the same decision can be right in one language and absent in
another, so a language folder is a real axis, not bookkeeping.
