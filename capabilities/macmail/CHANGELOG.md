# macmail — change log

## 2026-10-13 — `macmail` reads its gate and identifiers from the store in a project whose records are there

`macmail` now pins `capabilities-contract==0.4.1`, the library the store is reached through, because in a project whose `project.json` says `"records": "store"` the gate it passes before every operational verb and its identifiers are read from the store. Elsewhere it reads its files as before.
