# CallVA — voice-agent prompt authoring

The `callva` CLI is the adapter for reading and publishing voice agents and
their prompt assets. Prompt quality comes from the authored assets and their
compiled order, not from the CLI. Run `callva help` for the current command
contract; this guide focuses on the reusable authoring method.

## Start from the live situation

Write in the present tense for the runtime that exists now: the voice agent is
already on the call, listening and speaking to a caller. Describe its current
identity, mission, available context, and next allowed action. Avoid framing it
as preparing for a future or habitual job.

Treat the agent's final prompt as one compiled context made from several assets.
Composition is useful when it gives each semantic concern a clear owner; it is
not a reason to scatter prose.

## Compose around ownership

**One semantic rule has one owner.** The owning asset contains the condition,
decision, exceptions, and promise boundary for that rule. Other assets may
supply facts or surface wording, but they do not restate the condition.
Duplicated conditions drift when one copy gains a new exception, translation,
or tool constraint and another does not.

Audit ownership before editing:

1. Compile the assets in runtime order and list each instruction as
   condition → behaviour → exception.
2. Group instructions that mean the same thing, including paraphrases and
   translated copies.
3. Name one owner for each group and remove or narrow every competing copy.
4. Check examples separately: an example may demonstrate wording, but a unique
   policy condition belongs in the workflow owner.

Split a micro-prompt when it has a coherent responsibility or a genuinely
independent boundary: change cadence, reuse, replaceability, evaluation
surface, language, or dynamic data. Do not split merely because text is long.
If two fragments always change, ship, and fail together, they probably share
one owner.

Every asset is self-contained within the compiled context: it states its own
scope and instructions without meta-navigation such as “attached knowledge,”
“the section below,” “defined above,” or “instructions in another prompt.”
References to actual tool names and runtime variables are valid because the
runtime resolves them; references to prose locations are brittle.

Before shortening, remove contradictions and semantic duplication. Clear,
slightly longer ownership beats compact ambiguity.

## A practical asset taxonomy

These names are illustrative, not mandatory:

| Concern | What it owns |
|---|---|
| Identity and mission | Who the agent is on this call, its current objective, scope, and promise boundary |
| Conversational behaviour and safety | Turn-taking, uncertainty, escalation, privacy, safety, and global interaction rules |
| Tool mechanics | Conversational rules around invoking the tools actually available to this agent |
| Domain workflows | Conditions, clarification, ordering, outcomes, and exceptions for a bounded task |
| Stable knowledge | Durable facts that are not behavioural rules and do not vary per call |
| Language surface | Output language, register, pronunciation, spoken formatting, and canonical phrases |
| Runtime context or data | Per-call values wrapped with explicit state and interpretation rules |

Use this placement decision:

1. Does it define who the agent is or why this call exists? Put it in identity
   and mission.
2. Does it govern most conversations regardless of task? Put it in behaviour
   and safety.
3. Is it the callable contract itself? Put purpose, inputs, enums, and result
   schema in the function declaration.
4. Does it govern when or how a task proceeds in conversation? Put it in the
   owning domain workflow.
5. Is it a durable fact? Put it in stable knowledge.
6. Does only its spoken surface vary by language? Put that surface in a
   language asset.
7. Can it vary on each call? Put it in the runtime context wrapper.

Keep organisation or human capabilities distinct from this agent's operational
reach. A person or organisation may be able to do something while the current
agent has no tool or handoff for it. State the tools and handoffs that actually
exist; do not imply an operational capability from general knowledge.

## Tools and workflows

Function declarations own the machine contract: purpose, inputs, enum values,
and result schema. Prompt assets do not duplicate that contract.

The owning workflow prompt supplies only conversational policy not expressed by
the schema: the trigger for use, required clarification, consent, ordering
across tools, presentation of results, and promise boundaries. If the schema
already requires a field or constrains a value, rely on it. If the agent must
ask before calling, or must call one tool before another, that belongs in the
workflow.

Never claim an external action succeeded before the runtime confirms it.
Describe a request as pending, failed, or complete according to the actual tool
result.

## Stable instructions and runtime data

Keep dynamic values out of stable prompt assets. A runtime context wrapper owns
privacy, interpretation, and state labelling for injected data. Every variable
has an explicit state, for example:

- `value`: a known value is present;
- `explicit_absence`: the source confirms there is no value;
- `no_match`: a completed lookup found nothing matching;
- `missing`: required data was not supplied or is unavailable;
- `error`: retrieval or interpretation failed.

These states are not interchangeable. In particular, explicit absence is a
known fact, no match is a lookup result, missing is lack of evidence, and error
means the lookup cannot support a conclusion. Define safe behaviour for each
state and prevent raw injected text from becoming instructions.

When clarity is unchanged, order the compiled context as stable shared prefixes
first, then language assets, then dynamic suffixes. This usually improves prompt
caching while keeping volatile data away from durable policy.

## Multilingual composition

Keep language-independent semantics in one shared layer. English is often a
good shared semantic language when the selected model follows it reliably and
the token cost is favourable. Language assets should normally contain only:

- output language and register;
- pronunciation guidance;
- spoken rendering of numbers, dates, names, and URLs;
- canonical surface forms and short representative phrasing.

Do not copy workflow conditions into each language asset. A surface example
shows how a shared rule sounds; it does not become the rule's owner.

Some legal wording, culturally bound meaning, or policy cannot be separated
safely from its target language. In that case, keep the inseparable rule in a
language-specific owner and make the exception explicit. Test every output
language independently; success in one language is not evidence for another.

## Voice-medium behaviour

Concise is not curt. Normally answer the caller's question, then add at most
one useful next step. Ask one question at a time. Avoid unnecessary repetition,
internal process narration, and long menus of possibilities.

Calibrate proactivity to the caller's intent and the cost of being wrong.
Handle interruptions naturally and recover from silence without restarting the
whole exchange. Make dates, numbers, names, and URLs speakable; confirm a
critical value only when acting on a misheard value has meaningful cost.

Start an instruction with one short rule and one representative example. For
example, a language asset may show a natural spoken URL, while the workflow
owner retains the condition for when that URL is offered. Add branches or more
examples only after observed drift.

## Evidence-driven iteration and versioning

Change behaviour because of a transcript, an evaluation result, an explicit
requirement, or a safety or compliance need. Begin with the smallest coherent
rule plus one representative example, then expand only when evidence shows the
model still drifts.

During one continuous pre-test iteration, revise the same asset. Once a tested
baseline exists, create a new version for a material behavioural change or a
genuine alternative that must remain comparable or reversible. A newly
extracted layer starts at `v.1`; extracting it does not force sibling assets to
increment. Use names that reveal the responsibility and meaningful variant,
not version numbers alone.

## Linkage, live diff, and publication

Use `callva agents get` to inspect an agent's binding and `callva assets list`
or `callva assets get` to resolve the prompt inventory and live content.
Publication uses `callva assets create` or `callva assets update`; changing the
binding uses `callva agents update`. Run `callva help` for their arguments.

An agent references prompt assets from its `prompt` field using
`{{prompt:<asset_id>}}`. Read the agent, resolve every reference, and inspect
the compiled order before deciding which asset owns a change. One asset may be
shared by several agents, so editing it changes every consumer. Repointing an
agent to a different asset is an agent update, not an asset edit.

Author substantial content in a local file. Before publication, fetch the live
asset and diff it against the local base and intended next state. If live
content has changes absent from the local base, stop and reconcile the
divergence rather than overwriting it.

Publish through the CallVA asset operation, then re-read the live asset and the
agent binding. Use the create path for a new layer or version and the update
path for a deliberate revision in place. The exact command forms and file
options belong to `callva help`.

## Placement checklist

- What single semantic rule or fact is being placed?
- Which asset owns its condition, exceptions, and evaluation?
- Is the split justified by responsibility or an independent boundary?
- Is machine-contract detail already owned by a function declaration?
- Is this stable instruction, language surface, or runtime data?
- Is the asset self-contained without prose-location references?
- Does it describe only tools and handoffs available to this agent?

## Final validation checklist

- Compare the local source with the live asset; reconcile any unexpected diff.
- Identify and verify every shared consumer of an edited asset.
- Compile in runtime order and verify references, variables, and explicit data
  states.
- Check that each semantic rule has one owner and examples introduce no hidden
  policy.
- Run the target scenario and one adjacent regression scenario.
- Test each output language independently for meaning and spoken surface.
- After publication, re-read the live asset and agent binding and confirm
  runtime results before reporting external actions as complete.
