# CallVA — prompt authoring (voice agent prompts)

The `callva` CLI is a pure adapter: it lists, fetches, creates, and updates
prompt assets and the agents that reference them. It does not write or judge
prompt content — **the prompt text is authored by the agent**. This guide is how
to do that well: what makes a voice-agent prompt good, how a prompt binds to an
agent, and the revise-and-push discipline. Command shapes live in `callva help`;
orchestration (which prompt, when, for which agent) belongs to a routine in the
consuming project, which names this procedure by role.

## What the tool gives you

- `callva assets list --type prompt` / `callva assets get <id>` — the prompt
  inventory, and one prompt's content.
- `callva assets create` / `callva assets update <id>` — author a new prompt or
  revise an existing one (large content goes through a file flag, never inline).
- `callva agents get <id>` / `callva agents update <id>` — read and set which
  prompt an agent runs.

Run `callva help` for exact flags; this guide never restates them.

## Agent–prompt linkage

An agent references its prompt by id, embedded in the agent's `prompt` field as
`{{prompt:<asset_id>}}`. A prompt is therefore not stored *on* an agent — it is
an asset the agent points at. Consequences:

- To see what an agent actually runs, read the agent and resolve the
  `{{prompt:<asset_id>}}` reference to the asset.
- Repointing an agent at a different prompt is an *agent* update, not a prompt
  edit.
- One prompt asset can back several agents; editing it changes every agent that
  references it at once.

## Voice-specific quality

A voice prompt is not a chat prompt — the caller is listening, not reading, and
cannot scroll back. When authoring or reviewing one, hold it against:

- **Structure** — a clear role definition up front; instructions ordered the way
  the call actually flows; an explicit greeting and farewell; defined behaviour
  for uncertainty and error.
- **The voice medium** — conversational tone and short turns; graceful handling
  of interruptions and of silence; read critical values (numbers, dates, names)
  back to the caller and confirm before acting on them.
- **Clarity** — name the edge cases; give each instruction one clear home;
  consolidate duplicates and conflicts; every referenced variable must exist.
- **Precision** — enumerate genuinely closed sets. Express open-ended behaviour
  as a concise rule with representative examples, adding detail only when
  evaluation justifies it.

## Revise in place vs. new version

- **Revise in place** for typo fixes and minor wording — same asset, an update.
- **New version** for significant rewrites, alternative approaches, A/B
  experiments, or language variants — create a fresh asset, then offer to
  repoint the agent at it. A new version leaves the prior one intact to roll
  back to.

Name versions so the distinction is legible at a glance — encode the axis that
varies (audience, length, language, iteration), not just a number.

Treat each language variant as independently tested. Apply shared semantic
changes across variants deliberately, then validate each in its own language.

## Diff before you push

Ground behaviour changes in transcript or evaluation evidence, or in an explicit
requirement; safety, compliance, and business constraints may define edge cases
before drift is observed. Make the smallest coherent edit that fits the existing
structure, and expand it only when evaluation justifies more detail.

Before overwriting a prompt, compare your intended content against the live
asset (`callva assets get <id>`). If the live version carries changes your copy
does not, someone revised it outside this flow — **stop and surface the
divergence; never blind-overwrite**. Push only once the content is the
deliberate next state. After any write, re-read the intended asset and agent
binding, report the asset id, and rerun the target scenario plus an adjacent
regression scenario.
