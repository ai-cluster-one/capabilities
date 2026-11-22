# macmail — change log

## 2026-11-22 — `macmail help` is the menu, and `macmail help <verb>` one verb's whole contract

`macmail help` printed the whole reference, about 4 KB with flags on 20 of its lines, and `macmail help <anything>` was refused by the parser with exit 2. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", about 2.6 KB: the Automation grant macmail needs, every verb on one line saying what it answers, how an account is matched and the exit codes, then the contract verbs and guides the library renders, with no flag on any line. `macmail help <verb>` prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, what each flag does and its default, the text the old help held for it, its output in text and with `--json`, its exit codes and an example - for accounts, mailboxes, read, search, show, links, attachments, fetch, draft and export, with the account and mailbox matching, the Automation grant and the read-only switch on the pages they govern. The old contract commands table is the library's, and macmail's own text for doctor, connections, guide, ids, refs and inventory follows the library's page for each. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `macmail help`. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `macmail` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Почта Apple, and Estonian (`et`), Apple'i Mail, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `macmail` declares its title, Apple Mail, and its Lucide icon, `mail`

The manifest carries `title`, `Apple Mail`, the capability's human name, fixed here and never renamed, and `lucide_icon`, `mail`, the Lucide name of its line icon, from the `TITLE` and `LUCIDE_ICON` constants, so a reader draws the icon as a vector at its own weight and size. The capability binds `capabilities-contract` 0.48 or later, which emits them. It is still addressed as `macmail`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-10-31 — `macmail` carries its own icon

The bundle holds its icon as `icon.png`, an envelope: Lucide's `mail` line icon from `lucide-static` 1.66.0, drawn in one colour (`#64748b`) on a transparent ground at 256 x 256. `capabilities icon macmail` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

Lucide's notice, which ships with the icon:

ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

## 2026-10-13 — `macmail` reads its gate and identifiers from the store in a project whose records are there

`macmail` now pins `capabilities-contract==0.4.1`, the library the store is reached through, because in a project whose `project.json` says `"records": "store"` the gate it passes before every operational verb and its identifiers are read from the store. Elsewhere it reads its files as before.
