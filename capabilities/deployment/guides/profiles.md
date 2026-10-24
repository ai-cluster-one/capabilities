# Deployment Profiles

A deployment answers two independent questions, and they belong to two
different files.

`deployment/runtime.json` answers **what this project runs**: the service graph, which capability services are on and in what shape, the environment they need. Its `profile` field picks the **substrate** those services run on. A project whose gate is in the store is the exception: there, whether a service runs on a given node is neither file's answer but the gate's, read on that node (see **Where a service runs** below).

`deployment/targets/<name>/target.json` answers **where it goes**: a provider,
a connection, a resource handle. On a container profile the same folder holds
everything compiled for that destination.

Keeping them apart is what lets one declaration reach several destinations
without the service graph being written twice. Adding a destination is a target
file. Changing what the services run *on* is a profile.

## The profiles

`agent-box` compiles a container. `deployment sync` writes, into each target's folder, a Dockerfile, a Compose file, an entrypoint, an env example, and - when services are embedded - a Supervisor configuration that runs them as one PID 1. Every embedded service shares one image and one set of mounts. The project is copied into the image at build time.

`agent-box-checkout` compiles the same container and fills it differently. The
image carries tools and the boot path and no project at all; the checkout
arrives at run time on a volume. Everything else about it - the Compose file,
the embedded services, the Supervisor configuration, the mounts - is what
`agent-box` produces.

`agent-box-shell` compiles the same container with no body at all, for an agent that lives wholly in the store. The image carries tools and the boot path; nothing arrives from Git, there is no body volume and no body sync, and no ContextKit is installed or run. On every boot the entrypoint binds the empty `compiler.container.project_root` to the agent the store holds under `CAPABILITIES_PROJECT_ID` with `capabilities init --id`, which binds it by the id alone with no ContextKit binding, wires the agent CLIs' session hooks with `capabilities hooks install --claude --codex`, composes the context from the store with `capabilities build`, and then starts the compiled services under their runners, which the gate places per node. The Compose file sets `CAPABILITIES_PROJECT_ID` as the deploying folder resolves it at sync - a folder bound with `capabilities init --id <id>` that holds the `deployment/` declarations - and passes it through from the server's environment when that folder resolves none; a box started without it stops at boot. Whatever the box writes in its project root lasts only as long as the container: what has to outlive a redeploy lives in the store or on a service's declared volume.

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

## Where a service runs

Every compiled profile supervises each service it compiles through its runner, `deployment runner <name> ... -- <command>`: the Supervisor program in the agent container, the command of a service's own Compose service, and the program of its launchd agent on a Mac. Which services are compiled depends on where the project keeps its gate.

**Files mode.** Files carry no answer per node, so a project whose gate is in `capabilities/settings.json` keeps deciding in its runtime declaration, exactly as before: only capabilities the project enabled itself are candidates, `service_policy` (`auto_include`, `default_mode`, `auto`, `enabled`, `disabled` or `embedded` per capability, and the descriptor's `default_policy`) decides which of them run and how, and `service_policy.placement` and `services.<name>.placement` (`node`, `local` or `both`) choose the substrate. A capability enabled for its CLI and set to `disabled` gets no runner at all.

**Store mode.** An agent whose records are in the store - every agent `capabilities init` made since agents start there, and every one whose records moved with `capabilities migrate` - compiles every deployable service the installed catalogue declares, less the runtime's `capabilities.exclude`, and the runner, not the compile, decides whether the service's own command runs. The compile is the same on every node, and turning a service on or off for a node is a gate write that needs no recompile and no redeploy.

Once per reconcile (`service_policy.reconcile_seconds`, default 60) the runner reads the gate for its capability in this project on this node, the node being the name the machine declares in `AGENT_NODE_NAME`, else its host name. A project whose records are in the store places a service on a node only through its own policy row naming that node, which carries two answers apart: `enabled`, whether the capability's CLI is usable there, and `service`, whether its service is placed there. `capabilities place <name> --node <node>` and `capabilities unplace <name> --node <node>` change the service alone; `capabilities enable <name> --project --node <node>` writes both true and `disable` both false. Its row for every node, and any global row, make the capability's CLI usable there without placing the service, and a node row that does not place it, or no node row, leaves it off. In files mode the runtime declaration has already chosen which services are compiled, and the runner runs one where the project's own explicit enable stands, on every machine that reads those files. Nothing inherited or global places a service. A container node keeps its name across redeploys only by declaring it: set `AGENT_NODE_NAME` in the node's environment, which the Compose file passes through.

A service's descriptor declares how many nodes run it at once, as `service.placement`: `exclusive`, the default, for a service that holds one identity - one account, one connection - and `shared` for one that is safe beside itself on other nodes, such as a queue claimed row by row. Placing an exclusive service on a node with `capabilities place` takes it off every other node in the same write, which is how it moves; a shared one is placed beside the nodes it already runs on, and its runner takes no lease.

Placed, the runner of an exclusive service takes the service's lease before starting it: a row in the project's store, keyed by project and capability, naming the holding node and expiring three reconciles after its last renewal. The holder renews it every reconcile. A runner on another node whose gate also places the service refuses to start it while that lease is live, says so on its stderr naming the holder and the expiry, and keeps checking, so it takes over once the lease expires or is released. A runner the gate turns off stops its service - TERM to the service's process group, KILL after 20 seconds - and then releases the lease, so the node the service moved to need not wait for it to run out. A runner that cannot confirm its lease keeps a running service only until its own hold would have run out, and starts none. Exit 4 is the contract's policy refusal, so a service that exits with it is not restarted: its runner releases the lease, holds the service idle, and reports the refusal - the service's own error code and message when it printed one - in its log, in `deployment services` and in `deployment node status`. It tries again only when the gate answer it reads for this node changes (a row appears, disappears or flips) or the runner restarts. Every other exit follows the descriptor's restart policy. A service that exits with any other non-zero code, or that cannot be started at all, has why written onto the lease its node holds: the exit code, its last lines of stderr and when, which `deployment services` shows as the lease's `last_error` and any machine reading the store can read; they stay until it fails again, and another node taking the lease clears them. Where no database is resolvable for the project, which is possible only in files mode, no lease can be taken: the runner runs the service as the gate says and reports the lease as unavailable.

`deployment services` is a node's own view: for each compiled service, what the gate answers there and which row answered, whether its runner is running it, and who holds its lease. `deployment node status` runs it inside a container node and folds it into each service it reports. Each runner also records its state under `capabilities/deployment/state/runners/`, and the host profile's watchdog leaves alone a service its runner holds idle.

In store mode `service_policy` keeps only the shape of the container a service runs in: `shape` for every service and `capabilities.<name>` for one, each `embedded` (a Supervisor program in the agent container) or `container` (a Compose service of its own). There the switches that decide whether a service runs in files mode - `auto_include`, `default_mode`, and `auto`, `enabled` or `disabled` per capability - and `service_policy.placement` and `services.<name>.placement` are retired: `deployment doctor` and `deployment sync --check` report each one a store-mode runtime still carries, and `deployment sync` rewrites it, keeping the shape it implied (`enabled` was a container of its own); a `disabled` service is no longer kept from running by it, and `capabilities.exclude` is what keeps a service out of the runtime altogether. A files-mode runtime is neither warned about nor rewritten. `service_policy.reconcile_seconds` sets the reconcile in either mode.

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

**What still comes from the image.** Under both profiles the image installs
every capability in the official catalogue at `CAPABILITIES_REF`, and every boot
brings the registry to that catalogue, so a capability the catalogue gains or
changes reaches the box without a change to the project. Whether one is
usable is its gate's and its connections' answer, not whether it is installed.
Only the project travels through Git.

**Keeping the tools the box updates.** By default the capabilities manager's
home is part of the image: a redeploy brings every capability back to what the
image installed, and whatever a release or update run inside the box wrote is
gone with the old container. Setting
`compiler.container.persistent_manager` to `true` in `deployment/runtime.json`,
on either container profile, puts the manager's whole writable home on named
volumes that every container the image runs mounts: `capabilities_registry`
(`~/.capabilities`: the registry, its staging and the manager's releases),
`capabilities_bin` (`~/.capabilities-bin`, the launcher directory),
`capabilities_config` (`~/.config/capabilities`), `capabilities_state`
(`~/.local/state/capabilities`: release records, the mutation lock and dev
sessions), `capabilities_data` (`~/.local/share/capabilities`, dev worktrees)
and `capabilities_cache` (`~/.cache/capabilities`). Each directory the manager
writes is one volume, so every rename it makes - a temporary beside its target,
a stage beside the registry entry or release it becomes, a launcher swapped
beside the old one - stays inside one volume. The Dockerfile sets
`CAPABILITIES_BIN` to the launcher directory and puts it first on `PATH` in its
own `ENV`, installs the catalogue there at build, and creates each mount point owned
by the agent; the agent CLIs keep their links in the image's `~/.local/bin`.

A fresh volume is seeded from the image, so the first deploy starts from the
tools the image installed. From then on the volumes are what runs, and every boot
brings them to the catalogue the image names: the manager first, then each
capability the registry lacks, installed from another catalogue or behind it.
A capability installed from a local path or a registered source is left as it is. A capability service's own state mount under
`~/.local/state/capabilities` - `~/.local/state/capabilities/projects` - sits
inside `capabilities_state` as a volume of its own; Docker mounts the parent
before the child, the manager never writes under `projects`, and the service
keeps its own volume. The option changes nothing while it is unset.

**When initialization runs.** Host bindings, capability wiring, and compiled
context are build artifacts of a checkout. `agent-box` makes them at build time
against the copy it holds. `agent-box-checkout` has nothing to make them against
until the volume is mounted, so its entrypoint runs `capabilities init`, and
where ContextKit is bound `capabilities hooks install --claude --codex` and
`capabilities build`, on every boot; ContextKit itself is neither installed nor
run. Each body sync rebuilds the context with `capabilities build`. That costs boot time and needs the network at start; in exchange the
bindings always describe the checkout actually running. It also writes into the
checkout, which is now a working tree the profile itself commits and pushes: a
project on this profile has to ignore its generated host bindings, or every boot
shows up as a commit the box sends to the branch everyone else reads.

**Where the boot inputs sit.** A volume mounted at the project root hides
anything the image left underneath it, so the checkout profile copies the
entrypoint, the sync program, and the Supervisor configuration to
`/opt/agent` and reads them from there. They are copied into one directory, so
their file names must differ, and `deployment doctor` refuses a declaration that
collides with one of them. The generated `Dockerfile.dockerignore` narrows the build
context to exactly those files.

**What the box needs told.** `AGENT_REPO_URL` is required, and Compose passes
only declared keys, so an undeclared one never reaches the entrypoint and a
fresh volume has nothing to clone - `deployment doctor` refuses that runtime
rather than letting the first boot discover it. A private repository also needs
`GIT_DEPLOY_KEY_B64`. `AGENT_REPO_BRANCH` defaults to `main`.

The declared repository wins over the remote the body was cloned from. On every
boot of a populated body, when `AGENT_REPO_URL` is set and differs from the
checkout's `origin`, the entrypoint points `origin` at it before the boot sync
pass and logs that it did, so a box whose source moved follows it on its next
deploy; the body's content and its own commits stay as they are, and the sync
pass rebases them onto the new branch. A matching or unset `AGENT_REPO_URL`
changes nothing.

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

**Other repositories the box works on.** A box that develops another repository
beside its own body - the capabilities source it releases from, say - declares
it in `compiler.container.checkouts`, a list of `{path, url, branch, register}`:
an absolute path outside the project root, the repository to clone, the branch
(default `main`), and optionally `register: "official"`. On every boot, after
initialization and before the doctors, the entrypoint takes each in turn. A path
that is already a git checkout is left exactly as it is - never cloned over,
reset or fetched - because it is the box's own working tree. A path that is
absent or holds no files is cloned at the branch. A path that holds files but
no checkout is left untouched with a warning. With `register`, the checkout is
then registered with the capabilities manager as its integration checkout for
that source (`./bin/capabilities source register-checkout official` from the
checkout), unless the manager already names that checkout, so a fresh node can
open a development session with no manual step. A clone or a registration that
fails warns and lets the box come up. Keep the path on a volume of its own when
the work in it has to outlive a redeploy; without one, each new container clones
it afresh. The declaration applies only to `agent-box-checkout`, and a runtime
that declares none compiles exactly as before.

## What changes between a container and a host

**Supervision.** Both substrates read the same `restart` field from a capability's deploy descriptor, and each runner applies it to its service, honouring it differently on purpose. In a container, `unless-stopped` means an unconditional restart: nobody is at a terminal inside the image to stop anything, so a process that exits has failed. On a host there *is* a person, and `<capability> service stop` has to mean what it says - so the runner restarts the service after a non-zero exit and leaves it down after a clean one, until the gate turns it off and on again or the runner restarts. A crash comes back; a deliberate stop is honoured. `restart: "no"` starts the service once on both. The runner itself is kept running by its supervisor whatever the descriptor says, since it is what starts the service when the gate places it.

**The shapes collapse.** `service_policy` distinguishes `embedded` from a Compose service of its own (`enabled` in files mode, `container` in store mode) because a container can either fold a service into the agent image or give it its own Compose service. A host has no image to fold into, so both shapes render the same thing: one supervised runner per service.

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

## One machine at a time

Two machines that both run the same exclusive service are two processes competing for one identity - one Telegram account, one linked device, one state directory. The lease is what prevents it: a container node and a Mac compiled from the same project each run the service's runner, and only the one holding the lease runs the service, whichever profile compiled it. Where the project has no database the lease cannot be taken, so in files mode without a store keeping one machine at a time remains an operator decision, made with the runtime's placement. The capability's own `service start` and `service run`, called by hand, take no lease.

## The machine scope

Some services run once per machine rather than once per project: a capability whose manifest declares `service.machine` runs one process for every project that opted in to it. Such a process belongs to no project, so its agent cannot be compiled from one. `deployment machine` is that supervision, and like `host-agents` it is a Mac concern: on a server one agent is one environment, and its services run in project mode through a profile as they do today. Which of the two a service runs as decides how much its agent turns may safely do - a machine service sits beside a person's files and other projects, a container node beside nothing but its own project - and a capability that runs agent turns says how to choose for each: `tasks guide workers` does for workers' harness profiles, and why Claude Code's OS sandbox may be unavailable inside a container.

`deployment machine sync` asks the capabilities manager for the installed set and each capability's machine state, and reads each one's manifest snapshot from the registry. Every capability that declares `service.machine`, is allowed on this machine, and is not disabled in the machine declaration gets a launcher `capabilities-machine-<name>` and an agent `capabilities.machine.<name>.plist`. The agent runs the declared `service.machine.command` in the expanded `service.machine.state` (the home directory when the service declares none, which is never a project), with the compiling shell's PATH captured as the host profile captures it, `RunAtLoad`, `KeepAlive` on an unsuccessful exit, and `AbandonProcessGroup`, because a machine service starts work that outlives it and launchd must not reap that work when it stops or restarts the service. The machine homes the declaration was expanded against travel with the agent when they are set, so launchd starts it against the settings and state sync read. Every other installed capability is reported as skipped with its reason: it declares no machine service, it is quarantined here (lifting that is the user's decision), or it is disabled in the declaration. A machine service no project has joined yet is still compiled, and idles.

Sync also writes `capabilities.machine.watchdog.plist`, scheduled like the project watchdog. Its pass, `deployment machine watchdog`, asks each machine service's `service.machine.doctor` with the same failure count, timeout and cooldown, and runs `launchctl kickstart -k` on an agent that keeps answering badly. A loaded machine agent with no pid is never kickstarted, because a machine agent's `KeepAlive` restarts its process only after a failure: the pass reports it as `outside_launchd`, with the pid and the remedy, when the service's doctor says the service runs as a copy started outside launchd; as `stopped` when it exited cleanly (last exit code 0), which `launchctl kickstart gui/$UID/<label>` or the capability's own `service start --machine` starts again; and otherwise as `restarting`, which launchd owns. It runs outside any project, so it passes the gate only where `deployment` is enabled globally; sync warns when it is not.

The declaration is this machine's, at `$XDG_CONFIG_HOME/deployment/machine.json`: `{"watchdog": {...}, "services": {"<name>": {"disabled": true}}}`, where `watchdog` takes the keys `compiler.host.watchdog` takes. A missing file means every default; a file that cannot be read is refused rather than guessed at. The agents, launchers, logs and watchdog state land in `$XDG_STATE_HOME/deployment/machine/launchd/`. Nothing either verb writes depends on where it is run from.

`deployment machine next` prints the hand-over: stop any copy started by hand, create a working directory launchd would otherwise refuse to start in, remove any earlier installed copy, `ln -sf` each agent into `~/Library/LaunchAgents`, and `launchctl bootstrap gui/$UID` it. Installing stays a person's act. `deployment machine status` reports each compiled agent and whether it is linked and loaded, and writes nothing. An agent left for a service that is no longer supervised is reported by sync with the `bootout` that removes it; sync never deletes it.

A project that joins a machine service stops supervising that service itself first: in files mode set `service_policy.capabilities.<name>` to `"disabled"` in its `deployment/runtime.json`, run `deployment sync`, and boot out and remove its project agent. In store mode leave the project no row naming that node, or list the capability in `capabilities.exclude` to keep the project-mode copy out altogether. A runner that starts a project-mode copy anyway is refused by the capability with exit 4 and holds the service idle (see **Where a service runs**), rather than restarting it.
