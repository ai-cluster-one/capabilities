# Deployment Profiles

A deployment answers two independent questions, and they belong to two
different files.

`deployment/runtime.json` answers **what this project runs**: the service
graph, which capability services are on, the environment they need. Its
`profile` field picks the **substrate** those services run on.

`deployment/targets/<name>.json` answers **where it goes**: a provider, a
connection, a resource handle.

Keeping them apart is what lets one declaration reach several destinations
without the service graph being written twice. Adding a destination is a target
file. Changing what the services run *on* is a profile.

## The profiles

`agent-box` compiles a container. `deployment sync` writes a Dockerfile, a
Compose file, an entrypoint, an env example, and - when services are embedded -
a Supervisor configuration that runs them as one PID 1. Every embedded service
shares one image and one set of mounts.

`host-agents` compiles supervised processes on the machine itself. There is no
image and no Compose file. `sync` writes one launchd agent per service into
`compiler.host.agents_dir` (default `deployment/launchd/`), and `deployment
next` prints the `launchctl` steps that hand them over.

`generic` declares a runtime without compiling anything. Use it when the
project describes its shape for a reader and something outside this capability
executes it.

## What changes between a container and a host

**Supervision.** Both substrates read the same `restart` field from a
capability's deploy descriptor, and they honour it differently on purpose. In a
container, `unless-stopped` means an unconditional restart: nobody is at a
terminal inside the image to stop anything, so a process that exits has failed.
On a host there *is* a person, and `<capability> service stop` has to mean what
it says - so the agent restarts on a non-zero exit and stays down after a clean
one. A crash comes back; a deliberate stop is honoured. `restart: "no"` leaves
the job to `RunAtLoad` alone on both.

**The composition modes collapse.** `service_policy` distinguishes `embedded`
from `enabled` because a container can either fold a service into the agent
image or give it its own Compose service. A host has no image to fold into, so
both modes render the same thing: one supervised process per service.
`disabled` still means the CLI is installed and the service does not run.

**Secrets stay out of the artifact.** A container reads its environment from a
`.env` the operator fills. A host agent gets no such file: every capability
already resolves its own credentials through the cascade at run time, so a
compiled agent carries only `PATH` and the non-secret defaults from
`environment_defaults`. Nothing that a descriptor marks required is written
into it.

**PATH is compiled in.** launchd hands a job a bare environment - no login
shell runs, so nothing a profile would have exported is present. A service
needs more than its own executable: it spawns workers and reaches other tools,
and a PATH holding only the service commands would start cleanly and then fail
on the first thing it shells out to. So the PATH that was demonstrably working
- the one belonging to the shell that ran sync - is captured whole, behind the
directories the declared commands resolve from.

Each entry is resolved through symlinks on the way in, which matters for
version managers: a per-session shim directory disappears with the shell that
created it, while the installation directory it points at does not. Entries
that are not directories are dropped rather than written out as hopeful
guesses.

Compile from a shell where the services actually run, and recompile when the
toolchain moves. This is the main reason a compiled agent belongs to one
machine.

**State stays where the capability put it.** A container profile maps declared
mounts into volumes. A host profile maps nothing: each capability already owns
a state home and keeps using it.

## Compiled agents are machine-local

A launchd agent names an absolute working directory and an absolute PATH, so it
is bound to one checkout on one machine - closer to `.env.local` than to a
Dockerfile. Ignore `deployment/launchd/` in a repository that more than one
machine checks out, and let each machine compile its own.

`sync` still tracks them: an edited agent is reported as drift, the same as any
other generated artifact, so a hand-tweak surfaces instead of quietly diverging.

## Handing over, and taking back

`deployment next` prints the steps: stop anything you started by hand first,
link the compiled agent into `~/Library/LaunchAgents`, then
`launchctl bootstrap gui/$UID <plist>`.

Afterwards the controls are plain launchctl, and they are the ones to reach
for. `launchctl kickstart -k gui/$UID/<label>` restarts a job, `launchctl kill
TERM gui/$UID/<label>` stops it and leaves it stopped, `launchctl kickstart
gui/$UID/<label>` starts it again, `launchctl print gui/$UID/<label>` reports
its pid and last exit status, and `launchctl bootout gui/$UID/<label>` removes
it entirely.

`<capability> service stop` is a different thing and it is worth knowing which
you are holding. That verb reaches a daemon the capability started itself, by
the record it wrote when it did. A `service run` under a supervisor writes no
such record - the supervisor is what owns the process - so on some capabilities
the verb finds nothing and reports it, truthfully, as already stopped while
launchd keeps the service running. Where a capability's `run` does register
itself, the verb works and launchd honours it.

That it is honoured at all rests on the service exiting zero when asked to
stop. A process killed by a signal it does not handle looks like a crash to
launchd, and a crash is what `KeepAlive` exists to undo - so a service that
ignored SIGTERM would be restarted out from under whoever stopped it. Both
paths above end in a clean exit for a service that shuts down on SIGTERM, which
is what a `service run` written for a supervisor already does.

`deployment doctor` reads that state back: it reports each declared agent as
installed or not, loaded or not, so a service that was compiled and never
handed over, or handed over and since unloaded, is visible rather than assumed.

## One substrate at a time

Two profiles that both run the same service are two processes competing for one
identity - one Telegram account, one SQLite queue, one state directory. Nothing
in this capability prevents a container and a host agent from being started
against the same project, because nothing here can see the other machine. That
remains an operator decision, and it is worth making deliberately rather than
discovering through a lock file.
