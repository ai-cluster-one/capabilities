# Deployment Profiles

A deployment answers two independent questions, and they belong to two
different files.

`deployment/runtime.json` answers **what this project runs**: the service
graph, which capability services are on, the environment they need. Its
`profile` field picks the **substrate** those services run on.

`deployment/targets/<name>/target.json` answers **where it goes**: a provider,
a connection, a resource handle. On a container profile the same folder holds
everything compiled for that destination.

Keeping them apart is what lets one declaration reach several destinations
without the service graph being written twice. Adding a destination is a target
file. Changing what the services run *on* is a profile.

## The profiles

`agent-box` compiles a container. `deployment sync` writes, into each target's
folder, a Dockerfile, a Compose file, an entrypoint, an env example, and - when
services are embedded - a Supervisor configuration that runs them as one PID 1. Every embedded service
shares one image and one set of mounts. The project is copied into the image at
build time.

`agent-box-checkout` compiles the same container and fills it differently. The
image carries tools and the boot path and no project at all; the checkout
arrives at run time on a volume. Everything else about it - the Compose file,
the embedded services, the Supervisor configuration, the mounts - is what
`agent-box` produces.

`host-agents` compiles supervised processes on the machine itself, and that
machine is a Mac: launchd is the one supervisor it compiles for. There is no
image and no Compose file. `sync` writes one launchd agent per service into
`compiler.host.agents_dir` (default `deployment/launchd/`), and `deployment
next` prints the `launchctl` steps that hand them over.

`generic` declares a runtime without compiling anything. Use it when the
project describes its shape for a reader and something outside this capability
executes it.

A Linux host runs the same services one of two ways. A container profile
compiles them into the image and Compose file the host runs. `generic` declares
them, and a supervisor the project owns on that host - a
systemd unit, for instance - starts them and keeps them running.

## What changes between a baked body and a checkout body

Both container profiles build the same box. They disagree about one thing: where
the project comes from, and therefore what a redeploy costs.

**Where the body lives.** `agent-box` copies the project into the image, so the
running container's filesystem is a build artifact and a rebuild replaces it.
`agent-box-checkout` leaves `compiler.container.project_root` an empty mount
point and declares an `agent_body` volume over it. On a first boot the entrypoint
clones `AGENT_REPO_URL` at `AGENT_REPO_BRANCH` into that volume. On every boot
after, a populated volume is never cloned over: what was committed there
outranks anything the image believes. Bringing it level with the branch it
tracks is a separate job, and the profile does it - see **Keeping the body
current** below.

**What a redeploy costs.** This is the whole decision. Under `agent-box` a
rebuild is how a change reaches the box, and anything the agent wrote inside the
container is gone with the old image. Under `agent-box-checkout` the body
survives the rebuild, and a change reaches the box through Git instead - carried
by the profile's own sync program rather than by a rebuild or by anything the
project declares. Pick the checkout profile when the thing inside the box writes
to its own project and that writing has to last - an assistant whose repository
is its memory. Pick `agent-box` when the project is input the box only reads,
which is the ordinary case and the simpler one.

**What still comes from the image.** `deployment/capabilities.lock` is compiled
from the effective project gate on the workstation and copied into the image, so
under both profiles adding a capability needs an image rebuild rather than a
push. Only the project travels through Git.

**When initialization runs.** Host bindings, capability wiring, and compiled
context are build artifacts of a checkout. `agent-box` makes them at build time
against the copy it holds. `agent-box-checkout` has nothing to make them against
until the volume is mounted, so its entrypoint runs `capabilities init`, and
where ContextKit is bound `contextkit init`, `install-hooks` and `build`, on
every boot. That costs boot time and needs the network at start; in exchange the
bindings always describe the checkout actually running. It also writes into the
checkout, which is now a working tree the profile itself commits and pushes: a
project on this profile has to ignore its generated host bindings, or every boot
shows up as a commit the box sends to the branch everyone else reads.

**Where the boot inputs sit.** A volume mounted at the project root hides
anything the image left underneath it, so the checkout profile copies the lock,
the entrypoint, the sync program, and the Supervisor configuration to
`/opt/agent` and reads them from there. They are copied into one directory, so
their file names must differ, and `deployment doctor` refuses a declaration that
collides with one of them. The generated `Dockerfile.dockerignore` narrows the build
context to exactly those files.

**What the box needs told.** `AGENT_REPO_URL` is required, and Compose passes
only declared keys, so an undeclared one never reaches the entrypoint and a
fresh volume has nothing to clone - `deployment doctor` refuses that runtime
rather than letting the first boot discover it. A private repository also needs
`GIT_DEPLOY_KEY_B64`. `AGENT_REPO_BRANCH` defaults to `main`.

The repository is still needed wherever the image is built: the Compose build
context is the project, even though the running container clones its own copy.
What the checkout profile removes is the project from the *image*, not from the
build.

**Keeping the body current.** A box whose body never moves is a box stuck on the
commit it first cloned, and anything it wrote there never leaves it. That is a
property of this runtime shape rather than of any one project, so the profile
owns it: managed checkout artifacts render a sync program beside the entrypoint
and supervise it, and the project declares nothing to get it.

It runs in both directions in one pass. What was pushed to the tracked branch is
taken in by rebase, and what the box wrote and nobody committed deliberately is
snapshotted and sent on. Both halves are one program because a puller and a
pusher sharing a working tree have to agree on when it is safe to touch it.

It runs twice over: once in the entrypoint before anything else reads the body,
and then on an interval under Supervisor. The boot pass is what makes the box
repairable - a box that took in a broken declaration has to be able to take in
the fix, so the program depends on git and the base system alone and reaches for
no capability CLI, no project configuration, and no scheduler. A boot pass that
cannot reach the remote warns and lets the box come up on the body it has.

A change is only snapshotted once the tree has been continuously dirty for the
quiet window, measured from the first pass that saw it dirty, so a burst of
writing becomes one commit rather than five and a continuous writer cannot defer
the snapshot forever. The snapshot is taken before the network is touched, so an
unreachable remote costs the push and never the commit. A tree left
mid-operation - an interrupted rebase, an unresolved merge - stops the pass
loudly instead of reading as settling, and a rebase that conflicts is aborted,
leaving the box holding its own work until a person unpicks it. Every one of
those is a failed pass rather than a failed process: the program says so and
waits for the next tick, so a fault nobody has fixed yet costs one line an
interval rather than a restart.

`AGENT_BODY_SYNC` turns it off, `AGENT_BODY_SYNC_INTERVAL` sets how often a pass
runs, and `AGENT_BODY_SYNC_QUIET` sets the window. The generated `.env.example`
carries all three with the values the box will run with. A box told not to sync
exits zero and stays exited rather than restart-looping, and a program that
cannot run at all is backed off and reported rather than respawned under a
status that reads healthy.

Where the entrypoint artifact is declared `external`, the project has taken its
boot path and process management whole, and the profile renders no sync program
into it.

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

**The program carries the project's name.** A host agent does not run a
capability's CLI directly; each service compiles a launcher beside its plist,
named `<project>-<service>`, and the job names that. macOS lists a background
item by the basename of the program its job names, never by the job's label, so
without this every project supervising the same capability appears under one
indistinguishable name and nobody can tell which project a switch belongs to.

**Changing the program means re-registering, not reloading.** macOS records the
program when the agent file appears in `~/Library/LaunchAgents`, and keeps that
record across `bootout` and `bootstrap`. An agent whose compiled program
changed therefore keeps listing itself under the old one until the installed
file is removed and put back. `deployment doctor` compares what launchd holds
against what the compiler names and reports the difference; `deployment next`
prints the sequence that clears it.

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
identity - one Telegram account, one job queue, one state directory. Nothing
in this capability prevents a container and a host agent from being started
against the same project, because nothing here can see the other machine. That
remains an operator decision, and it is worth making deliberately rather than
discovering through a lock file.

## The machine scope

Some services run once per machine rather than once per project: a capability whose manifest declares `service.machine` runs one process for every project that opted in to it. Such a process belongs to no project, so its agent cannot be compiled from one. `deployment machine` is that supervision, and like `host-agents` it is a Mac concern: on a server one agent is one environment, and its services run in project mode through a profile as they do today.

`deployment machine sync` asks the capabilities manager for the installed set and each capability's machine state, and reads each one's manifest snapshot from the registry. Every capability that declares `service.machine`, is allowed on this machine, and is not disabled in the machine declaration gets a launcher `capabilities-machine-<name>` and an agent `capabilities.machine.<name>.plist`. The agent runs the declared `service.machine.command` in the expanded `service.machine.state` (the home directory when the service declares none, which is never a project), with the compiling shell's PATH captured as the host profile captures it, `RunAtLoad`, `KeepAlive` on an unsuccessful exit, and `AbandonProcessGroup`, because a machine service starts work that outlives it and launchd must not reap that work when it stops or restarts the service. The machine homes the declaration was expanded against travel with the agent when they are set, so launchd starts it against the settings and state sync read. Every other installed capability is reported as skipped with its reason: it declares no machine service, it is quarantined here (lifting that is the user's decision), or it is disabled in the declaration. A machine service no project has joined yet is still compiled, and idles.

Sync also writes `capabilities.machine.watchdog.plist`, scheduled like the project watchdog. Its pass, `deployment machine watchdog`, asks each machine service's `service.machine.doctor` with the same failure count, timeout and cooldown, and runs `launchctl kickstart -k` on an agent that keeps answering badly. It runs outside any project, so it passes the gate only where `deployment` is enabled globally; sync warns when it is not.

The declaration is this machine's, at `$XDG_CONFIG_HOME/deployment/machine.json`: `{"watchdog": {...}, "services": {"<name>": {"disabled": true}}}`, where `watchdog` takes the keys `compiler.host.watchdog` takes. A missing file means every default; a file that cannot be read is refused rather than guessed at. The agents, launchers, logs and watchdog state land in `$XDG_STATE_HOME/deployment/machine/launchd/`. Nothing either verb writes depends on where it is run from.

`deployment machine next` prints the hand-over: stop any copy started by hand, create a working directory launchd would otherwise refuse to start in, remove any earlier installed copy, `ln -sf` each agent into `~/Library/LaunchAgents`, and `launchctl bootstrap gui/$UID` it. Installing stays a person's act. `deployment machine status` reports each compiled agent and whether it is linked and loaded, and writes nothing. An agent left for a service that is no longer supervised is reported by sync with the `bootout` that removes it; sync never deletes it.

A project that joins a machine service stops supervising that service itself first: set `service_policy.capabilities.<name>` to `"disabled"` in its `deployment/runtime.json`, run `deployment sync`, and boot out and remove its project agent. Otherwise its project agent and watchdog keep starting a project-mode process the joined project refuses.
