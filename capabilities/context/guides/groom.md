# Context — groom

Use this to review an agent's doctrine documents for semantic quality. The store checks the file form; grooming checks whether each document is still worth carrying, sits at the right altitude, routes by its description, and says each fact in one home.

## Flow

1. Run `context list` for the documents with their load and order.
2. Load `context guide authoring`.
3. Read each target with `context show <name>`; `context history <name>` and `context diff <name>` show how one came to read as it does.
4. Judge only against the authoring guide and the document's current job for this agent.

## Checks

- **Earned** — the document governs work the agent still does.
- **Altitude** — inline only for orientation, standing law, routing and safety; doctrine needed only sometimes is a stub; a procedure is a routine; evidence and history are assets; a capability's commands are that capability's help and guides.
- **Routing** — a stub's description is one line that says when to load it, enough for a fresh session to decide from it alone.
- **One home** — no fact restated from another document, a routine, a capability's help or guides, or memory; a duplicate names its owner instead.
- **Live** — present tense; no history framing, dated narrative, plans, to-dos, or old names used as doctrine.
- **Commands that exist** — every command the document prescribes is in the current help of the tool it names; a command of a tool no longer installed, or an edit to a folder whose layer the store now holds, is a finding.
- **Shape** — each paragraph and list item is one physical line.

## Output

Return `no findings` when every document is earned, well-placed and routes.

For findings, group by document. Each finding gives the quoted fragment, the check it fails, and the smallest fix or move, naming the home it moves to. Change documents only when the caller asked for edits: save each through `context save <name> --from FILE --base <id> --note "<why>"`, keep evidence that leaves a document as an asset with `assets save` first, and run `capabilities build` at the end and read its warnings.
