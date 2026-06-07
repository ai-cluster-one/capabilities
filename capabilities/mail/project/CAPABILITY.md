# Mail

The project's **mail access** — reading incoming mail and staging outgoing **drafts** for the user, through the local Mail.app via the `mail` CLI. Read + draft only: it never sends, so anything it composes lands in the account's Drafts folder for the user to review and send by hand.

> Template note: `<namespace>` and the account/mailbox specifics fill at install. Replace this role paragraph with how *this* project actually uses mail (which account, which mailboxes, what it drafts), and keep the sibling links pointing at this folder's real files. Keep this file **lightweight** — role + pointers; the command surface is `mail --help`, not here.

## Interaction

Via the `mail` CLI on `PATH` — run `mail --help` first (the CLI surface is self-documenting). **macOS only**: it drives Mail.app over macOS Automation (JXA), so there is **no token or config** — it reads whatever accounts Mail.app is already signed into. First use on a machine needs a one-time OS grant (System Settings → Privacy & Security → Automation → the terminal → enable Mail).

`<account>` matches case-insensitively against an account's Mail.app name or any of its addresses — see [identifiers.md](identifiers.md) for the ones this project uses.

## Operational context (load on demand)

- [identifiers.md](identifiers.md) — the fixed values for this project: which account(s) it acts on, the mailboxes it reads/exports, and any export path convention.
- [reference.md](reference.md) — the standing home for project-specific operational context (which mailboxes mean what, drafting conventions, routing quirks in this project's mail). Ships empty as a self-describing scaffold; populated as context accrues.
