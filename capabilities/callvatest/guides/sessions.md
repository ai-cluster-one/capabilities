# Driving a session

A scenario is written before the call; a session is decided during it. `session
start` places one call and leaves it open, `session say` sends a line and
returns what the agent said back, and `session end` closes the call and hands
back the same report a scenario run produces. Between those, the next line is
chosen after reading the reply to the last one.

Run `callvatest help` for the command surface. This guide is about which of the
two to reach for, and how not to waste a call.

## Sessions find, scenarios prove

A scenario's input is byte-identical on every run, so a red-then-green pair is
evidence that something changed. That is what a regression gate needs, and a
session cannot give it: the moment you choose the next line by hand, two runs
stop being comparable.

What a session gives instead is the ability to ask a second question. An agent
that answers oddly can be pushed — rephrase the request, contradict it, ask the
same thing a different way — inside the same conversation, with the state that
produced the oddity still standing. A scenario cannot do that: its next line was
written before the odd answer existed.

So the two are one pipeline, not two options. Explore in a session until you
understand what the agent does wrong; write the smallest scenario that fails on
it; keep that scenario as the gate. A finding that never becomes a scenario will
be rediscovered by hand every time.

## Ask a discriminating question

A session is worth its cost when the second line tells you something the first
could not. Before sending it, name what each possible answer would rule out.

The strongest shape is the same request in two framings. If an agent handles
"how does X work" well and "please do X" badly, the failure is in how the
request is classified, not in what the agent knows — and that is a different
repair from teaching it the fact. One call answered that; a scenario for each
framing would have cost two and still not shown they were the same case.

## Read the whole call, not only the replies

`session say` returns the reply. It does not return the tool calls, because
those are only knowable after the call has ended. Two consequences.

The interesting evidence arrives at `session end`: which tools ran, in what
order, with which arguments, and the fields the platform stored. An agent that
answers correctly from nothing is a different finding from one that answers
correctly after consulting — and only the end report tells them apart.

The log tells you the rest as it happens. Every observed event is appended to
`log.jsonl` while the call runs, so a session that dies mid-conversation has
already written everything it saw. `session end` on a dead holder recovers from
that log and still fetches the platform's side, marking the report `recovered`.
A dropped call costs the rest of the conversation, never the part already had.

## The call is live while you think

The room stays open between invocations, which is the point, and it is also the
cost: an open session is a real call, billing, and the agent is waiting. A long
pause is not free — the agent has its own limits on silence and total duration,
and reaching one ends the call underneath you.

So keep a session to one line of enquiry and end it when that line is answered.
`session list` shows what is still open, including sessions whose holder has
died. Ending a session you are finished with is not tidiness; it is the only
thing that stops the call.
