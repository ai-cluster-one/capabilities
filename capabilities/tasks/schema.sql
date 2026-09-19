-- tasks — what has to be done, and what was done about it.
--
-- The schema name below is the one the connection names as `db_schema`; the
-- default is `tasks`. Rename it here and in the connection together, or the CLI
-- will look somewhere this file never created anything.

create schema if not exists tasks;

create table if not exists tasks.tasks (
  id           uuid primary key default gen_random_uuid(),
  type         text        not null,
  unique_key   text        unique,
  title        text        not null,
  objective    text,
  description  text,
  status       text        not null default 'draft'
               check (status in ('draft','todo','in_progress','complete','closed')),
  assignee     text,
  tags         text[]      not null default '{}',
  metadata     jsonb       not null default '{}'::jsonb,
  pickup_at    timestamptz,
  created_by   text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

comment on column tasks.tasks.type is
  'Which pipeline this task belongs to. Extended by decision, not in passing.';
comment on column tasks.tasks.unique_key is
  'Idempotency handle computed by whoever seeds the row. Nullable: many nulls coexist.';
comment on column tasks.tasks.objective is
  'Why the task exists and what counts as done. Does not go stale while the task lives.';
comment on column tasks.tasks.description is
  'What was seen when the task was raised. A snapshot, and says so.';
comment on column tasks.tasks.assignee is
  'Who carries the task. Free text - a name, not a key into any directory.';
comment on column tasks.tasks.metadata is
  'Only what code filters on. Anything a person reads belongs in description.';
comment on column tasks.tasks.status is
  'draft is unreleased, todo is releasable, in_progress is held by a claim, '
  'complete is done, closed is over without having been done - superseded, '
  'obsolete, or answered somewhere else. Both terminal states leave the queue; '
  'only one of them claims the work happened.';
comment on column tasks.tasks.pickup_at is
  'Do not raise the task before this moment. Empty means no appointed moment, not permission.';
-- In the create above for a new store, and added here for one that predates it.
alter table tasks.tasks add column if not exists created_by text;
comment on column tasks.tasks.created_by is
  'Who created the task, as the CLI resolved it: the raise it ran under, the '
  'actor it was told, or the account it ran as.';

-- A store created before `closed` existed keeps the check it was created with,
-- and `create table if not exists` never revisits one. The constraint therefore
-- replaces itself, which is safe to repeat and rejects nothing a live row holds.
alter table tasks.tasks drop constraint if exists tasks_status_check;
alter table tasks.tasks add constraint tasks_status_check
  check (status in ('draft','todo','in_progress','complete','closed'));

create index if not exists tasks_status_pickup_idx on tasks.tasks (status, pickup_at);
create index if not exists tasks_assignee_idx       on tasks.tasks (assignee);
create index if not exists tasks_metadata_gin      on tasks.tasks using gin (metadata jsonb_path_ops);
create index if not exists tasks_tags_gin          on tasks.tasks using gin (tags);

create table if not exists tasks.task_activities (
  id          uuid primary key default gen_random_uuid(),
  task_id     uuid not null references tasks.tasks (id) on delete cascade,
  description text not null,
  actor       text,
  created_at  timestamptz not null default now()
);

comment on table tasks.task_activities is
  'Work that actually happened, in plain words: at this moment, this occurred. '
  'Creating a task or editing a field is not activity. Entries carry no shape yet - '
  'a kind will be added once we can see from real entries what kinds there are.';
-- In the create above for a new store, and added here for one that predates it.
alter table tasks.task_activities add column if not exists actor text;
comment on column tasks.task_activities.actor is
  'Who recorded the entry, as the CLI resolved it: the raise it ran under, the '
  'actor it was told, or the account it ran as.';

create index if not exists task_activities_task_idx on tasks.task_activities (task_id, created_at desc);

create or replace function tasks.touch_updated_at() returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists tasks_touch_updated_at on tasks.tasks;
create trigger tasks_touch_updated_at before update on tasks.tasks
    for each row execute function tasks.touch_updated_at();

create table if not exists tasks.task_executions (
  id           uuid primary key default gen_random_uuid(),
  task_id      uuid        not null references tasks.tasks (id) on delete cascade,
  attempt      integer     not null,
  worker       text,
  handler      text,
  status       text        not null default 'running'
               check (status in ('running','ok','failed','handback','abandoned')),
  lease_until  timestamptz,
  run_system   text,
  run_ref      text,
  detail       text,
  metrics      jsonb       not null default '{}'::jsonb,
  started_at   timestamptz not null default now(),
  ended_at     timestamptz
);

-- A store created before this column exists upgrades in place; `migrate` creates
-- tables that are absent and never alters one that is present, so the column has
-- to say so itself.
alter table tasks.task_executions add column if not exists metrics jsonb not null default '{}'::jsonb;

comment on table tasks.task_executions is
  'One row per raise: opened when the task is claimed, closed when it is released. '
  'Immutable once closed, so nothing here is ever read-modify-written, and the '
  'attempt number is the count of these rows rather than a counter to keep in step.';
comment on column tasks.task_executions.lease_until is
  'When the claim lapses. A worker that dies stops renewing nothing - the moment '
  'simply passes, the next claim closes the row as abandoned, and the task is free.';
comment on column tasks.task_executions.metrics is
  'What the runner measured about this raise, in its own vocabulary - a duration, '
  'a token count, a price. The tracker stores it and reads none of it: a number '
  'that means something only to the thing that produced it does not belong in a '
  'column every consumer inherits.';
comment on column tasks.task_executions.run_ref is
  'Opaque handle of the run in whatever system executed it, named by run_system. '
  'A pointer and never a copy: the cost, the log and the duration stay with their owner.';

create index if not exists task_executions_task_idx
  on tasks.task_executions (task_id, started_at desc);

-- Two workers cannot hold one task, because the store refuses the second row
-- rather than because a dispatcher promised to take one at a time.
create unique index if not exists task_executions_one_open_idx
  on tasks.task_executions (task_id) where status = 'running';

create table if not exists tasks.task_changes (
  id            uuid primary key default gen_random_uuid(),
  task_id       uuid        not null references tasks.tasks (id) on delete cascade,
  field         text        not null,
  old_value     text,
  new_value     text,
  execution_id  uuid references tasks.task_executions (id) on delete set null,
  actor         text,
  changed_at    timestamptz not null default now()
);

comment on table tasks.task_changes is
  'Status, pickup, type and assignee moves, written by the CLI under whatever verb made them, '
  'never by whoever remembered to. Extended to another field by decision, and '
  'never by recording everything that can change.';
comment on column tasks.task_changes.execution_id is
  'The raise that caused the move, where one did. Null for a move a person made.';
comment on column tasks.task_changes.actor is
  'Who made the move: the worker holding the raise for claim and release, and '
  'otherwise the identity the CLI resolved - a raise, a named actor, or the account it ran as.';

create index if not exists task_changes_task_idx
  on tasks.task_changes (task_id, changed_at desc);
