# WhatsApp — operational model

How a WhatsApp identity is linked, what arrives when, and what each of those arrivals costs.

A connection is one WhatsApp identity, driven by the engine the CLI carries: whatsmeow, compiled into the installed wheel and run in-process, so a consuming project needs nothing beyond the CLI. The credential is the linked-device session itself, minted by pairing into the user state home beside the account's store. A connection may instead name a self-hosted WAHA bridge, in which case the identity is that instance's session and the bridge's own wiring lives on the entry; the verb surface is the same either way.

## The device

Linking adds an entry to the account's Linked Devices list on the phone, and that entry is the only place a person sees what is attached. The capability names itself there recognisably and declares itself a desktop, because it runs on a machine. A person can remove it at any time, which is a state the CLI reports as itself so the remedy — link again — is never mistaken for an empty account.

## Three arrivals, and only one of them repeats

**The pairing burst** arrives once, in the seconds after a device links, and carries the account's chats plus a window of their messages. Its depth is a pairing-time declaration. It is never repeated: a reconnect delivers none of it, so an invocation that links a device holds the connection open until the burst has landed.

**The offline queue** is what the server held for the device while nothing was connected. It drains by the act of connecting, at no extra cost, so every verb that reaches the network collects it. This is what makes a design with no daemon viable: between invocations nothing is captured live, and the next connection catches up. How often that happens is the consuming project's decision — by hand, from a routine, or on a schedule.

**Reaching back** is asked for, anchored on the oldest message already held, and answered by the phone rather than by WhatsApp's servers. It walks backwards only; there is no "fetch newer" counterpart, because forward coverage is the queue's job. The phone must be online to answer, and its silence is a distinct failure rather than an empty result.

## Depth is separable from reach

A shallow initial window does not cap how far the capability can later go: anchored paging walks straight past the pairing boundary. So the default takes little at pairing and deepens when a read asks for more than the store holds. Reaching back is paid for per request, so warming a large conversation is minutes of work rather than an instant.

Declaring a deep initial sync is available where a consumer genuinely wants the whole archive on disk from the start. It is a deliberate choice, because an archive already written cannot be unwritten.

## What the record cannot hold

**Media has a retention window, and it differs by kind.** The decryption keys stay valid indefinitely; the ciphertext leaves WhatsApp's content network. Measured on one account: an image fetched cleanly at 28 days and was refused at 41; a voice note was refused at 25. So the horizon is weeks rather than months, and shorter for voice than for images. Attachments are worth capturing close to arrival, and a loss is recorded as a loss rather than retried forever or quietly omitted.

**Some messages arrive unreadable.** A small share of any capture is empty or undecryptable — the client could not decrypt it at the time. Those rows are kept as what they are, so a reader can see the gap instead of inferring silence.

**Identities arrive in two forms.** A sender is either a phone-number identity or a privacy-preserving one, and group participants in particular arrive as the latter, which says nothing to a reader. Both forms are kept and resolved against the account's own contact store, so a name is available wherever one is known and the mapping stays reversible.

## Session lifecycle

Two failure modes deserve deliberate handling, because nothing watches the session between invocations.

**Removal from the phone** ends the session immediately. The next invocation says so and names re-linking as the remedy.

**Inactivity** ends it eventually: WhatsApp drops linked devices that have not connected for a while. A capability that only runs when a human asks can go weeks untouched, and re-linking then yields a fresh shallow burst — a gap in the record that reaching back can only partly repair. Running any network-touching verb occasionally both keeps the session alive and drains the queue, which is the strongest argument for something periodic even with no service running.

## One writer at a time

Two clients driving one linked device desync it and can log it out, so an invocation takes an exclusive lock on the session for as long as it is connected and a second invocation is refused rather than queued. The refusal is never worked around.

## Adding an identity

Adding a second WhatsApp identity is a connection-registry edit plus one pairing ceremony. Each connection keeps its own session and its own store, so two accounts never share a file and several projects consuming one account share one capture instead of each building their own.
