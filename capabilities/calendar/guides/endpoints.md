# Wiring a connection to a CalDAV endpoint

Use this guide to find the endpoint and credential a provider actually serves, and to know what to do when one of them is withdrawn. What the connection fields mean is `calendar help`'s; what to put in them is this guide's.

## Finding the endpoint

Discovery will not do it for you. Some providers serve `/.well-known/caldav`; several large ones answer it with a 404 and expect the client to be configured. Ask the provider's own documentation for the CalDAV base it publishes — [RFC 6764](https://www.rfc-editor.org/rfc/rfc6764) describes the discovery a provider may or may not implement — and put that value on the connection.

A provider that addresses its principals as a prefix plus an address needs only the base. One that does not needs the principal collection stated outright. Which of the two you are dealing with is answered by trying it: `calendar doctor` resolves the principal and reports what it found, per connection.

Nothing about any provider is compiled into this capability, so an endpoint that moves is a registry edit made once in the consuming project, and never a code change.

## Proving a host serves CalDAV at all

Not every mail host carries a calendar. A host that serves no CalDAV answers `PROPFIND` with `405 Method Not Allowed`, and this capability reports that as `not_caldav` rather than as a network fault, because the remedy is a different endpoint — or a different system entirely — and not a retry.

## The credential

Basic authentication over TLS is what CalDAV specifies and what this capability speaks. Where a provider issues application-specific passwords, the credential is one of those and not the account password; where an account has two-factor authentication, the application password is usually the only credential the CalDAV endpoint will accept. A few servers authenticate on a login distinct from the calendar's address, which the connection states separately.

Give each connection its own environment key so revoking one account's access never touches another's. An application password can stop working for reasons that have nothing to do with this capability: the issuing account's two-factor settings change, an administrator revokes application passwords for the domain, or the credential is retired on its own schedule. The failure arrives as an exit-2 auth refusal, so a consuming project should treat that code as *re-issue the credential*, not as *the calendar is down*.

## When Basic authentication is withdrawn

Some providers publish a modern, OAuth-only CalDAV endpoint beside one that accepts a password, and describe the password-accepting endpoint as legacy. That is a real risk to plan for and not a reason to avoid the protocol.

The fallback, when it is needed, is an OAuth bearer token against the provider's current endpoint. That arrives as a new transport on the connection rather than as a new capability or a changed schema, and a consuming project's calls do not change. What does change is the ceremony: a bearer token is minted and refreshed, so the consumer acquires one and the connection points at where it lives.

## Choosing the time zone

Set the connection's zone whenever its consumer states times in a local one — a booking rule that says "no meetings before 10:00" means 10:00 somewhere, and that somewhere belongs on the connection rather than in every call. A connection that serves only machine-generated timestamps carrying their own offsets needs none.

The consequences of leaving it unset, and what the capability does with a zone it cannot resolve, are `calendar help`'s TIME section.
