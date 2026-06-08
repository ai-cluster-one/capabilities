# Procedure: package your own tool into a capability

You are an LLM agent. The user has something they already use — a script, a notebook, a pile of API calls, a routine they run by hand — and wants it turned into a capability they own. This procedure is the **discovery-first funnel**: it works out *what they actually have*, decides honestly *whether it should be a capability at all*, and only then builds one — handing the mechanical authoring to [conform.md](conform.md) and the install to [INSTALL.md](../INSTALL.md). It is interactive: **ask, don't assume.** Its authority is **capabilities only** — on a "no" it says so and points politely elsewhere, nothing more.

## 0. Discover what they have

Ask — don't guess:

- **What is the thing?** A working script; a notebook; a set of `curl`/API calls; a Node or other-language program; steps you do by hand; or nothing executable yet but a service you keep reaching for.
- **What do you actually do with it?** The two or three operations that matter — this becomes the *smallest useful surface*, not everything the underlying API can do.
- **What does it touch, and how does it authenticate?** The service/system, and the auth shape (a token, a login session, OAuth, none).
- **Where will it be used?** One project, several, or machine-wide — this shapes the config-dependency class.

## 1. Decide if it should be a capability at all — the honest gate

A capability is a specific thing: a self-contained CLI an agent drives to read or act on a system, installed once and self-describing. Not everything belongs in that shape. Judge honestly against the boundaries the convention already draws:

- A recurring *procedure* — "do these steps in order, every morning" — is a **routine**, and it lives in the consuming project, not as a capability ([../ROUTINES.md](../ROUTINES.md); DOCTRINE rules 9/11). Capability is the noun, routine is the verb.
- A stable but light set of *instructions* with no real tool to drive is better as a **skill / prompt**.
- A shared, always-on, structured/streaming integration meant for many clients may fit an **MCP server** better.
- A genuine one-off needs no packaging at all.

A capability is the right shape when there is a **system to reach** and a **small surface of operations** worth giving an agent, with credentials and a tool to run.

**If it is not a good capability candidate, say so — plainly and early.** State the one-line reason and a polite pointer ("this reads like a routine — you are better off keeping it as a project routine"; "this is closer to a skill"; "this looks like a job for an MCP server"), then **stop**. This convention has authority over *capabilities*; it does not teach how to build those other things — that is the user's call, and a brief pointer is the whole of our advice. Don't force a poor fit into the capability shape just to produce something.

## 2. Shape the capability

Once it's a yes, pin the design before writing code:

- **Surface** — the smallest set of subcommands worth exposing (the verbs from step 0), each agent-drivable.
- **Credentials** — the secret/connection it needs, which cascade tier holds it, and the **config-dependency class** (`none` / `global` / `project-required`): usable from any project once installed, or needing project-side config.
- **I/O** — JSON on stdout, a structured error envelope on stderr; if it emits a structured payload, plan a keyless `contract` command that prints the shape.
- **doctor** — the cheapest authenticated round-trip that will prove readiness.

This is the contract. [../SHEBANG.md](../SHEBANG.md) is where it gets realized — name it by role here, don't restate it.

## 3. Make it a single runnable script

A capability's executable is one `uv`/PEP-723 Python file. Bring the raw material to *a* single runnable script of that shape:

- a working Python script → a light touch.
- a notebook, a `curl`/API pile, a bash script, a Node/other-language program → **port** the operations from step 0 into one Python CLI, reusing the logic and dropping the rest.
- nothing executable yet → write the thin CLI against the service's API from the surface you defined.

If the thing genuinely cannot become a non-interactive CLI (it needs a GUI, a human mid-call, something not automatable), that is a late "not a capability" — return to step 1's off-ramp. The output of this step is a script that **runs**; making it **conformant** is the next step's job.

## 4. Conform and author the capability folder

Hand the runnable script to [conform.md](conform.md) — the mechanical authoring step: it brings the executable to the SHEBANG.md standard, scrubs every consumer specific and secret, authors the doc slots, and audits in fresh, independent context until clean.

Two things are yours to hold across that hand-off:

- **Stay in the loop.** Review the audit verdicts; don't rubber-stamp. The standing consent to auto-converge belongs to *conformance*, not to design choices — those are the user's.
- **Check worth, not just conformance.** The audit proves the capability matches the doctrine; it does not prove the capability is worth having. Sanity-check it yourself: is the surface minimal-and-useful, does `doctor` actually prove something, is any subcommand dead weight. A structurally-valid but thin or pointless capability is not the goal.

## 5. Install it into your own project

The folder is now a complete capability. Install it where it'll be used with [INSTALL.md](../INSTALL.md) — for a capability you authored locally, use its **local-source** path (copy the folder from disk into the registry; no GitHub fetch). That places the CLI on `PATH`, the stub into every session, and the project layer into the consuming repo, then runs `doctor` to confirm it's ready. You now own a working capability built from what you already had.

Contributing it to the public catalogue is entirely optional — a PR that adds the folder and lists it in the catalogue index. A private capability simply stays yours.
