# calendar — change log

## 2026-11-21 — `calendar help` is the menu, and `calendar help <verb>` one verb's whole contract

`calendar help` printed the whole reference, about 20 KB with the connection and secret sections the library appends, its verb table carrying flags on 52 lines. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", about 5 KB: what the adapter is, the conferencing boundary, what a connection is, the write gate, TIME and RECURRENCE in brief, every verb on one line under COMMANDS, the I/O contract and the exit codes, then the contract verbs and the guides the library renders, with no flag on any line. `calendar help <verb>` prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, what each flag does, what it does, its output, its exit codes and an example - with connection selection, the I/O contract and the exit-code table on each; TIME is whole on the page of every verb that reads or writes events, RECURRENCE on `events`, `search`, `freebusy`, `create`, `update` and `delete`, UID LOOKUP on `show`, `update` and `delete`, the alarm SPEC, the conferencing boundary and the write gate on the write verbs. CONNECTIONS moved verbatim to `help connections`, after the library's connections home and secret cascade, and the declared connection fields are the library's `help connection`; the contract `context` verb has its page at `help context`, and the identifiers sentence follows the library's `help ids`. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `calendar help`. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, JSON key, output or exit code changes.

## 2026-11-16 — `calendar` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Календарь, and Estonian (`et`), Kalender, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `calendar` declares its title, Calendar, and its Lucide icon, `calendar`

The manifest carries `title`, `Calendar`, the capability's human name, fixed here and never renamed, and `lucide_icon`, `calendar`, the Lucide name of its line icon, from the `TITLE` and `LUCIDE_ICON` constants, so a reader draws the icon as a vector at its own weight and size. The capability binds `capabilities-contract` 0.48 or later, which emits them. It is still addressed as `calendar`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-10-31 — `calendar` carries its own icon

The bundle holds its icon as `icon.png`, a calendar: Lucide's `calendar` line icon from `lucide-static` 1.66.0, drawn in one colour (`#64748b`) on a transparent ground at 256 x 256. `capabilities icon calendar` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

Lucide's notice, which ships with the icon:

ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

Lucide's `calendar` is derived from the Feather project, whose notice ships with it too:

The MIT License (MIT)

Copyright (c) 2013-present Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## 2026-10-31 — `calendar help` names the agent's vault in the credential cascade

Bare `calendar help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-13 — Outside any project, `calendar connections` and `calendar doctor` read the machine's own connections

`calendar` declares `connections` and `doctor` as machine reads. Run outside any project, `calendar connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `calendar doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `calendar` declares what a person is asked for, and writes it through the contract

`calendar manifest` now declares a connection's fields under `connections.fields` (`backend`, `address`, `username`, `base_url`, `principal_url`, `secret_env`, `timezone`, `meeting_url`, `calendars`), each with its label, help, type, whether it is required and whether it is a secret, and `calendar connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `calendar connection remove <id>` takes it out again with the secrets it wrote. Bare `calendar help` ends with a section rendered from these declarations. Nothing else changes.
