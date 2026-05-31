# Procedure: install a capability

You are an LLM agent. Someone pointed you at this repo and said, in effect, *"install `<capability>`."* This file is the procedure. Follow it by reasoning about the actual environment you're in — it is not a script, and there are no hardcoded paths to obey blindly. Ask before you guess. Never write a real secret.

## 0. Inputs

- The **capability name** (a top-level folder in this repo).
- Any **extra context** the user gave you (a target project, a non-default location, "global only").
- The **environment** you can observe: OS (macOS/Linux), single- vs multi-user, whether this is a personal laptop or a shared/public box, and whether you're sitting inside a project repo right now.

## 1. Read the manifest

Open `<capability>/manifest.md`. It declares the global artifacts, the project artifacts, the credentials, the template variables (classed `discoverable` / `must-confirm` / `leave-breadcrumb`), and any dependencies. Everything below is driven by it. Also skim [../TEMPLATE.md](../TEMPLATE.md) so you place things at the right altitude.

## 2. Decide scope — lean global

One flow, not two. A capability is **installed globally by default** (visible on the whole machine) and then *expanded* in whatever project wants it. So:

- Always do the **global layer** unless the user explicitly said project-only and the machine already has it.
- Do the **project layer** if you're inside a project now, or the user named one.

Prefer global because a globally-installed capability is declared *just enough to be visible* everywhere, and each project adds only its own context on top.

## 3. Global layer

For each global artifact in the manifest, find the right home on *this* machine by its **requirement**, not a fixed path:

- **The executable** must land somewhere on `PATH` and be executable (`chmod +x`). A proven spot is `~/bin/<name>`; on a multi-user box a shared `bin` may be more appropriate — reason about it.
- **The stub** must end up **auto-loaded into every agent session**. A proven method (Claude Code): drop it at `~/.claude/tools/<name>.md` and add the matching `@`-import line to the user-level `~/.claude/CLAUDE.md` so it's pulled in automatically. If the host agent isn't Claude Code, achieve the equivalent for that agent and note how.
- **Credentials**: copy `credentials.env.example` to its standard home (e.g. `~/.config/<name>/credentials.env`) **with empty values**. For services with a standard routing, the manifest names it. Fill values only from what the user provides — never invent or fetch a secret.

For each **must-confirm** template variable (e.g. a Windmill instance URL), **ask the user** and write their answer into the credentials/env file. For **leave-breadcrumb** variables, write the empty key in place so first real use surfaces it.

## 4. Project layer

If installing into a project, lay down the template instance from `<capability>/project/`:

- `project/capability.md` → `.claude/rules/capability/<NAME>.md` (auto-loaded).
- `project/assets/*` → `.assets/<namespace>/` (identifiers, reference, guide, optional `scripts/`).

Resolve placeholders: **discover** what you can (e.g. infer `<namespace>` from the project), **ask** for must-confirms, and **breadcrumb** the rest as clearly-marked placeholders. Connection-level values do **not** go in the markdown — they belong in the project's `.env` / `.env.local` as an override of the global credentials.

## 5. Dependencies

If the manifest declares dependencies on other capabilities, check whether they're installed; if not, offer to install them first (recurse into this procedure).

## 6. Verify and record

Run the capability's health check if it has one (`<name> doctor` / `<name> help`). Confirm the stub loads and the executable resolves. If the user chose to **deviate** from the template (different location, skipped a slot), record the deviation + their reason where a future audit will find it (see [audit.md](audit.md)) — so it reads as deliberate, not drift.

Report what you placed where, and what's still empty (the breadcrumbs the user must fill).
