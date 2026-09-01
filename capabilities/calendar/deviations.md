# calendar — recorded deviations

Deliberate departures from the executable standard (SHEBANG.md), kept in this
dedicated file so an audit reads them as choices, not drift.

## A permission rides the connection identity

The standard keeps what a project may do with a connection apart from what the
connection is: a grant carries `enabled` and `allow_write`, and a permission
change is a grant edit rather than a restated identity.

A connection here also carries `calendars`, and that list is a permission — it
governs which collections reads and writes may reach, and a verb aimed outside
it is refused with exit 4 `out_of_scope`. The grant schema is closed, so the
list sits on the connection record and widening it is an identity edit, which
is what the refusal's own hint names.

The reach is what makes it worth the departure. Writing into a principal's
calendar is outward-facing twice over: the event is visible to everyone who
shares the calendar, and a scheduling server mails an invitation to every
attendee. `allow_write` alone answers *may this connection write*; it cannot
answer *into which of a principal's calendars*, and a booking agent given one
calendar should not reach the rest of them.

`_enforce_write` therefore layers the scope on the standard `_write_gate`, and
read verbs consult the same list: an explicit `--calendar` outside it exits 4
with the same code, and without one, reads intersect with the scope silently.
Neither half is liftable by a flag or an environment variable.

The `connections` report carries one key beyond the standard shape —
`calendars`, beside `allow_write` — so the resolution report publishes the
whole of a connection's policy rather than half of it.

## The iCalendar layer is written here; recurrence rules are not

The standard asks for minimal dependencies and for a protocol-specific client
only where the protocol genuinely demands one. This capability draws that line
inside iCalendar (RFC 5545) rather than around it.

What is written here: unfold, parse properties and their parameters, read and
write a VEVENT, fold again. That is bounded, it is covered by this
capability's tests, and a general iCalendar library would bring a model far
larger than the verb surface uses.

What is not: recurrence rules are evaluated by a dependency. A rule is a small
language — intervals, weekday selections, counts, until-dates, excluded and
added dates, and per-occurrence overrides on top — and every way of getting it
slightly wrong is silent. An occurrence this capability fails to produce does
not raise anything; it reads as free time, and free time is what a booking
agent writes into. That is the one place where being approximately right is
worse than taking a dependency, so the rules go to code that is exercised by
far more calendars than this one.

The expansion runs on every reply rather than only when a server declines to
do it. CalDAV's expand extension is requested, and a server that honours it
sends less, but a server may also accept the request and answer it unexpanded.
Expanding regardless makes the answer independent of which server replied.

What the capability does not do at all is stated where a caller meets it —
`calendar help`, under RECURRENCE and TIME: no rule is authored, and `update`
refuses an object carrying per-occurrence overrides rather than flattening it.
