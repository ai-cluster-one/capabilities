# routine — change log

## 2026-10-29 — A system agent's routines are not written

In a system agent - one an app keeps on its machine for itself, `capabilities_agents.kind` `system`, made with `capabilities init --system` - every write verb (`save`, `retire`, `import`, `title`, `enable`, `disable`, `transfer`) is refused with exit 4 `system_agent` and changes nothing; reading is unchanged, and so is every other agent. The capability now needs `capabilities-contract` 0.37.0, whose core tier carries the refusal (`_system_agent_gate`).

## 2026-10-24 — Any routine is switched off for one agent, its own included

`routine disable <name>` switches a routine off for the agent it runs in, and `routine enable <name>` switches it back on. The name names the agent's own routine where it has one, else the global routine of that name, as `show`, `history` and `diff` resolve it, and `--global` names the global one. A global routine is switched as before, by its enablement. A routine of the agent's own switched off stays in the store and in every verb, still shadows a global routine of its name, and leaves the routine index the manager builds; who switched it off and when are recorded. Both answers carry `origin` (`own` or `global`) and `enabled`. `routine list --json` gives every routine it lists `origin` and `enabled`: in the store, the agent's own routines that are off are listed with `enabled` false, and `list --store --global --json` run in a project marks each global routine `enabled` or not for it; a routine file is `own` and on. The text form of `list --store` marks a routine that is off `(off)`. The ledger goes to 1.2: `routine_documents` gains `disabled_at` and `disabled_by`. A routine CLI of ledger 1.1 reads a 1.2 store as before, and lists a routine that is off as on.

Before this change, `enable` and `disable` with the name of a global routine the agent shadows with a routine of its own acted on the global routine's enablement; they now act on the agent's own routine, and `--global` keeps the former action.

## 2026-10-23 — import, export, titles and title find the routines folder of an agent whose routines the store holds

`routine import` and `routine export` without DIR, `routine titles` and `routine title` take the folder from the manager's `path --json` answer also where it names the routines layer as held in the store (`{"where": "store", ..., "folder": <path>}`), so they use the same folder as before.

## 2026-10-23 — A global routine is written once and taken up by each project that enables it, and only its author project changes it

`routine save <name> --global` keeps a routine of no project in the store, authored by the project that first saves it; `list --store --global` lists them with their authors, and `show`, `history` and `diff` read one with `--global`, from a project or outside one. A global routine reaches a project only once it runs `routine enable <name>`, recorded with who and when, and leaves it on `routine disable <name>`: enabled, it is in that project's `list --store` (marked global, with its author), `show`, `history`, `diff` and `titles`, and in the routine index the manager builds, at its active version. A routine of the project's own with the same name shadows it, and `list --store` marks the project's routine as shadowing one. Only the current author project saves a new version of a global routine, retires it, or hands it on with `routine transfer <name> --to <project id>`; any other project is refused with exit 4 `not_author`. The ledger goes to 1.1: `routine_documents.project_id` is NULL for a global routine and `author_project_id` names its author, and `routine_enablements` holds what each project has enabled. A routine CLI of ledger 1.0 reads a 1.1 store as before. Project routines and every other verb are unchanged.

## 2026-10-22 — `titles` lists an agent's routines with their titles, and `title` sets or clears one

`routine titles [--json]` answers the agent's routines where they live - in the store when it holds an active routine of the project, else in the routines layer the manager names, the rule the manager builds the routine index by - each with its name, title (its name when it has none, `title_source` saying which), description and source, the folder's path or the store's active version. For a folder it lists what `contextkit routines --json` lists. `routine title <name> <title>` and `routine title <name> --clear` set or remove a routine's display title: in the folder the `title:` line ContextKit writes, in the store a new version holding that same title. The read creates no table; a store that is set but does not answer is read past to the folder with a warning, and refuses a title. `list` and the other verbs are unchanged.

## 2026-10-21 — A connection whose network flow died fails in seconds

`routine` uses `capabilities-contract` 0.7.2. Its store connections give a network flow up once data sent on it has gone unacknowledged for 20 seconds (`tcp_user_timeout`), which keepalives cannot notice since none is sent while data waits, and send keepalives after 15 seconds idle, every 5 seconds, 3 probes: a connection whose flow stopped carrying packets without a FIN or a reset fails in about 20 seconds as a lost connection instead of waiting until the store ends the session, and a plain read that loses it that way runs once more on a new connection. A connection lost where `routine` catches nothing ends the command as `store_unreachable`, exit 5, as before.

## 2026-10-20 — A read is a transaction of its own in one round trip

`routine` uses `capabilities-contract` 0.6.0. On its working connection a plain read that would open a transaction, and any statement in autocommit, reaches the store in one round trip - the transaction's start, the schema binding and the commit go with it - and leaves the connection idle, never `idle in transaction`, so a transaction-mode pooler hands its server session on as soon as the store answers; a write still opens a transaction that its commit closes, and a write whose commit may have reached the store is never sent twice. A connect that fails is tried again after jittered, doubling pauses, and once a store did not answer, a further connect to it is refused at once as `store_unreachable` for a doubling pause of at most 30 seconds rather than dialled again on every statement.

## 2026-10-20 — Routines are kept in the store as versioned documents

`routine` keeps each routine of a project as a document in the project's PostgreSQL store: `routine_documents` holds what never changes about it, `routine_versions` every version of its title, description and body, both created through the contract's ledger. `show`, `history` and `diff` read a routine and name each version's id, so a follower can record the version it followed; `save` adds a version against the base it names and is refused as `stale_base` when another save came first; `retire` frees a name; `import` and `export` carry the routines folder in and out byte for byte, and `list --store` lists the store's routines while `list` keeps reading the files. The store keeps one version active, refuses any change to a version but clearing `active`, and deletes nothing. A file whose front matter ContextKit reads but whose keys stand out of order or include others is imported with those keys dropped and marked `normalised`.

## 2026-10-13 — `routine` reads its gate and identifiers from the store in a project whose records are there

`routine` now pins `capabilities-contract==0.4.1`, the library the store is reached through, because in a project whose `project.json` says `"records": "store"` the gate it passes before every operational verb and its identifiers are read from the store. Elsewhere it reads its files as before.
