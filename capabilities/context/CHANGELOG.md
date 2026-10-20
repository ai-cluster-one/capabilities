# context — change log

## 2026-10-20 — An agent's doctrine is kept in the store as versioned documents

`context` keeps each doctrine file of a project, or of every project with `--global`, as a document in the project's PostgreSQL store: `context_documents` holds what never changes about it, `context_versions` every version of its front matter and body. A save adds a version against the base it names and is refused as `stale_base` when another save came first; the store keeps one version active, refuses any change to a version but clearing `active`, and deletes nothing. `list`, `show`, `history` and `diff` read the documents, `retire` frees a name, and `import` and `export` carry a context folder in and out byte for byte.
