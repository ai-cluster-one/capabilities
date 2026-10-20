# context — change log

## 2026-10-20 — A read is a transaction of its own in one round trip

`context` uses `capabilities-contract` 0.6.0. On its working connection a plain read that would open a transaction, and any statement in autocommit, reaches the store in one round trip - the transaction's start, the schema binding and the commit go with it - and leaves the connection idle, never `idle in transaction`, so a transaction-mode pooler hands its server session on as soon as the store answers; a write still opens a transaction that its commit closes, and a write whose commit may have reached the store is never sent twice. A connect that fails is tried again after jittered, doubling pauses, and once a store did not answer, a further connect to it is refused at once as `store_unreachable` for a doubling pause of at most 30 seconds rather than dialled again on every statement.

## 2026-10-20 — An agent's doctrine is kept in the store as versioned documents

`context` keeps each doctrine file of a project, or of every project with `--global`, as a document in the project's PostgreSQL store: `context_documents` holds what never changes about it, `context_versions` every version of its front matter and body. A save adds a version against the base it names and is refused as `stale_base` when another save came first; the store keeps one version active, refuses any change to a version but clearing `active`, and deletes nothing. `list`, `show`, `history` and `diff` read the documents, `retire` frees a name, and `import` and `export` carry a context folder in and out byte for byte.
