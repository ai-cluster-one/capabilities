# Context — authoring

Use this when writing a new doctrine document in the store or substantially rewriting one. A document is live doctrine: what holds for this agent now, composed into the sessions that load it.

## One Fact, One Home

Every durable fact has one live home, and every other place names that home instead of restating it. A copy drifts from its source the first time either changes.

Before saving, find the existing owner and extend it rather than open a second one: `context list` and `context show <name>` for doctrine, `routine list --store` for procedure, `assets search <terms>` for evidence, `memory search <terms>` for recorded conclusions, and the owning capability's `help` and `guide` for its commands.

A capability's verbs, flags, outputs and identifiers belong to that capability's help, guides and `ids`. Doctrine names the capability and the surface that answers, and never copies the surface.

## Altitude

Put each fact at the lightest altitude that still reaches the work when it needs it:

- `load: inline` is always-on context, composed into every session: orientation, standing law, routing and safety. Keep it short; every line costs every session.
- `load: stub` shows a session only the document's title, description and `context show <name>`; the body is loaded when a task calls for it. Doctrine a task needs only sometimes is a stub.
- A capability guide (`<name> guide <topic>`) holds how to use that capability, and belongs to it.
- A routine holds a repeatable procedure with ordered steps (`routine guide authoring`).
- An asset holds evidence: sessions, plans, research, reports (`assets guide authoring`).
- A memory entry holds one conclusion that should govern later work and is found by search (`memory help`).

`order` places a document among the others composed at its altitude; lower comes first.

## Routing By Name And Description

A session decides whether to load a stub from its name, title and description alone, so the description is load-bearing: one line that says when to load it, starting `Load when`, and names the task or surface it governs. A description that summarises the body instead of saying when it is needed routes nothing.

The name groups by subject, as a path (`identity/MISSION`, `guidelines/AUTONOMY`), and the title says what the document is.

## Writing The Body

Open with what the document owns, so a reader knows what belongs in it and what does not.

State live doctrine in the present tense, as what holds. History, plans and to-dos are not doctrine: how a rule came to be is evidence and stays in an asset.

Write each paragraph and each list item as one physical line, and leave wrapping to the reader's editor. Break a line only where the break carries Markdown structure.

## Saving

1. `context show <name> --json` gives the active version and its id; a name not in use starts a new document.
2. Write the whole file form to a file: the front matter `title`, `description`, `load` and `order`, then the body.
3. `context save <name> --from FILE --base <id> --note "<why>"`. A `stale_base` refusal means another save came first: read the document again and make the edit on that version.
4. Run `capabilities build` and read its warnings, so the composed context carries the change.

`context diff <name>` shows what a save changed. `context retire <name>` takes a document out of the context and keeps every version. A global document (`--global`) reaches every agent that enables it, and only its author writes it.
