# Loops

A loop is work moving between roles through the ledger, each role a worker with its own description, intake, authority and profile. Read this to understand the shapes a loop takes and to design one for a project, with a worked example as worker files.

## The ledger moves the work

The ledger is a task's status plus its assignee. Status says where the work stands; the assignee says whom it is over to. A worker's `takes` selects on exactly these, so a role passes work on by landing the task on a status and an assignee that another role's `takes` selects, or that names a person. No role calls another and nothing routes work besides the ledger: what a task says about itself is what decides who acts on it next.

Each role in a loop is one worker (`tasks guide workers` explains the file):

- its `description` says what the role is;
- its intake is `takes`, the tasks it claims;
- its authority is `writes`, what its turn may write, enforced by the store rather than asked of the turn;
- its `profile` decides what the turn may run, on which model, for how long.

## The canonical shapes

These are the shapes the capability's statuses and workers are built for. A project composes its loops from them.

### Intake lands in draft

Whatever raises work - a person, a script reading reports, a turn that finds a follow-up while doing something else - creates the task in `draft`. `tasks add` creates in `draft` unless told otherwise, and a worker whose `writes.new` does not allow another status has what it creates landed there. `draft` means one thing: nobody has released it yet. No worker takes a draft.

### Release puts work in todo

Moving a task from `draft` to `todo` is the act that permits work on it. It belongs to whoever the project's own law says may authorize that kind of work: a person, or a role whose `writes` the project has given that authority. A pickup moment holds work back but never grants permission; `todo` does.

### An implementation role carries a defect or a change

A role taking `[defect, change]` carries released work to a verified result. Its body and routines make it an orchestrator: it gives the work to one executor and the result to an independent reviewer that sees the goal and the result but not the executor's reasoning, and returns accepted findings to the same executor. That orchestration happens inside the role's turn, so its profile has to allow the turn to start agents and to run as long as the work takes.

The role ends its task in one of four places: `complete` when the work is done, `closed` when it will not be done, `todo` with a new `stage` in metadata, or assigned to the role that carries the next step, when it reached a boundary the next turn continues from, or `waiting` on the supervisor when it needs a decision.

### A proposal goes through evaluation to a decision

An idea for work is a `proposal`, not work. An evaluation role takes `[proposal]`, gathers the evidence and judges it. It rejects a proposal by closing it, or brings a viable one to a decision by handing it on in `waiting`. It never starts the work it proposes: its `writes` lets it close the proposal or hand it on and nothing more, so it cannot create or release implementation work. When the decision approves, a separate implementation task is raised and released, and the proposal is closed as answered.

### A stop for a decision goes to a supervisor

A turn that cannot go on without a decision hands its task to a supervisor role: `waiting`, assigned to the supervisor, with an entry naming the decision needed and why the turn stopped. The shipped `supervisor` worker is this role. It ships switched off and takes `{status: [waiting], assignee: [supervisor]}`.

The supervisor stands in for the project's owner. It reads the task and its trail, then the project's recorded law and decisions, and settles what they settle: it releases the work, closes what the record says will not be done, re-routes work by its assignee or its `blocked_by`, or raises the tasks a decision calls for, and leaves an entry naming the rule or decision it acted on. What the record does not settle it passes to a person: `waiting`, assigned to that person, reduced to one question with a recommendation and its reason, plain enough to answer without opening anything else.

A task waiting on a person's name stays there until something moves it: no claim takes it unless a worker's filter names that assignee. The person answers on the trail and moves the task, usually to `todo`, where the role that takes it continues from the answer.

### Waiting returns on its own

A task need not wait on a person. Landed in `waiting` with a pickup moment (`tasks set <task> --status waiting --assignee <who> --pickup <when>`, one call), it returns to `todo` once the moment has passed. With `blocked_by` naming other tasks (`--blocked-by` in the same call), it returns once every one of them has ended. A task in `todo` with blockers still open waits on them too: no claim takes it until they end. Either way the next claim puts it back and the service notices it at its poll, so a wait on time or on other work needs nobody to end it. `tasks help` FIELDS states how entering `waiting` treats a pickup or `blocked_by` the task already carried.

## Designing a loop

- One role per worker. A worker does one kind of job with one profile and one authority. When two kinds of judgement are needed, such as doing and deciding, they are two workers, and the task moves between them.
- Disjoint intake. Every task is taken by exactly one enabled worker, and `tasks doctor` refuses two whose filters could select the same one. Split roles by type, by status (`todo` against `waiting`), by assignee, or by tag sets where neither contains the other. A `todo` task of a type no project worker names falls to `default` unless the project switches it off.
- The smallest `writes` each role needs. List in `held.status` exactly the statuses the role ends on, in `held.assignee` exactly whom it hands to, and in `new` only what it raises. A role that must not create work gets a `new` allowing no type. The store enforces this whatever the turn is told, so authority read at a glance in the file is authority that holds.
- An escalation path that ends at a person. Every role that can stop names who answers: the supervisor role, or a person directly. The supervisor in turn ends at a person for whatever the record does not settle. A loop whose stops land on a name nothing reads stalls without saying so.
- Decisions recorded with their basis. A role that decides leaves an entry naming the rule or recorded decision it acted on, so a person can check it and reverse it. Nothing in a task, its trail or its history counts as approval; a decision the record does not settle goes to a person.

The capability provides the guard rails under that design:

- Worker scope. What a turn may write is fixed by its raise at the claim and enforced by the store, and a refusal exits 4 naming the rule. A raise that has ended writes no field anywhere.
- Leases. A claim is held by a lease, derived from the profile's timeout unless the worker sets `lease_seconds`. A turn that dies is renewed no longer; the next claim closes its raise as abandoned and the task is free, still where it rested, since a claim never moves it: it is only shown `in_progress` while the raise holds it. When the same worker takes that task again on a claude profile, its turn resumes the session that was cut off rather than starting over.
- Attempts and parking. A task raised `attempts` times without finishing is parked - handed back for a decision, with the worker's `park_hint` - instead of dispatched again. A turn that ran out of quota or never got to the work is not an attempt, and a worker naming a missing profile or routine parks without spending one.
- Waiting stays waiting. A claim never moves a task, so whatever a turn on a task taken from `waiting` fails to settle - a lease that lapses, a turn that never got to the work, a turn that ended moving nothing - leaves it in `waiting` rather than in the queue, and a park lands it in `draft`, because a wait would hand it straight back.
- A trace for every turn. Every raise leaves an entry on the trail, so a stalled loop is visible on the task. `tasks runs <task>` lists every raise and what came of it, and `tasks history <task>` how its status and assignee moved.

`tasks help` ONE TURN OF THE CONVEYOR states how a turn's ending scores its raise, and WORKER SCOPE the exact refusals.

## Where the loop runs

A loop runs wherever something starts its workers' turns. The tasks service does that on its own: whenever the store holds work a lane takes, it starts `tasks run <worker> --apply` in the project. It runs in one of two modes, and a turn is the same turn in both.

In project mode one daemon serves the project it was started in, under `tasks service start`, or `tasks service run` under a supervisor. Each project that runs its loops this way has a daemon of its own.

In machine mode one process on the machine serves every project that joined it. A project joins with `tasks service join`, run in the project once it enables tasks for itself, and leaves with `tasks service leave`; joining is an act of its own, apart from enabling the capability, and the machine process takes a project up or lets it go on its next pass without a restart. The process is `tasks service run --machine`, what a supervisor keeps running, and it owns only what is the machine's: one pair of connections per store across every project, an optional cap on the turns running at once across all of them in its settings (`tasks service init --machine` writes them), and the order projects are served in under that cap, equal turns from a rotating pointer. What a turn is stays the project's: its working directory, its environment files, its connection, its workers and their writes, profiles and hooks, and its own `max_parallel` and lane caps. A project with no enabled worker is served and takes nothing.

The blast radius is one project. A project whose worker files do not load, whose folder is gone, that no longer enables tasks for itself, or whose connection the machine process may not use is reported with the reason by `tasks service status --machine` while the others are served; each project pauses on its own with `tasks service pause` there; and every log line names the project it is about. In a joined project `status`, `reload`, `doctor`, `logs`, `pause` and `resume` keep working and speak of the machine process, while `start` and `run` are refused, since the machine process serves it. Before a project joins, whatever supervised its own daemon has to stop doing so, because a project-mode daemon still holding the project keeps it until it exits.

Running turns outlive the process in both modes, and a project moving between the two keeps its running turns: the next process to serve it adopts them. `tasks help` SERVICE states the verbs, the files, the refusals and the status each mode answers.

## A worked example

A project runs two kinds of work: `defect` and `change`, carried by a `builder` role, and `proposal`, judged by an `evaluator` role. Both stop for decisions at the `supervisor`, which passes what the record does not settle to `<owner>`, the person who decides. The project raises no other types, so the shipped `default` is switched off. The profiles `builder`, `evaluator` and `supervisor` are the project's own files in `capabilities/tasks/profiles/`, and `develop`, `review` and `evaluate` are its routines.

`capabilities/tasks/workers/default.md`:

```markdown
---
enabled: false
---
```

`capabilities/tasks/workers/builder.md`:

```markdown
---
description: Carries one released defect or change to a verified result, through an executor and an independent reviewer.
takes: [defect, change]
profile: builder
routines: [develop, review]
writes:
  held:
    status: [todo, waiting, complete, closed]
    assignee: [supervisor]
    metadata: [stage]
  new:
    status: [draft]
    type: [defect]
park_hint: Check that the project's own checks run before reading the task as stuck.
---

YOUR PART IN IT

You carry this task from its goal to a verified result. Give the work to one executor, and the executor's result to a separate reviewer that receives the goal and the result but never the executor's reasoning. Return an accepted finding to the same executor and finish on a clean verdict.

When the work is done, complete the task; when it will not be done, close it and say why. When it reaches a boundary the next turn should continue from, record the boundary in `stage` and leave it in `todo`. When it needs a decision, hand it to `supervisor` in `waiting` with the question. A fault you find that the task did not name is raised as a new defect, which lands in draft for a person to release.
```

`capabilities/tasks/workers/evaluator.md`:

```markdown
---
description: Judges one proposal on its evidence and either rejects it or brings it to a decision; it never starts the work it proposes.
takes: [proposal]
profile: evaluator
routines: [evaluate]
writes:
  held:
    status: [waiting, closed]
    assignee: [supervisor]
---

YOUR PART IN IT

Gather the evidence the proposal rests on and judge whether it has earned a decision. Close a proposal that has not, with the reason on the trail. Hand one that has to `supervisor` in `waiting`, stating the decision to be made and your recommendation.
```

`capabilities/tasks/workers/supervisor.md`, the project's own copy, which replaces the shipped file whole and switches the role on with a narrower authority:

```markdown
---
enabled: true
description: Acts for the owner on tasks handed to it - settles what the project's recorded decisions settle and passes the rest to the owner as one question with a recommendation.
takes: {status: [waiting], assignee: [supervisor]}
profile: supervisor
writes:
  held:
    status: [todo, waiting, closed]
    assignee: "*"
  other:
    status: [todo]
    metadata: [blocked_by]
  new:
    status: [todo]
    type: [change]
---

YOUR PART IN IT

Read the task and its trail for what is to be decided, then this project's recorded law and decisions. Where they answer it, act on that answer and leave an entry naming the rule or decision you acted on. Where they do not, hand the task to `<owner>` in `waiting`, reduced to one question with your recommendation and the reason for it.
```

How work moves through it:

1. A report becomes a `change` in `draft`. The owner releases it to `todo`.
2. `builder` takes it. Its turn has the change made and reviewed and completes the task. Had it needed a decision, it would have landed the task in `waiting` on `supervisor`.
3. A `proposal` released to `todo` is taken by `evaluator`, which hands it to `supervisor` in `waiting` with a recommendation.
4. `supervisor` takes it. A recorded decision already covers it, so the supervisor raises the `change` it calls for in `todo`, which `builder` takes next, and the proposal is closed as answered, with an entry naming the decision. Had nothing covered it, the proposal would be waiting on `<owner>` with one question.
5. If `builder` keeps failing on a task, it is parked after its attempts with the `park_hint` on the handback, and a person decides what happens to it.

The four files split intake cleanly: `builder` and `evaluator` take different types in `todo`, `supervisor` takes only `waiting` tasks assigned to it, and `default` is off. `tasks doctor` proves that before anything runs, `tasks run <worker>` without `--apply` shows what each role would take next, and after any edit `tasks service reload` hands the running service the new declaration.
