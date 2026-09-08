# whatsapp — recorded deviations

Deliberate departures from the executable standard, kept here so an audit reads
them as choices rather than as drift to correct.

## The engine is a pinned wheel from a release asset, not an index package

The PEP 723 header resolves the engine from a GitHub release URL rather than
from a package index. The engine binds two calls the published package does not
expose — the on-demand history request and the generic peer message — and
without them a read can only ever return what one pairing burst happened to
hand over. The URL pins one immutable artifact, the same guarantee a pinned index version
gives. The compiled library travels inside the wheel rather than being fetched
at run time, so no other build can be substituted for it. What the wheel tag
does not carry is the platform it was built for, which defers a host mismatch
from install to first use — where it surfaces as a refusal naming the engine,
and where a read served from an existing store is unaffected, because the engine
is imported only by what reaches the account. When the change lands upstream
this becomes an ordinary pinned dependency.

## `messages --json` emits the capability's own envelope

The standard asks a capability not to change a published output shape as a side
effect of changing how it fetches. This one did: the JSON that verb emitted was
the bridge's own wire objects, and no engine but that bridge can produce them.
The envelope replacing it is the one the export already publishes, so the two
read surfaces now agree, and the export's `schema_version` carries the change.

## `send` is declared and refused

`send` is named in the surface and in `WRITE_VERBS`, and it exits 6 rather than
sending. The write gate is real and answers first, so a read-only connection
still refuses at exit 4; what is missing is the confirmation step a message that
reaches a person deserves. Declaring it keeps the gate and the refusal in one
place instead of leaving the verb absent and its policy undefined.

## The process ends itself

Every path exits through one funnel that flushes and terminates rather than
returning to the interpreter's own shutdown. The engine runs its blocking call
on a thread it does not mark as a daemon, so an interpreter reaching normal
shutdown with that thread alive waits for it indefinitely — and a client that
outlives its invocation outlives the lock that invocation released, which is the
two-clients-on-one-device state that desyncs the account. Ending deliberately is
what makes the lock mean anything.

## The store is rebuilt from raw chunks, not migrated

A change to how a history chunk becomes rows raises the parser version, and a
store opened at an older version re-parses the chunks it kept rather than
altering the rows in place. The chunks are written before anything parses them
precisely so this is possible; enrichment lives in its own table and is never
touched by the rebuild.
