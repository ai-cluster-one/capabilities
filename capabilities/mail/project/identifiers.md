# Mail — identifiers

All fixed, non-secret structural values for this project's mail setup. Pure lookup; the operating model and command surface are in `mail --help`.

> Template note: fill these at install from the project's real Mail.app setup. There is **no connection section** — `mail` holds no secret or config; it authenticates through the macOS Automation grant (see the capability file). Nothing to point at env.

## Accounts

The Mail.app account(s) this project acts on. `<account>` matches case-insensitively against the account's Mail.app name or any of its configured addresses; `mail accounts` lists what Mail.app knows.

| Selector | Role in this project |
|---|---|
| `<account-name-or-address>` | `<what this project uses it for>` — fill each account this project reads or drafts from |

## Mailboxes

The mailboxes (folders) this project reads or exports, per account. `mail mailboxes <account>` lists the names.

| Account | Mailbox | Purpose |
|---|---|---|
| `<account>` | `<mailbox-name>` | `<why this project reads/exports it>` |

## Export destination

Where `mail export` dumps a mailbox to JSON for this project, if it does. State the path convention this project uses.

| What | Path |
|---|---|
| Mailbox export JSON | `<e.g. .assets/messages/email/<conv>/messages.json>` |
