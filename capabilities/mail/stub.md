---
name: mail
description: Mail.app CLI (macOS) — read and draft mail across Mail.app's already-configured accounts (list accounts/mailboxes, read, search, show, list links, save attachments, fetch a linked URL, draft, export a mailbox to JSON). Read + draft only, never sends. Run `mail --help` for the full command surface before the first subcommand in a session.
---

Mail.app CLI — read and draft mail over macOS Automation (JXA), across the accounts Mail.app already has configured. Read + draft only; it never sends — drafts land in the account's Drafts folder for the user to send by hand.

- Executable: `mail` (on `PATH`)
- Credentials: **none** — there is no token or config file. The CLI drives Mail.app via macOS Automation, so it reads whatever accounts Mail.app is signed into. First invocation needs a one-time grant: System Settings → Privacy & Security → Automation → your terminal → enable Mail.
- Platform: **macOS only** (depends on Mail.app + JXA). No-op anywhere else.
- Load full reference: `mail --help`

`<account>` matches case-insensitively against an account's Mail.app name or any of its configured addresses. Run `mail --help` before issuing any subcommand the first time in a session. Project-scoped accounts, mailboxes, and routing live in each project's own assets, not here.
