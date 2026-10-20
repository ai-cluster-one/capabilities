# assets — change log

## 2026-10-20 — A project's text assets as versioned documents in its store

`assets` keeps an agent's markdown assets - plans, research, sessions, meetings, journal, digests, reports and notes - as documents in the PostgreSQL the project resolves, in the tables `assets_documents` and `assets_versions` under its own migration ledger. A document is named by its path under `assets/` without `.md`, and its top folder gives its kind. Every save adds a version and leaves one active; versions are never edited or deleted, and the store refuses both. `save` takes an optional base and is refused with exit 7 `stale_base` when another save came first; `retire` hides a document without deleting anything. `list`, `show`, `history`, `diff` and full-text `search` read them, `import` reads an assets folder into the store, listing every file that is not markdown as skipped, and `export` writes the active versions back byte for byte.
