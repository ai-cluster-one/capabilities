# ical — change log

## 2026-11-10 — `ical` declares its title, Apple Calendar, and its Lucide icon, `calendar`

The manifest carries `title`, `Apple Calendar`, the capability's human name, fixed here and never renamed, and `lucide_icon`, `calendar`, the Lucide name of its line icon, from the `TITLE` and `LUCIDE_ICON` constants, so a reader draws the icon as a vector at its own weight and size. The capability binds `capabilities-contract` 0.48 or later, which emits them. It is still addressed as `ical`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-10-31 — `ical` carries its own icon

The bundle holds its icon as `icon.png`, a calendar: Lucide's `calendar` line icon from `lucide-static` 1.66.0, drawn in one colour (`#64748b`) on a transparent ground at 256 x 256. `capabilities icon ical` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

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

## 2026-10-13 — Outside any project, `ical connections` and `ical doctor` read the machine's own connections

`ical` declares `connections` and `doctor` as machine reads. Run outside any project, `ical connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `ical doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `ical` declares what a person is asked for, and writes it through the contract

`ical manifest` now declares a connection's fields under `connections.fields` (`calendars`), each with its label, help, type, whether it is required and whether it is a secret, and `ical connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `ical connection remove <id>` takes it out again with the secrets it wrote. Bare `ical help` ends with a section rendered from these declarations. Nothing else changes.
