# WhatsApp — sending

What it takes to send a message from a connection, what the recipient sees, and the one surprise that costs an afternoon.

## One gate, and it is the connection

`send` refuses on a connection that does not carry `allow_write`, and nothing else in this capability stands between a command and a real person. Whether a particular account needs a human's word before each message is a rule about that account, and it belongs where the consuming project states its rules — not in a mechanism imposed on every caller.

The gate answers before anything else: before the text is examined, before a session opens, before a credential resolves. A refusal is exit 4 and it names the connection.

## What the recipient sees

An ordinary message from the linked account, carrying no machine marking of any kind. Saying who is writing is the sender's job, and it belongs in the text, where a reader can answer it.

That is a choice about where disclosure lives, not an argument against it. A sentence in the message is part of the conversation; a protocol flag is a standing claim about the account, repeated on every message it sends.

## The "AI" badge, and where it comes from

WhatsApp has a protocol node — `<bot biz_bot="1"/>` — that a client can attach to an outgoing message. The recipient's app renders it as a small **AI** label under the message, and the sending account's traffic counts as automated from then on.

The engine appends it to messages addressed to a person, never to groups. Upstream does this by default. **The build this capability pins does not**: the node is opt-in, and `NEONIZE_BOT_TAG=on` restores it.

Two things follow, and both matter more than they look:

**The badge cannot be seen from this side.** It is not in the send result, not in the store, and not in anything a later read returns — the flag travels with the message and is rendered by the recipient. The only way to know is to send to an account you can look at and look at it. A self-chat does this in one message.

**The variable is read at process start, not at send.** The engine is a compiled library with its own runtime, and that runtime copies the environment when the library loads — from the list the process was started with. Exporting the variable from inside a running program updates the C environment and changes nothing here. It has to be in the environment the command was launched with:

```sh
NEONIZE_BOT_TAG=on whatsapp send <chat_id> "..."
```

This is the failure that looks like a working fix: the code sets the variable, the variable is set, and the badge still appears. If a change to this behaviour seems not to take, check whether it was set before launch before checking anything else.

## What the store does not hold

A sent message is not written into the local store by the act of sending, and the account's other linked devices do not receive it as an incoming message either. The send reports the id the account minted for it, which is what a later read would carry if the message is captured by some other path — but the local history will not show it. A record that has to include what was said outward is kept by whatever sent it, not by this capability's capture.
