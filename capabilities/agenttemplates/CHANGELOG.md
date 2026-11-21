# agenttemplates — change log

## 2026-11-21 — `agenttemplates help` is the menu, and `agenttemplates help <verb>` one verb's whole contract

`agenttemplates help` is the short complete menu of SHEBANG.md, "Agent-first help is the surface": what the templates are and where the manager reads them, `doctor`, `list` and `show` each on one line, the I/O and the exit codes, then the contract verbs the library renders, with no flag on any line. `agenttemplates help <verb>` prints that verb's whole contract from `HELP_VERBS` - what it does, its output, its exit codes and an example - and an unknown verb exits 6 `unknown_verb` with nothing on stdout and the hint to run `agenttemplates help`. The paragraph naming the manager's `init --with-template` and `init --template` moved verbatim to the `list` and `show` pages; `help doctor` follows the library's page with what this doctor checks, that it takes no arguments, and its exit 1 for a missing templates folder; the contract commands are the library's own pages. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, JSON key, output or exit code changes.

## 2026-11-16 — `agenttemplates` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Шаблоны агентов, and Estonian (`et`), Agendimallid, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `agenttemplates` declares its title, Agent Templates, and its Lucide icon, `layout-template`

The manifest carries `title`, `Agent Templates`, the capability's human name, fixed here and never renamed, and `lucide_icon`, `layout-template`, the Lucide name of its line icon, from the `TITLE` and `LUCIDE_ICON` constants, so a reader draws the icon as a vector at its own weight and size. The capability binds `capabilities-contract` 0.48 or later, which emits them. It is still addressed as `agenttemplates`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-11-08 — The Personal Agent's onboarding runs the manager's commands

The Personal Agent template's `onboarding` routine has the agent run `capabilities build` and read its warnings after writing what it learned, where it ran `contextkit build` and `contextkit audit`, and close onboarding with `capabilities agent set --onboarding-status complete`, where it ran `contextkit onboarding complete`; ContextKit is a retired tool no machine carries any more. An agent made from the template before this change keeps the routine it was made with. Help points at `capabilities help` for the template verbs, where it pointed at `contextkit guide bootstrap`. No verb, flag or output key changes.

## 2026-10-31 — `agenttemplates` carries its own icon

The bundle holds its icon as `icon.png`, a page layout: Lucide's `layout-template` line icon from `lucide-static` 1.66.0, drawn in one colour (`#64748b`) on a transparent ground at 256 x 256. `capabilities icon agenttemplates` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

Lucide's notice, which ships with the icon:

ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

## 2026-10-20 — agenttemplates carries the agent templates

The starter context files and the `personal-agent` template are files in this bundle, listed by `list` and read by `show`; with the capability installed, the manager's `templates`, `init --with-template` and `init --template` read them from here.
