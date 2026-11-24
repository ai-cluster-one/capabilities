# routine — change log

## 2026-11-24 — `routine inventory` counts the project's routines where it keeps them, and the icon is `scroll`

`routine inventory` answered the empty envelope; it now answers one metric, `routines`, with `kind` `documents` and `glyph` `page`, so a reader draws the capability's tile on an agent's page from the answer itself. It counts the routines by the rule `titles` reads by: the store's, its own and the global ones it has enabled, once the store holds an active routine of this project, else the routine files in the routines folder, with a note saying which. Where no store is configured the folder is the only home and the count answers bare. Where one is, only the store can say which home it is, so the capability declares its inventory network `optional`: a bare run opens no store and names `routines` in `deferred`, and `routine inventory --network` reads the store and answers the count, or names it in `deferred` with what the store said when it does not answer. Outside a project the count is 0. `LUCIDE_ICON` is `scroll`, in place of `list-ordered`; `icon.png` is unchanged. The capability binds `capabilities-contract` 0.53 or later, which supplies the tile helper. No other verb, flag or exit code changes.

## 2026-11-23 — A routine in the store can hold one draft: saving it adds no version, and publishing it adds one against its base

`routine draft save <name> --from FILE|- [--base ID] [--global]` keeps the file form as the routine's one draft and adds no version; saving again writes over it and keeps its base, which is --base where given, else the draft's base, else the active version. `routine draft show <name> [--global] [--json]` prints the draft, with --json its `scope`, `base`, the `active` version, `stale`, `saved_at`, `saved_by` and `content`; `routine draft discard <name> [--global]` removes it (`"changed": false` with none); `routine draft publish <name> [--base ID] [--note TEXT] [--global]` adds the draft as one new version against its base and removes it in one transaction, answering what `save` answers, and refuses with exit 7 `stale_base`, naming the draft's base and the active version and keeping the draft, when another save came after the base; publishing again with --base naming the active version confirms the draft over it. A draft equal to the active version adds no version and is removed. A draft is never read: `list`, `show`, `history`, `diff`, `titles`, `export` and the routine index the manager builds read versions only. `list --store`, and `list` where the store holds the routines, marks a routine with a draft, `(draft)` in text and `draft` in each JSON entry; the folder listing is unchanged, since a routine file has no draft. A draft is only of a routine already in the store, and a global routine's draft is its author's. draft save, discard and publish are writes, refused under the read-only switch and in a system agent; draft show reads. `no_draft` exits 3 where there is none. Ledger step `0009-drafts` (routine tables 2.2) adds `routine_drafts`, one row per routine with a draft; a reader at 2.1 keeps working against 2.2. Every other verb, flag, answer and exit code is unchanged.

## 2026-11-22 — `routine help` is the menu, and `routine help <verb>` one verb's whole contract

`routine help` printed the whole reference, about 11.5 KB with flags on 38 of its lines, and `routine help <anything>`, an unknown word included, was refused by the parser with exit 2. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", under 6.5 KB: what a routine is, every verb on one line saying what it answers under THE ROUTINES FOLDER, ROUTINES IN THE STORE, GLOBAL ROUTINES and THE AGENT'S ROUTINES AND THEIR TITLES, THE FILE FORM, which verbs are writes, STORE, the I/O and the exit codes, then the contract verbs and guides the library renders, with no flag on any line. `routine help <verb>` prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, what each flag does, the text the old help held for it, its output in plain lines and with `--json`, its exit codes and an example - with the I/O and the exit-code table on each; the version-id rule is on `show`, `diff` and `save`, the `--global` reading rule on `show`, `history` and `diff`, the write gates on the write verbs, GLOBAL ROUTINES on the verbs that take `--global` and on `transfer`, and where the agent's routines live on `titles` and `title`. `help list` covers `list --store` and `list --store --global`, and `help enable` and `help disable` are one page. The old contract commands section is the library's, and routine's own text for doctor, connections and migrate follows the library's page for each. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `routine help`. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `routine` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Процедуры, and Estonian (`et`), Rutiinid, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `routine` declares its title, Routines, and its Lucide icon, `list-ordered`

The manifest carries `title`, `Routines`, the capability's human name, fixed here and never renamed, and `lucide_icon`, `list-ordered`, the Lucide name of its line icon, from the `TITLE` and `LUCIDE_ICON` constants, so a reader draws the icon as a vector at its own weight and size. The capability binds `capabilities-contract` 0.48 or later, which emits them. It is still addressed as `routine`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-11-08 — An agent's routines go with it, at routine 2.1

Step `0008-keys` (routine 2.1, core 4.0) keys `routine_documents`, `routine_versions` and `routine_enablements` as context does: an agent's routines, versions and enablements go with it, a global routine's author loses the pointer (`ON DELETE SET NULL`), and a version and an enablement go with their routine. The versions guard admits the cascade of a deliberate delete and nothing else. A reference to an agent the store no longer holds is settled when the step runs, kept in `routine_premig_m9`. Runs on capabilities-contract 0.45. No verb, flag or JSON key changes.

## 2026-11-08 — Help no longer names ContextKit

`routine help` describes the routine file form, `titles` and `title` on their own terms, where it measured them against ContextKit, a retired tool no machine carries any more. Nothing they do changes: no verb, flag, output key or file form changes.

## 2026-11-05 — The store names the agent: routine's tables go to schema major 2

`routine`'s tables name an agent where they named a project: `routine_documents.project_id` is `agent_id`, `routine_documents.author_project_id` is `author_agent_id` and `routine_enablements.project_id` is `agent_id`, and every constraint and comment named for them moves with them. Step `0007-agent-names` renames them in place with every row kept and takes the ledger to 2.0. The move crosses only through `capabilities store upgrade`: outside it, this release refuses a store still at schema major 1 with `store_upgrade_needed`, naming that command, and a release of routine 1.x refuses a store that crossed rather than misreading it. On an empty store, or one that crossed, a first routine call makes them at 2.0. Declares `capabilities-contract>=0.43,<1`, the library that reads the store's core tables by their new names. No verb, flag, environment variable or output key changes.

## 2026-10-31 — `routine` carries its own icon

The bundle holds its icon as `icon.png`, a numbered list: Lucide's `list-ordered` line icon from `lucide-static` 1.66.0, drawn in one colour (`#64748b`) on a transparent ground at 256 x 256. `capabilities icon routine` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

Lucide's notice, which ships with the icon:

ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

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
