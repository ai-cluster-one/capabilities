# macmail — change log

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
