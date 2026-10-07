# What tasks is for

`tasks` is a work queue for agents: an agent's own technical queue of jobs, there so that an agent can work autonomously by taking the work set in front of it. Read this before adopting it, to know what it carries and what it leaves to other tools.

## A job queue, not a task board

The capability is called `tasks`, but what it holds are jobs. Each one is a unit of work an agent can take, carry under a lease, and leave somewhere definite, with a record of what it did. It is not a board for people to plan on, and it does not try to replace a project-management tool where people rank, discuss and schedule their work. A project that has one keeps it; work comes here when an agent is meant to take it.

What `tasks` answers is the question a chat log cannot: what is open, what may be picked up now, and what already happened. Everything in it serves that question.

## The record: a goal and its trail

A task is a goal and the append-only trail of what was done about it, and those two are the record.

The objective says why the task exists and what counts as done; it does not go stale while the task lives. The description is what was seen when the task was raised, a snapshot that a worker re-checks before acting on it. The trail is a list of entries, each a moment, a sentence and who wrote it, saying what actually happened: a message sent, a check run, a blocker hit, a decision made and why. Nothing edits or removes an entry, so a wrong one is corrected by a later one, and the record of having been wrong stays.

The trail is how a later turn continues work an earlier one started, and how a person checks what an agent did without asking it. A task that was picked up and left no entry reads exactly like one never picked up, which is why the conveyor writes an entry for any turn that wrote none.

## What it leaves out

Three things a task board carries are outside its scope on purpose.

- Ranking. There is no priority field. What runs first is decided by which worker takes what, described below, not by a number someone has to keep current.
- Per-project status vocabularies. The statuses are a fixed set and each means one thing: `draft` nobody has released yet, `todo` released and takeable, `in_progress` held by a claim, `waiting` over to the name it carries, `complete` done, `closed` over without being done. A project's own vocabulary lives in its task types, which are free text and name its pipelines.
- A large backlog kept so that nothing is lost. A draft is not out of the way: it reads as work somebody has yet to release. Ideas and long-range plans belong where people plan; a task is raised here when there is work for an agent to do.

## Where order comes from

Order is the product of three things, and nothing else.

- What workers take. Each worker declares which tasks it claims as filters over status, type, assignee and tags, tried in the order it lists them and each filter's types in the order it names them, so the order of a worker's `takes` is its whole arbitration. Among the tasks that match, the soonest pickup goes first and then the oldest task.
- `pickup`, the moment before which a task is not raised. It holds work back; it never grants permission, which is what `todo` does.
- `blocked_by`, the task's own field: the tasks it waits on, given by key in its project or by id in any and kept by id. While any of them has not ended no claim takes the task, whatever its open status; a waiting one returns to `todo` on its own once every one of them has ended. `tasks set <task> --blocked-by <task>[,<task>...]` writes it and refuses a name that is no task, the task itself, or a cycle.

Tags filter: a worker's claim and the scans (`list`, `ready`, `search`, `counts`) can be narrowed to tasks carrying all the tags named. Anything else a project wants to carry goes in metadata, which takes any key and any value, is written by naming each key so a write never drops the rest, and is matched by `tasks search` along with the task's text. Metadata is for what code filters on; what a person reads belongs in the description or the trail.

## Where to go next

- `tasks guide workers` explains a worker: one role in the conveyor, declared as one file.
- `tasks guide loops` explains how work moves between roles, and how to design a loop of your own.
- `tasks help` is the reference: FIELDS for what each field means, FILTERS and ORDER for reading the queue, METADATA VALUES for how a value is read, and ACTIVITY for what belongs on the trail.
