# Attaching a meeting room to a booked event

Use this guide to decide where a meeting room comes from before you promise anyone a video link. CalDAV can carry a meeting URL onto an event and cannot create one, and that difference decides whether a booking flow works or quietly produces meetings nobody can join.

## Why the protocol cannot mint a room

An event booked over CalDAV is a VEVENT: the iCalendar object defined by [RFC 5545](https://www.rfc-editor.org/rfc/rfc5545). That object has fields for a summary, a location, a URL and a list of attendees. It has no field that asks a server to allocate a conference. Providers that create a room when a person books through their own calendar UI do it through their own API — a conferencing structure on their event resource, hooked by an add-on that watches that UI. The CalDAV endpoint never sees it.

So an event this capability creates arrives with exactly the fields it was given. Reading such an event back shows an empty location and no provider conferencing property, even on an account whose conferencing integration is enabled and working. This is structural. No flag, no header and no property in the VEVENT changes it.

## Where the room comes from instead

Decide once, per connection.

**A standing room.** Where the principal has a permanent personal meeting URL, put it on the connection and every booking carries it. This costs nothing per booking and is the right answer whenever a recurring room is acceptable.

**A minted room.** Where each meeting needs its own room, the minting belongs to a conferencing capability: it creates the meeting, hands back a join URL, and this capability carries that URL into the event. The two calls are ordered — mint first, book second — so a failed mint never produces an event that promises a link it does not have.

**No room.** An internal block, a hold, or an in-person meeting needs neither; a room name or a street address is free text on the event.

`calendar help` names the fields and flags each of these uses.

## What round-trips

Free-text fields survive the round trip intact: summary, location, description and URL are read back as they were written. A join URL is safe in either the URL property or the description; put it in the URL when a consumer will parse it, and in the description when a human will read it. Attendees are carried too, each with its participation status, so an invitation that a server dispatches is visible on the next read.

## Telling a consumer the truth

Every booking reports its conferencing status, and that report is always the same: none. The field is not decoration. A consuming project that reads it cannot come to believe a room was allocated on its behalf, whatever URL the event carries. Where a booking flow promises a link to a human, the flow — not this capability — owns proving that the link exists.
