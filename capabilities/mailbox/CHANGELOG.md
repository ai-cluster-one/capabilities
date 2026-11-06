# mailbox — change log

## 2026-11-06 — `mailbox draft` saves a letter into Drafts without sending it

`mailbox draft --to A --subject S` takes the body, `--cc`, `--attach` and `--in-reply-to` as `send` does and builds the same message, then saves it into the account's Drafts folder - the one the server marks `\Drafts`, else one named Drafts - with the `\Draft` flag. Nothing is sent. The answer names the folder, the new message's uid and its Message-ID; `--in-reply-to` writes `In-Reply-To` and `References`, so the draft threads under the message it answers. A draft is a write: on a connection without `allow_write` it exits 4 `read_only`, saying that writing drafts needs write access, and saves nothing. With no Drafts folder it exits 6 `drafts_folder_missing` and saves nothing. `help` and the stub line name the verb and its grant. `send` is unchanged.

## 2026-10-31 — `mailbox` carries its own icon

The bundle holds its icon as `icon.png`, an envelope: Lucide's `mail` line icon from `lucide-static` 1.66.0, drawn in one colour (`#64748b`) on a transparent ground at 256 x 256. `capabilities icon mailbox` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

Lucide's notice, which ships with the icon:

ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

## 2026-10-31 — `mailbox help` names the agent's vault in the credential cascade

Bare `mailbox help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-29 — `mailbox send` takes Bcc recipients

`mailbox send --bcc A`, repeatable like `--to` and `--cc`, delivers the message to A through the SMTP envelope alone. No Bcc header is written, so neither the message the recipients receive nor the copy saved to Sent names a Bcc recipient. The JSON result lists them under `bcc`. Sends without `--bcc` are unchanged.

## 2026-10-13 — Outside any project, `mailbox connections` and `mailbox doctor` read the machine's own connections

`mailbox` declares `connections` and `doctor` as machine reads. Run outside any project, `mailbox connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `mailbox doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `mailbox` declares what a person is asked for, and writes it through the contract

`mailbox manifest` now declares a connection's fields under `connections.fields` (`address`, `imap_host`, `imap_port`, `smtp_host`, `smtp_port`, `secret_env`), each with its label, help, type, whether it is required and whether it is a secret, and `mailbox connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `mailbox connection remove <id>` takes it out again with the secrets it wrote. Bare `mailbox help` ends with a section rendered from these declarations. Nothing else changes.
