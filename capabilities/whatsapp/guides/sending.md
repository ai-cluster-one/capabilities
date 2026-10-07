# WhatsApp — sending

What it takes to send a message from a connection, what the recipient sees, and the one surprise that costs an afternoon.

## One gate, and it is the connection

`send` refuses on a connection that does not carry `allow_write`, and nothing else in this capability stands between a command and a real person. Whether a particular account needs a human's word before each message is a rule about that account, and it belongs where the consuming project states its rules — not in a mechanism imposed on every caller.

The gate answers before anything else: before the text is examined, before a row is written, before a session opens, before a credential resolves. A refusal is exit 4 and it names the connection.

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

## Every send is a row

A message is written into the store before it goes anywhere: a row in `whatsapp_messages`, from the account, with a delivery state and an internal id (`local_id`). It starts `pending`, becomes `sending` when a sender claims it, and ends `sent`, carrying the id WhatsApp minted and the time it was sent, or `failed`, carrying the reason. The copy of a sent message WhatsApp hands back lands on the same row, because by then the row carries WhatsApp's id, so a sent message is held once.

Who sends the row depends on who holds the account's connection. With the assistant service running on the account, the service sends it and `send` waits up to 60 seconds for the service's answer, so a worker or any other process sends through the same verb while the service runs. Without the service, `send` connects and sends the row itself. The answer is the same either way: the id WhatsApp minted, plus `local_id`, `delivery` and `sent_by` (`service` or `direct`).

A send the service has not answered within the wait exits 5 `send_pending` and names the local id. The row is still sent when the service can, so sending the same text again sends it twice. A row a sender claimed and never answered for - the process ended while the message was on the wire - is failed at the service's next start as `interrupted_delivery_unknown` and never sent again: whether it reached WhatsApp cannot be known, and sending it again could deliver it twice.

Reads show what WhatsApp has: a message still pending, or one that failed, is not part of the conversation `messages`, `chats`, `export` and `health` report.

## Quotes, mentions and typing

`--reply-to <message id>` quotes a message of the same chat. The store must hold it, because a quote carries the quoted message itself; one it does not hold exits 3 `quoted_not_found` with nothing written.

`--mention <phone>` mentions a number, and repeats for several. A mention renders where its `@number` is written in the text, so a number the text does not carry is put at its start.

`--typing` shows the account composing in the chat for a moment between 1.5 and 3.5 seconds, jittered, then paused, before the message goes.

## The send rate

The service sends at most `defaults.send_rate` messages a minute for its account, oldest first; the rest wait their turn as pending rows. The setting is the service's (`defaults.send_rate`, 1 to 120, default 20; a top-level `send_rate` is read as it). Automated volume on one account is what WhatsApp acts against, and it acts against the whole account, so the cap is per account rather than per caller.
