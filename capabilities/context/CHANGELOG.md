# context — change log

## 2026-11-08 — An agent's documents go with it, at context 2.1

Step `0007-keys` (context 2.1, core 4.0) keys `context_documents`, `context_versions` and `context_enablements`: an agent's documents, their versions and its enablements go with it, a global document's author keeps the document and loses the pointer (`author_agent_id`, `ON DELETE SET NULL`), and a version and an enablement go with their document. The versions guard admits the cascade of a deliberate delete and nothing else. A reference to an agent the store no longer holds is settled when the step runs, kept in `context_premig_m9`. Runs on capabilities-contract 0.45. No verb, flag or JSON key changes.

## 2026-11-08 — `context` ships the `authoring` and `groom` guides

`context guide` lists two guides, where it answered that none ship: `context guide authoring` for writing a doctrine document in the store - one fact in one live home, the altitude a fact belongs at (inline, stub, a capability guide, a routine, an asset, a memory entry), routing a stub by its name and description, the present tense, one line per paragraph, and saving with `context save --base` followed by `capabilities build` - and `context guide groom` for reviewing the documents against it. No verb, flag or output key changes.

## 2026-11-05 — The store names the agent: context's tables go to schema major 2

`context`'s tables name an agent where they named a project: `context_documents.project_id` is `agent_id`, `context_documents.author_project_id` is `author_agent_id` and `context_enablements.project_id` is `agent_id`, and every constraint and comment named for them moves with them. Step `0006-agent-names` renames them in place with every row kept and takes the ledger to 2.0. The move crosses only through `capabilities store upgrade`: outside it, this release refuses a store still at schema major 1 with `store_upgrade_needed`, naming that command, and a release of context 1.x refuses a store that crossed rather than misreading it. On an empty store, or one that crossed, a first context call makes them at 2.0. Declares `capabilities-contract>=0.43,<1`, the library that reads the store's core tables by their new names. No verb, flag, environment variable or output key changes.

## 2026-10-31 — `context` carries its own icon

The bundle holds its icon as `icon.png`, an open book: Lucide's `book-open` line icon from `lucide-static` 1.66.0, drawn in one colour (`#64748b`) on a transparent ground at 256 x 256. `capabilities icon context` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

Lucide's notice, which ships with the icon:

ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

## 2026-10-29 — A system agent's context documents are not written

In a system agent - one an app keeps on its machine for itself, `capabilities_agents.kind` `system`, made with `capabilities init --system` - every write verb (`save`, `retire`, `import`, `transfer`, `enable`, `disable`) is refused with exit 4 `system_agent` and changes nothing; reading is unchanged, and so is every other agent. The capability now needs `capabilities-contract` 0.37.0, whose core tier carries the refusal (`_system_agent_gate`).

## 2026-10-27 — A global document is changed only by its author project and reaches a project only once it enables it

A global document now works as a global routine does. The project that first saves it with `--global` is its author, and only the current author saves or imports a new version of it, retires it, or hands it on with `context transfer <name> --to <project id>`; any other project is refused with exit 4 `not_author`. Writing a global document therefore needs a project; reading one does not. A global document saved before documents had authors has none until a project writes it, which makes that project its author. A global document reaches a project only once it runs `context enable <name>`, recorded with who and when, and leaves it on `context disable <name>`: enabled, it is in that project's `list` (`origin` `global`, with its author) and in the context `capabilities build` composes, at its active version, and a document of the project's own with the same name shadows it, which `list` marks as `shadows_global`. `list --global` names each global document's author and, in a project, whether it is enabled there. A `--global` save or import refuses text that looks like a secret - a private key, a provider's key or token, a JSON web token, a password in a URL, or a password, secret, token or key assigned a value - with exit 6 `secret_like_text`, naming the line and the kind and never the value; personal data is not detected. The ledger goes to 1.1: `context_documents.author_project_id` names a global document's author, and `context_enablements` holds what each project has enabled. Project documents and every other verb are unchanged.

## 2026-10-23 — import and export find the context folder of an agent whose context the store holds

`context import` and `context export` without DIR take the folder from the manager's `path --json` answer also where it names the context layer as held in the store (`{"where": "store", ..., "folder": <path>}`), so they default to the same folder as before.

## 2026-10-21 — A connection whose network flow died fails in seconds

`context` uses `capabilities-contract` 0.7.2. Its store connections give a network flow up once data sent on it has gone unacknowledged for 20 seconds (`tcp_user_timeout`), which keepalives cannot notice since none is sent while data waits, and send keepalives after 15 seconds idle, every 5 seconds, 3 probes: a connection whose flow stopped carrying packets without a FIN or a reset fails in about 20 seconds as a lost connection instead of waiting until the store ends the session, and a plain read that loses it that way runs once more on a new connection. A connection lost where `context` catches nothing ends the command as `store_unreachable`, exit 5, as before.

## 2026-10-20 — A read is a transaction of its own in one round trip

`context` uses `capabilities-contract` 0.6.0. On its working connection a plain read that would open a transaction, and any statement in autocommit, reaches the store in one round trip - the transaction's start, the schema binding and the commit go with it - and leaves the connection idle, never `idle in transaction`, so a transaction-mode pooler hands its server session on as soon as the store answers; a write still opens a transaction that its commit closes, and a write whose commit may have reached the store is never sent twice. A connect that fails is tried again after jittered, doubling pauses, and once a store did not answer, a further connect to it is refused at once as `store_unreachable` for a doubling pause of at most 30 seconds rather than dialled again on every statement.

## 2026-10-20 — An agent's doctrine is kept in the store as versioned documents

`context` keeps each doctrine file of a project, or of every project with `--global`, as a document in the project's PostgreSQL store: `context_documents` holds what never changes about it, `context_versions` every version of its front matter and body. A save adds a version against the base it names and is refused as `stale_base` when another save came first; the store keeps one version active, refuses any change to a version but clearing `active`, and deletes nothing. `list`, `show`, `history` and `diff` read the documents, `retire` frees a name, and `import` and `export` carry a context folder in and out byte for byte.
