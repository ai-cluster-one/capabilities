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
shares one image and one set of mounts. The project is copied into the image at
build time.

`agent-box-checkout` compiles the same container and fills it differently. The
image carries tools and the boot path and no project at all; the checkout
arrives at run time on a volume. Everything else about it - the Compose file,
the embedded services, the Supervisor configuration, the mounts - is what
`agent-box` produces.

`host-agents` compiles supervised processes on the machine itself. There is no
image and no Compose file. `sync` writes one launchd agent per service into
`compiler.host.agents_dir` (default `deployment/launchd/`), and `deployment
next` prints the `launchctl` steps that hand them over.

`generic` declares a runtime without compiling anything. Use it when the
project describes its shape for a reader and something outside this capability
executes it.

## What changes between a baked body and a checkout body

Both container profiles build the same box. They disagree about one thing: where
the project comes from, and therefore what a redeploy costs.

**Where the body lives.** `agent-box` copies the project into the image, so the
running container's filesystem is a build artifact and a rebuild replaces it.
`agent-box-checkout` leaves `compiler.container.project_root` an empty mount
point and declares an `agent_body` volume over it. On a first boot the entrypoint
clones `AGENT_REPO_URL` at `AGENT_REPO_BRANCH` into that volume. On every boot
after, a populated volume is left exactly as it is: what was committed there
outranks anything the image believes.

**What a redeploy costs.** This is the whole decision. Under `agent-box` a
rebuild is how a change reaches the box, and anything the agent wrote inside the
container is gone with the old image. Under `agent-box-checkout` the body
survives the rebuild, and a change reaches the box through Git instead. Pick the
checkout profile when the thing inside the box writes to its own project and
that writing has to last - an assistant whose repository is its memory. Pick
`agent-box` when the project is input the box only reads, which is the ordinary
case and the simpler one.

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
checkout, which is now a working tree someone may be committing: a project on
this profile has to ignore its generated host bindings, or every boot shows up
as a change.

**Where the boot inputs sit.** A volume mounted at the project root hides
anything the image left underneath it, so the checkout profile copies the lock,
the entrypoint, and the Supervisor configuration to `/opt/agent` and reads them
from there. They are copied into one directory, so their file names must differ.
The generated `.dockerignore` narrows the build context to exactly those files.

**What the box needs told.** `AGENT_REPO_URL` is required, and Compose passes
only declared keys, so an undeclared one never reaches the entrypoint and a
fresh volume has nothing to clone - `deployment doctor` refuses that runtime
rather than letting the first boot discover it. A private repository also needs
`GIT_DEPLOY_KEY_B64`. `AGENT_REPO_BRANCH` defaults to `main`.

The repository is still needed wherever the image is built: the Compose build
context is the project, even though the running container clones its own copy.
What the checkout profile removes is the project from the *image*, not from the
build.

**Two writers, one branch.** A box that commits its own body and a person who
commits the same repository are two writers, and nothing in this capability
arbitrates between them. That belongs to whatever runs inside the box.

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
identity - one Telegram account, one SQLite queue, one state directory. Nothing
in this capability prevents a container and a host agent from being started
against the same project, because nothing here can see the other machine. That
remains an operator decision, and it is worth making deliberately rather than
discovering through a lock file.
