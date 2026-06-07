# Asana

The project's **Asana access** — reading and driving its projects and tasks through the `asana` CLI. Tasks, comments (with @mentions that notify), board sections as status, tags, dependencies, and the API-only `external` field for machine state.

> Template note: `<namespace>` and every gid fill at install. Replace this role paragraph with how *this* project actually uses Asana (which workspace/boards, what raises a task, who it @mentions, whether it runs a task pipeline). Keep this file **lightweight** — role + pointers; the command surface is `asana help`, not here.

## Interaction

Via the `asana` CLI on `PATH` — run `asana help` first (the CLI surface is self-documenting). It authenticates with a Personal Access Token resolved by the cascade (project `.env(.local)` > `~/.config/asana/credentials.env` > process env); `--token-env <KEY>` acts as an alternate identity by naming the key that holds its PAT. Any `<task>` / `--project` / `--workspace` / `--assignee` / `--mention` accepts a bare gid **or** a full Asana URL.

The free plan has no custom fields, no Search API, and no native Rules — so status is **section membership**, low-cardinality labels are **tags**, and durable machine state (session ids, dedup keys) lives in the per-task **`external`** field. See [identifiers.md](identifiers.md) for the gids this project uses.

## Operational context (load on demand)

- [identifiers.md](identifiers.md) — the fixed values for this project: workspace / project / user gids, the token-name → identity map, board section gids, and any tag gids.
- [reference.md](reference.md) — the standing home for project-specific operational context (what each board/section means here, how this project uses `external`, escalation/@mention conventions). Ships empty as a self-describing scaffold; populated as context accrues.

> If this project runs an automated task pipeline over Asana (sections-as-status, `external` dedup, escalation to a human), that procedure is a **project routine** in `.routines/`, not part of this capability — point to it from the reference, don't embed it here.
