# journal — change log

## 2026-10-31 — `journal` carries its own icon

The bundle holds its icon as `icon.png`, a notebook: Lucide's `notebook` line icon from `lucide-static` 1.66.0, drawn in one colour (`#64748b`) on a transparent ground at 256 x 256. `capabilities icon journal` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

Lucide's notice, which ships with the icon:

ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

## 2026-10-31 — `journal help` names the agent's vault in the credential cascade

Bare `journal help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-13 — Outside any project, `journal connections` and `journal doctor` read the machine's own connections

`journal` declares `connections` and `doctor` as machine reads. Run outside any project, `journal connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `journal doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `journal` declares what a person is asked for, and writes it through the contract

`journal manifest` now declares a connection's fields under `connections.fields` (`db_host`, `db_port`, `db_user`, `db_name`, `db_sslmode`, `db_password_env`, `user_id`), each with its label, help, type, whether it is required and whether it is a secret, and `journal connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `journal connection remove <id>` takes it out again with the secrets it wrote. Bare `journal help` ends with a section rendered from these declarations. Nothing else changes.
