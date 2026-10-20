# routine — change log

## 2026-10-20 — A read is a transaction of its own in one round trip

`routine` uses `capabilities-contract` 0.6.0. On its working connection a plain read that would open a transaction, and any statement in autocommit, reaches the store in one round trip - the transaction's start, the schema binding and the commit go with it - and leaves the connection idle, never `idle in transaction`, so a transaction-mode pooler hands its server session on as soon as the store answers; a write still opens a transaction that its commit closes, and a write whose commit may have reached the store is never sent twice. A connect that fails is tried again after jittered, doubling pauses, and once a store did not answer, a further connect to it is refused at once as `store_unreachable` for a doubling pause of at most 30 seconds rather than dialled again on every statement.

## 2026-10-20 — Routines are kept in the store as versioned documents

`routine` keeps each routine of a project as a document in the project's PostgreSQL store: `routine_documents` holds what never changes about it, `routine_versions` every version of its title, description and body, both created through the contract's ledger. `show`, `history` and `diff` read a routine and name each version's id, so a follower can record the version it followed; `save` adds a version against the base it names and is refused as `stale_base` when another save came first; `retire` frees a name; `import` and `export` carry the routines folder in and out byte for byte, and `list --store` lists the store's routines while `list` keeps reading the files. The store keeps one version active, refuses any change to a version but clearing `active`, and deletes nothing. A file whose front matter ContextKit reads but whose keys stand out of order or include others is imported with those keys dropped and marked `normalised`.

## 2026-10-13 — `routine` reads its gate and identifiers from the store in a project whose records are there

`routine` now pins `capabilities-contract==0.4.1`, the library the store is reached through, because in a project whose `project.json` says `"records": "store"` the gate it passes before every operational verb and its identifiers are read from the store. Elsewhere it reads its files as before.
