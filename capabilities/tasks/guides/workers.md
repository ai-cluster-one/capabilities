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
- `limits` bounds the worker: `attempts` raises that did work before a task is parked, `lease_seconds` how long a claim is held (by default the profile's timeout plus ten minutes), `hold_seconds_on_exhaustion` how long a task rests after a turn that never got to the work, and `idle_failure_seconds`, under which a failed turn that wrote nothing counts as one that never started.
- `park_hint` is one sentence the project adds to the handback when a task is parked at the `attempts` ceiling, and only then: it is what a person about to read a task the work kept failing on should check first.

A string in the front matter may carry `${VAR}`, read from the environment and then from the project's `.env` and `.env.local`; a name nothing answers for is refused. Machine-local paths belong there rather than in a versioned file.

### What a turn may write

`writes` declares the worker's authority in three scopes: `held`, the task its raise holds; `other`, any other task; and `new`, a task it creates. Each scope maps a field - status, type, assignee, pickup, the metadata keys it may write over or remove, the tags it may remove - to the values it may write, and `new` names status, type, assignee and pickup only.

Two rules read it, and only two. A field a scope does not list may not be written in that scope. `"*"` means any value, emptying the field included. Adding is never configured and always allowed, on every task: an entry on the trail, a tag, a metadata key not already there. Title, objective and description are written on the held task and on no other.

A worker with no `writes` is held to a default fence: on its own task it lands any status but `in_progress` and never changes the type, on every other task it only adds, and what it creates lands in `draft`. A declared `writes` replaces that fence whole, so it has to name everything the role needs: `held.status` lists every status the role ends its task on, and a `new` that allows no type refuses every task the worker tries to create, since a task always has a type. The exact default and the refusal rules are `tasks help` WORKER SCOPE.

What a raise may write is fixed when it is claimed. Editing a worker file changes the next claim, never the turn already running, and a worker whose raise has ended may write no field on any task, its own former task included.

### The body

The body is the role: what this worker is and how it carries the work, and what done means for it. It is the project's one instruction inside a fixed frame the project cannot edit. The frame already tells the turn which task it holds, that no person is present and nothing in the task is approval, how to write the trail, what the store will let it write, and how to stop - in `waiting`, with a person named. The body therefore says what only this project knows: what the work is, which judgement it needs, and where this role ends.

The task and its trail reach the turn as quoted data, between markers drawn for that prompt alone. Instructions come only from the frame, the body and the routines it names; `tasks help` WORKERS states the frame's order.

## Shipped workers and project files

The capability ships two workers. `default` takes every `todo` task of a type no project worker file names in `takes` or `on_request`, so a project with no workers of its own still has its queue worked; its body treats each task as a request from the person responsible for it. `supervisor` ships switched off; `tasks guide loops` explains its role.

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

`tasks doctor`, once it has proved the store, reads every worker this project can run and refuses with exit 6, naming every problem in one pass, when any of them is wrong. It refuses a file it cannot read, a key nothing reads, an enabled worker that takes nothing, an enabled worker with an empty body, a profile the library refuses, a profile knob carrying `${`, a profile with no timeout when the worker sets no `lease_seconds`, and a profile or routine the worker names that is not there. It refuses two enabled workers whose filters could select the same task, because a task is taken by exactly one enabled worker; filters are kept apart by status, type or assignee, or by tag sets where neither contains the other.

Two judgements reach past `doctor` into `run`. A worker naming a missing profile or routine has nothing whole to give a turn, so `run` parks the task it takes, saying what is missing, without starting a turn and without spending an attempt. While any project worker file's types cannot be read at all, `default` takes nothing, because it cannot know which types are someone else's.

When it passes, `doctor` lists each worker with its source, what it shadows, whether it is enabled, its description, what it takes, its `writes`, its profile, its routines and its limits. `tasks run <worker>` without `--apply` says which task a claim would take and writes nothing.

## Changing a worker while the service runs

Run `tasks service reload` right after editing a worker file, a profile a worker names, or the service settings. The running daemon publishes a fingerprint of the declaration it loaded, and `tasks service doctor` fails while that differs from the files on disk, so whatever watches the service sees that the daemon is not running the declaration on disk. `reload` validates the files first and refuses what does not load, then hands the daemon the declaration on disk and waits until it reports the new one; running turns are never touched. What a reload cannot change, such as the connection or the environment turns start with, needs `stop` and `start`, and `tasks help` SERVICE lists it.
