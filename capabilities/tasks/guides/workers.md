# Workers

A worker is one role in a project's conveyor: one lane that claims the tasks it is declared to take and runs one agent turn on each. Read this to write a worker file, to change or switch off a shipped worker, and to know what `doctor` will say about one.

## What a worker is

A worker is a role, not a process. It is one file in the project's capability envelope, `capabilities/tasks/workers/<name>.md`, and it is run by its name, the file's name without `.md`.

One turn of a worker is `tasks run <worker> --apply`: claim the next task the worker's `takes` selects, hold it under a lease, start one headless agent turn on it with the worker's profile, and settle the raise from where the turn left the task. The turn moves the task's status and writes its trail itself, through the same verbs anyone uses; `run` never reads the turn's answer, so a turn that reports success and moved nothing is not believed. The service runs every enabled worker that takes something as a lane, starting a turn whenever the store holds work that worker takes; without the service, anything may run `tasks run <worker> --apply` on its own schedule, and a worker nothing runs never runs.

Because the role is the unit, a worker does one kind of job. Work that needs several kinds - doing it, judging it, deciding about it - moves between several workers, and `tasks guide loops` is about that movement.

## Anatomy of a worker file

A worker file is Markdown. Its YAML front matter is the worker's settings and its body is the role. The shape is this capability's contract and the content is the project's; any key the contract does not name is refused rather than ignored. `tasks help` WORKERS states every key with its default; this section says what each one is for.

- `enabled` switches the worker on or off, and defaults to on.
- `description` is one sentence saying what the worker is and does. `doctor` shows it beside the worker, and it opens the frame every turn of the worker receives, so it is the first thing the agent reads about its role.
- `profile` names the profile the turn runs on. The profile, not the worker, decides which harness runs, on which model, with which tools, at what effort and for how long; a worker names no harness. A project keeps its own profiles in `capabilities/tasks/profiles/`, searched before the machine's and the library's.
- `takes` says which tasks the worker claims, in one of three forms. A list of types, `takes: [defect, change]`, takes tasks of those types in `todo`, in the order listed. One filter map, `takes: {status: [waiting], assignee: [<role>]}`, takes what it describes. A list of filter maps takes a task matching any of them, tried in order. A filter names any of `status`, `type`, `assignee` and `tags`, each a list of allowed values; one naming no status takes `todo`, and only `todo` and `waiting` can be taken. The top-level `tags` and `assignee` narrow every filter at once.
- `on_request` lists types the worker runs only when a task in `todo` is named to it with `--key`, never by taking the next one.
- `writes` is what the worker's turn may write, described below.
- `routines` names the project's procedures, each found at `routines/<name>.md`; the first is the procedure for the work and the rest apply at the moments they name. A worker naming none leaves the turn to choose one by the routines' descriptions, or to work from the project's doctrine, and to say on the trail what it chose.
- `limits` bounds the worker: `attempts` raises that did work before a task is parked, `lease_seconds` the longest a claim is held (by default the profile's timeout plus ten minutes), within which a running turn holds its task by a short lease with one writer - the service for a turn it started, the `run` process itself for a run by hand - renewed while the turn lives, `cool_down_seconds` how long a task rests after a raise that left it where it was - failed, or abandoned - before any claim takes it again (60 by default; the older `hold_seconds_on_exhaustion` is read as it, and `doctor` names it), and `idle_failure_seconds`, under which a failed turn that wrote nothing counts as one that never started.
- `park_hint` is one sentence the project adds to the handback when a task is parked at the `attempts` ceiling, and only then: it is what a person about to read a task the work kept failing on should check first.
- `hooks` names the project's own commands that run around a turn, described below.

A string in the front matter may carry `${VAR}`, read from the environment and then from the project's `.env` and `.env.local`; a name nothing answers for is refused. Machine-local paths belong there rather than in a versioned file.

### What a turn may write

`writes` declares the worker's authority in three scopes: `held`, the task its raise holds; `other`, any other task; and `new`, a task it creates. Each scope maps a field - status, type, assignee, pickup, the metadata keys it may write over or remove, the tags it may remove - to the values it may write, and `new` names status, type, assignee and pickup only. A task's `blocked_by` is answered for under `metadata`, by naming `blocked_by` there, as when it was a metadata key.

Two rules read it, and only two. A field a scope does not list may not be written in that scope. `"*"` means any value, emptying the field included. Adding is never configured and always allowed, on every task: an entry on the trail, a tag, a metadata key not already there, blockers on a task that lists none. Title, objective and description are written on the held task and on no other.

A worker with no `writes` is held to a default fence: on its own task it lands any status but `in_progress` and never changes the type, on every other task it only adds, and what it creates lands in `draft`. A declared `writes` replaces that fence whole, so it has to name everything the role needs: `held.status` lists every status the role ends its task on, and a `new` that allows no type refuses every task the worker tries to create, since a task always has a type. The exact default and the refusal rules are `tasks help` WORKER SCOPE.

What a raise may write is fixed when it is claimed. Editing a worker file changes the next claim, never the turn already running, and a worker whose raise has ended may write no field on any task, its own former task included.

### The body

The body is the role: what this worker is and how it carries the work, and what done means for it. It is the project's one instruction inside a fixed frame the project cannot edit. The frame already tells the turn which task it holds, that no person is present and nothing in the task is approval, how to write the trail, what the store will let it write, and how to stop - in `waiting`, with a person named. The body therefore says what only this project knows: what the work is, which judgement it needs, and where this role ends.

The task and its trail reach the turn as quoted data, between markers drawn for that prompt alone. Instructions come only from the frame, the body and the routines it names; `tasks help` WORKERS states the frame's order.

### Hooks

A hook lets a project gate or follow a worker's work with a check of its own, without `tasks` knowing what the check is. A worker declares at most two, one at each handler point of the conveyor - `before` a claim and `after` a raise - each one command line run from the project root without a shell, with the task as JSON on stdin and its id, key, type and assignee, the worker's name and the project root in the environment.

Both answer in one vocabulary, by their exit: 0 is go; 75 is hold, until the moment on its last stdout line when it prints one; 76 is escalate, its last stdout line the reason. Anything else - another exit, a timeout, a command that cannot start - is a hold with no moment.

`before` is asked about each task a claim would take, before anything is written. Go lets the claim take it. Hold writes nothing to the task - no pickup, no history, not even `updated_at` - and the next task is asked about; the service remembers the moment, or its next poll, and starts no turn for that task before then. Escalate sends the task along the project's escalation chain at once. Use it for a condition the store cannot see - a shared resource that is busy, a window the work must wait for, a precondition another system answers - so a task that cannot start now never opens a raise, never spends an attempt and never starts a turn only to stop, and for a condition it can tell will never clear, so the task reaches someone who can act.

`after` is asked once a raise is settled, with the outcome, the status the task landed in, the raise's detail and metrics, and how the turn ended: the harness's exit, its failure kind and what it last said. Go changes nothing. Hold holds the worker's lane - no claim of that worker, the service's or by hand, takes a task until the moment, or for its cool-down without one - and is recorded on the raise, so a service restart keeps it. Escalate sends the task along the chain, unless it already ended. Use it to follow the work - notify, record, release what `before` checked - and to stop a lane on a condition that will hit every task alike.

The capability ships after-raise handlers of its own for the harnesses' accounts. A raise that ended on Claude Code's or Codex's usage limit holds the lane until the reset the harness names - the five-hour and the weekly limits alike - and one naming no reset, or a harness not logged in, for 30 minutes; the raise is no attempt at the work, and no task of the lane is escalated for the time the lane stood held.

```markdown
---
takes: [change]
profile: implementation
hooks:
  before: scripts/resource-free.py
  after: scripts/notify.sh
---
```

Here `scripts/resource-free.py` reads the task from stdin, exits 0 when the resource the work needs is free, and otherwise prints the moment to try again, `2026-01-05T09:00:00+00:00`, and exits 75. `tasks help` HOOKS states the whole contract: the environment, the timeouts, the shipped handlers and where every held-back task and held lane is reported.

## When a task cannot move: escalation

A worker can hold a task back for ever - a `before` hook that keeps answering "not now", a turn that keeps ending without moving it - and without a rule above the workers such a task stands still with nobody named to move it. A project names that rule in `capabilities/tasks/conveyor.toml`, beside its workers:

```toml
escalate_to = ["supervisor", "owner"]
stall_after = "6h"
```

`escalate_to` is the chain a stuck task goes along, in order, and its last name is a person - a name no worker file of the project has. Every name before it is a worker that is switched on and takes the waiting tasks assigned to it, as the supervisor does. `stall_after` is how long a task may be held back before it is escalated, 6h when unsaid.

The claim decides it, for the tasks the claiming worker would take. A task whose raises in place - the raises since it last changed status or assignee, apart from those that never got to the work - have reached the worker's `limits.attempts` is escalated instead of claimed, before its `before` hook is asked. A task its `before` hook has held back for longer than `stall_after` is escalated instead of held again; the clock starts at the first hold in its place, so time spent waiting its turn never counts, and that first hold is the one thing the claim records, as a row among the task's raises that is no raise. A hook answering escalate sends the task on at once. Escalating moves it to `waiting` on the next name in the chain, with one trail entry by `tasks:scan` saying why, since when it stood where it was, and how many raises it had there. If that name cannot move it either, the next escalation goes on, ending at the person. A task assigned to a person is never escalated this way.

Waiting is not refusal: a task behind other work in a busy lane is never escalated, and neither is one in a paused lane, whose wait `tasks service status` shows instead. With a chain, `attempts` counts raises in place, so a pipeline whose every stage is a raise that moves the task keeps its count low and 3 is enough; without a chain nothing here applies and every worker runs as it did. `tasks help` ESCALATION states the contract.

Escalating lets go of the task's `blocked_by` as it lets go of its pickup, and the entry names the blockers that were still open: a task with open blockers is taken by no claim, so kept, they would hide the task from the name it went to. Whoever takes it sets a blocker again when it is still wanted.

### Dead ends

Some tasks cannot move however long anyone waits, and the configuration alone says so: a task on a worker's name that no enabled worker takes in its status, type and tags - the supervisor's name in `todo` when the supervisor takes only waiting tasks, or a worker switched off or broken after tasks were left on it; a task whose worker names a profile or routine that is not there; an open task whose `blocked_by` names no task, names a draft, or leads back to it. A run's first claim, of any worker of the project, scans the project's open tasks for these and escalates each at once, with the rule as the reason, no ceiling waited out; without a chain it names them under `dead_ends` and moves nothing. A task on a person or on nobody is never a dead end, and a waiting task whose wait ends on its own, by its pickup or its blockers, counts as taken by whoever takes it in `todo`.

Writes do not make them in the first place. `add`, `set` and `release` refuse with exit 4 a write that would leave a task in `todo` or `waiting` on a worker's name nobody takes there, `add`, `set` and the deprecated `meta set` refuse a `blocked_by` naming no task, the task itself or a cycle, and `in_progress` is never written: it is shown while an open raise holds the task. A turn refused this way stops on a named person, as its frame tells it. So a worker's `writes.held.assignee` should name only workers that take the task where the worker may land it, or people: `doctor` lists every other place as `lands_untaken` on the worker and warns of it.

## Shipped workers and project files

The capability ships two workers. `default` takes every `todo` task of a type no project worker file names in `takes` or `on_request`, so a project with no workers of its own still has its queue worked; its body treats each task as an assignment given within the project's own authority, to act on inside what the project's doctrine lets an unattended turn do. `supervisor` ships switched off; `tasks guide loops` explains its role.

A project file with the name of a shipped worker replaces the shipped file whole. Nothing is merged: to change one setting of a shipped worker, copy the whole shipped file - `doctor` names its path - into the project's `workers/` and edit it there. `doctor` reports the project file as the worker's source and names the shipped file it shadows. A project switches the supervisor on this way, with its own `workers/supervisor.md`.

## Switching a worker off

Set `enabled: false` in its file. A switched-off worker never claims, is not a lane, and `tasks run` refuses it with exit 4. The front matter alone is a whole file for a worker that is off, so `workers/default.md` holding only

```markdown
---
enabled: false
---
```

switches the shipped catch-all off.

A worker that is off still owns the types it names: `default` never takes them, so switching a worker off, or breaking its file, never hands its work to the catch-all. Removing the file is what gives those types back.

## How doctor judges workers

`tasks doctor`, once it has proved the store, reads every worker this project can run and refuses with exit 6, naming every problem in one pass, when any of them is wrong. It refuses a file it cannot read, a key nothing reads, an enabled worker that takes nothing, an enabled worker with an empty body, a profile the library refuses, a profile knob carrying `${`, a profile with no timeout when the worker sets no `lease_seconds`, and a profile or routine the worker names that is not there. It refuses two enabled workers whose filters could select the same task, because a task is taken by exactly one enabled worker; filters are kept apart by status, type or assignee, or by tag sets where neither contains the other. It refuses an escalation chain whose last name is a worker, that names before its end a name no worker here has, or a worker that is off or takes no waiting task assigned to it, and it warns a project with enabled workers and no chain, and of every place a worker may land the task it holds where no enabled worker takes it.

Two judgements reach past `doctor` into `run`. A worker naming a missing profile or routine has nothing whole to give a turn, so `run` parks the task it takes, saying what is missing, without starting a turn and without spending an attempt. While any project worker file's types cannot be read at all, `default` takes nothing, because it cannot know which types are someone else's.

When it passes, `doctor` lists each worker with its source, what it shadows, whether it is enabled, its description, what it takes, its `writes`, its profile, its routines, its limits and its hooks. `tasks run <worker>` without `--apply` says which task a claim would take and writes nothing; for a worker with a `before` hook it asks the hook and names what the hook would hold back, and for a held lane it names the hold. `doctor` also names every key a project still sets that is read as another now, such as `hold_seconds_on_exhaustion` or the service's `retry_delay_seconds`.

## Changing a worker while the service runs

Run `tasks service reload` right after editing a worker file, a profile a worker names, or the service settings. The running daemon publishes a fingerprint of the declaration it loaded, and `tasks service doctor` fails while that differs from the files on disk, so whatever watches the service sees that the daemon is not running the declaration on disk. `reload` validates the files first and refuses what does not load, then hands the daemon the declaration on disk and waits until it reports the new one; running turns are never touched. What a reload cannot change, such as the connection or the environment turns start with, needs `stop` and `start`, and `tasks help` SERVICE lists it. In a project joined to the machine service the same `tasks service reload` reaches the machine process, which takes up this project's files and leaves the other projects as they are.

Stopping or restarting the service leaves running turns running: each settles its own task, and the next daemon to serve the project adopts them. The service is the one writer of its turns' leases, so a turn no daemon adopts before its lease runs out loses its raise and ends itself. `tasks service stop --end-turns` is the one act that ends them at once, after `shutdown_grace_seconds`, settling what they held.

## Pausing the service

Run `tasks service pause` to hold the conveyor without stopping the daemon, and `tasks service resume` to let it go again. While the pause holds, the daemon starts no new turn; turns already running are not touched and finish on their own, and the daemon goes on polling and listening, so it is ready the moment the pause lifts. Name workers to pause or resume only those lanes, `tasks service pause --reason "<why>" <worker>...`; with no name the verb covers every lane. A name that is not a lane of the service is refused.

The pause is runtime state kept beside the daemon's pid, not a setting in `service/config.toml`. It needs no reload, moves no fingerprint, holds across a restart of the daemon, and can be set or lifted while no daemon runs. `tasks service status` shows which lanes it holds, its reason, when it was set and by whom, and for each held lane its oldest due task and how long that has waited, since a paused lane escalates nothing; the service log records every pause and resume. `tasks service doctor` reports it and still answers ok, so a supervisor that restarts a service on a failing probe leaves a paused one running. A turn started by hand with `tasks run <worker> --apply` is not held by the pause.
