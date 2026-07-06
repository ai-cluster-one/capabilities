# Windmill — scheduled orchestration

How a consuming project uses Windmill as the scheduling and dispatch layer around agent work.

Windmill owns *when* a run happens and the run record that proves it happened. The domain work lands in the systems the script drives: a tracker, a source system, or the agent box. Keep scripts thin: deterministic selection, transport, timeout/budget boundaries, and enough result parsing to record the run. Do not copy a routine's judgment into TypeScript.

## Producer Shapes

Two producer shapes are common:

- **Judgment producer.** The script boots the reasoning agent to run a producer routine over a source where deciding what becomes work requires reading or classification. The sweep is not itself a tracker task; the Windmill job is the run record. The output is the registered work items.
- **Mechanical producer.** The script diffs a structured source against the system of record and tracker, then enqueues one job per genuinely new item, or a homogeneous bundle only when the downstream processor has an independent idempotency/validation guard for each item. No model runs in the producer.

In both shapes, the script names the processor by role and embeds or attaches the substrate. The worker resolves the processor from the project context.

## Engine Shape

A dispatch engine is also a scheduled script: it selects ripe work, confirms no live worker already owns the same task, assembles the task context, and boots one reasoning worker per item. Recovery/resume/fresh ordering is a project policy, but the invariant is general: the engine dispatches exactly one worker for exactly one work item at a time.

Wake signals belong in the tracker or the source system, not a repo-local cursor. The engine reads live state every tick.

## SSH To Agent Box

When the project has a deployed agent box, Windmill should hold only the SSH key needed to reach it. The box holds service credentials and installed capability CLIs. Scripts connect over SSH, resolve the live container, and run box commands through `docker exec`.

The script-authoring guide covers the SSH helper shape and quoting rules. This guide covers the orchestration boundary: Windmill schedules and dispatches; the box and project routines perform the work.
