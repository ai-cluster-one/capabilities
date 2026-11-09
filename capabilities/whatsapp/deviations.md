# whatsapp — recorded deviations

Deliberate departures from the executable standard, kept here so an audit reads
them as choices rather than as drift to correct.

## `messages --json` emits the capability's own envelope

The standard asks a capability not to change a published output shape as a side
effect of changing how it fetches. This one did: the JSON that verb emitted was
the bridge's own wire objects, and no engine but that bridge can produce them.
The envelope replacing it is the one the export already publishes, so the two
read surfaces now agree, and the export's `schema_version` carries the change.

## The engine is pinned to a release that does not announce itself

neonize up to 0.5.2 appended a `<bot biz_bot="1"/>` node to every message addressed to a person, which the recipient's WhatsApp renders as an "AI" badge and which marks the sending account's traffic as automated. From 0.6.0 the engine leaves that node off by default and takes `NEONIZE_BOT_TAG=on` to restore it, and the pinned 0.10.0 does the same. A pin moved to a release that brings the default back would undo this choice, so that is checked before the pin moves.

The choice is not about hiding what the sender is. It is about where that is
said: a sentence in the message is answerable and belongs to the conversation,
while the protocol flag is a standing claim about the account, made on every
message, on accounts that also carry a person's own correspondence. Saying who
is writing is left to what gets written.

The flag could not be set from here. A Go runtime inside a shared library reads the environment its process started with, so a value exported after the interpreter is running never reaches it - which is why the default has to live in the engine rather than in a line of Python before the import.

## The process ends itself

Every path exits through one funnel that flushes and terminates rather than
returning to the interpreter's own shutdown. The engine runs its blocking call
on a thread it does not mark as a daemon, so an interpreter reaching normal
shutdown with that thread alive waits for it indefinitely — and a client that
outlives its invocation outlives the lock that invocation released, which is the
two-clients-on-one-device state that desyncs the account. Ending deliberately is
what makes the lock mean anything.

## Parsed rows are rebuilt from raw chunks, not migrated

The tables' shape is migrated through the store's ledger like any other owner's.
What a history chunk becomes is not: a change to how a chunk becomes rows raises
the parser version, and an account whose rows were built at an older version has
the chunks it kept re-parsed rather than its rows altered in place. The chunks
are written before anything parses them precisely so this is possible;
enrichment lives in its own table and is never touched by the rebuild.

## `connection add` refuses a number another connection links, and the refusal names the holder

The contract's `connection add` is answered the same way by every capability, and its refusals carry `code`, `message`, `hint` and `status` alone. This one wraps the bound verb with one refusal before it, exit 6 `number_linked`, when the number or account id the add gives is one whose device another connection already links on this machine, and `pair` refuses the same way. One WhatsApp account is reached through one connection, and another project is given that connection by a grant; the account's home on this machine is what records which connection linked it, and the contract has no step where a capability checks an entry before it is written. The refusal carries `account` and `holder`, `{connection, owner}`, beside its code, so a client can offer the holding connection instead of reading names out of the message. Everything else `connection add` does, and every other refusal, is the contract's own.
