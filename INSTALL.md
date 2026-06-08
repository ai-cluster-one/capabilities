# Install a capability

You are an agent. Someone gave you a link to this file and said, in effect, *"install a capability for me."* This file is the whole installer — there is no script to run and nothing to clone. You read it, ask which capability, and place a handful of files by reasoning about the machine you're on. Ask before you guess. Never write a real secret.

The repository is `ai-cluster-one/capabilities` on `main`. Raw files are at
`https://raw.githubusercontent.com/ai-cluster-one/capabilities/main/<path>`.

## 0. Identify your host

A capability is surfaced into a session differently per agent host. Detect which one you are and follow that section. Today this file covers **Claude Code**; another host adds its own section.

If you are **Claude Code**, continue below.

## 1. Pick the capability

The capability comes from one of two sources — establish which first:

- **From the catalogue.** List it and ask the user which one to install (and whether they want the project layer too — see step 3):

```
curl -fsSL https://raw.githubusercontent.com/ai-cluster-one/capabilities/main/README.md
```

The capability index there names each folder under `capabilities/`; take the chosen name as `<name>`.

- **From a local folder.** A capability the user authored themselves with [procedures/package.md](procedures/package.md) lives on disk, not in the catalogue. There is nothing to list — take `<name>` and the folder path straight from that folder, and note the path (step 2a copies from it).

`<name>` carries through the rest of this file.

## 2. Global layer — the machine, once per capability

The capability lives, immutable, in a per-host-neutral registry at `~/.capabilities/<name>/`; from there its CLI is symlinked onto PATH, and its stub is `@`-imported into every session via the host's global `CLAUDE.md`.

**a. Place the capability folder into the registry.** No clone, no working tree — just the one folder. It comes from one of two sources:

- **From the catalogue** — a capability listed in this repo:

```
mkdir -p "$HOME/.capabilities"
curl -fsSL https://codeload.github.com/ai-cluster-one/capabilities/tar.gz/refs/heads/main \
  | tar -xz -C "$HOME/.capabilities" --strip-components=2 capabilities-main/capabilities/<name>
```

(If the repo is private, use `gh` with the user's auth instead.) Re-running this is how an update lands — it overwrites the immutable folder in place.

- **From a local folder** — a capability the user authored themselves with [procedures/package.md](procedures/package.md), living on disk and not in the public catalogue. Copy it straight in:

```
mkdir -p "$HOME/.capabilities"
cp -R "<path-to-capability-folder>/." "$HOME/.capabilities/<name>/"
```

The registry copy is the install image either way; nothing downstream cares which source filled it.

**b. Put the CLI on PATH.** The executable is at `~/.capabilities/<name>/bin/<name>`. Make it executable and symlink it into a directory already on `PATH` (prefer `~/bin`, else `~/.local/bin`; if neither is on `PATH`, create `~/.local/bin`, symlink there, and tell the user to add it to `PATH` once):

```
chmod +x "$HOME/.capabilities/<name>/bin/<name>"
ln -sf "$HOME/.capabilities/<name>/bin/<name>" "$HOME/bin/<name>"
```

**c. Put the stub into every session.** The stub surfaces by `@`-import. Install it to `~/.claude/tools/<name>.md` (a symlink to the immutable source — the registry stays the one home), then list it once in the host's global `~/.claude/CLAUDE.md` so the harness expands it inline every session. The stub is **awareness only**, no front-matter — what the tool is and to run `<name> help`; `<name> doctor` answers whether it is ready here.

```
mkdir -p "$HOME/.claude/tools"
ln -sf "$HOME/.capabilities/<name>/stub.md" "$HOME/.claude/tools/<name>.md"
# then, once, if absent, add this line to the host's global ~/.claude/CLAUDE.md:
#   @./tools/<name>.md
```

**d. Credentials.** Copy the example to the standard home **with empty values**, then resolve the capability's template variables: ask for **must-confirm** values (a self-hosted URL, a token) and write them; leave **breadcrumb** keys empty in place. Read `~/.capabilities/<name>/manifest.md` for the variable classes.

```
mkdir -p "$HOME/.config/<name>"
cp -n "$HOME/.capabilities/<name>/credentials.env.example" "$HOME/.config/<name>/credentials.env"
```

A real secret only ever lands in `~/.config/<name>/credentials.env` (or a project `.env`), never in the registry or any committed file.

## 3. Project layer — the consuming project, if there is one

The CLI is centralized (step 2) — a project never copies it; it calls `<name>` by PATH. The project layer is only the lightweight, project-specific knowledge, surfaced by two project-side loaders (one for capabilities, one for routines).

**a. Lay down the assets.** Copy the project template into `<project>/.capabilities/<ns>/` (infer `<ns>` from the project; confirm if unsure). The template `~/.capabilities/<name>/project/` holds the entry file `CAPABILITY.md` (a front-matter-free awareness stub — role prose + pointers) plus `identifiers.md` and a self-describing `reference.md` scaffold, and an optional `scripts/`:

```
mkdir -p "<project>/.capabilities/<ns>"
cp -R "$HOME/.capabilities/<name>/project/." "<project>/.capabilities/<ns>/"
```

Resolve placeholders: discover what you can (the namespace), ask for must-confirms, breadcrumb the rest as clearly-marked placeholders. Connection-level values go in the project's `.env` / `.env.local`, never the markdown.

**b. Ensure the loader exists.** A `SessionStart` hook that echoes stub content into the session is capped at ~10k characters, so the project surfaces its `.capabilities/` registry a different way: a capability-agnostic generator at `.claude/hooks/build-capabilities-rule.sh` writes `.claude/rules/CAPABILITIES.md` — a manifest of `@`-imports, one per discovered `CAPABILITY.md`. The harness auto-loads the rule file and expands the imports inline, so the stubs land in context **uncapped**. If the generator isn't there, create it with exactly this content (it is the same for every project — drop a capability folder and it loads next session, with no edit here):

```bash
#!/bin/bash
# Generate the capabilities rule file — the plug-in loader, rules-tier edition.
# A SessionStart hook echoing stub content into the session is capped at ~10k
# characters, so instead this writes .claude/rules/CAPABILITIES.md as a manifest
# of @-imports — one per discovered .capabilities/<ns>/CAPABILITY.md. The harness
# auto-loads the rule file and expands the imports inline, so the stubs land in
# context uncapped. Source of truth stays in each CAPABILITY.md; this file is
# generated, never hand-edited.
#
# Discovery is by folder glob (drop a folder, it loads — no registry, no list).
# Regeneration is deterministic: identical capabilities produce identical bytes,
# so the committed manifest only diffs when a capability is added or removed.

CAP_DIR="$CLAUDE_PROJECT_DIR/.capabilities"
RULE_FILE="$CLAUDE_PROJECT_DIR/.claude/rules/CAPABILITIES.md"

# SessionStart delivers a JSON payload on stdin with a "source" field
# (startup | resume | clear | compact). Read it defensively and fail open — an
# unparseable payload still regenerates.
input=$(cat 2>/dev/null)
source=$(printf '%s' "$input" | sed -n 's/.*"source"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
case "$source" in
  startup|resume|compact|clear|"") : ;;  # regenerate
  *) exit 0 ;;
esac

mkdir -p "$(dirname "$RULE_FILE")"

# Build into a temp file, then move into place atomically so a concurrent reader
# never sees a half-written rule.
tmp="$(mktemp "${RULE_FILE}.XXXXXX")" || exit 0
trap 'rm -f "$tmp"' EXIT

{
  echo "# Capabilities (installed)"
  echo ""
  echo "Each import below is a capability stub — its role plus pointers to load on demand. Routing context, not hard directives. Generated at session start from .capabilities/<ns>/CAPABILITY.md; do not hand-edit."
  echo ""

  FOUND=0
  if [ -d "$CAP_DIR" ]; then
    while IFS= read -r f; do
      [ -f "$f" ] || continue
      # @-import path is relative to this rule file (.claude/rules/): climb two
      # levels to the project root, then descend into .capabilities/.
      echo "@../../.capabilities/$(basename "$(dirname "$f")")/CAPABILITY.md"
      FOUND=$((FOUND + 1))
    done < <(find "$CAP_DIR" -mindepth 2 -maxdepth 2 -name 'CAPABILITY.md' | sort)
  fi

  if [ "$FOUND" -eq 0 ]; then
    echo "No capabilities installed."
  fi
} > "$tmp"

mv -f "$tmp" "$RULE_FILE"
trap - EXIT
exit 0
```

Then `chmod +x .claude/hooks/build-capabilities-rule.sh`. The generated `.claude/rules/CAPABILITIES.md` is deterministic and safe to commit.

**c. Add the routines loader.** A capability's primary consumer is the **routine** — a project's repeatable procedure (see [ROUTINES.md](ROUTINES.md)) — and routines surface by the same mechanism: a sibling generator at `.claude/hooks/build-routines-rule.sh` writes `.claude/rules/ROUTINES.md`, one line per `.routines/*.md` (a markdown link plus the routine's front-matter `description`), so a routine **declares itself into every session as soon as its file exists**, its body staying on-demand. Capability-agnostic and project-wide — set up once, beside the capabilities loader. Create it with exactly this content:

```bash
#!/bin/bash
# Generate the routines rule file — the routine index, rules-tier edition.
# Instead of listing routines on stdout (capped at 10k per hook), this writes
# .claude/rules/ROUTINES.md: one line per discovered .routines/*.md, each a
# markdown link to the routine file plus its front-matter description. The recipe
# bodies stay on-demand — only this index is always-on — so a routine names where
# it lives and what it does, and the model loads the file when it runs it.
# Source of truth is each routine's YAML front matter; this file is generated,
# never hand-edited.
#
# Discovery is by glob (no registry: add a file, it shows up next session).
# Regeneration is deterministic — identical routines produce identical bytes, so a
# committed index stays clean and only diffs when a routine is added, removed, or
# its name/description changes.

ROUTINES_DIR="$CLAUDE_PROJECT_DIR/.routines"
RULE_FILE="$CLAUDE_PROJECT_DIR/.claude/rules/ROUTINES.md"

# SessionStart delivers a JSON payload on stdin with a "source" field
# (startup | resume | clear | compact). Read it defensively and fail open — an
# unparseable payload still regenerates.
input=$(cat 2>/dev/null)
source=$(printf '%s' "$input" | sed -n 's/.*"source"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
case "$source" in
  startup|resume|compact|clear|"") : ;;  # regenerate
  *) exit 0 ;;
esac

# Build into a temp file, then move into place atomically so a concurrent reader
# never sees a half-written rule.
tmp="$(mktemp "${RULE_FILE}.XXXXXX")" || exit 0
trap 'rm -f "$tmp"' EXIT

{
  echo "# Routines (repeatable procedures)"
  echo ""
  echo "Each is a self-contained recipe. Load its file when you need to run that procedure. This file is generated at session start from each routine's YAML front matter; do not hand-edit."
  echo ""

  FOUND=0
  if [ -d "$ROUTINES_DIR" ]; then
    while IFS= read -r f; do
      # name + description come from the first YAML front-matter block.
      name=$(awk '/^---[ \t]*$/{c++; next} c==1 && /^name:/{sub(/^name:[ \t]*/,""); print; exit}' "$f")
      desc=$(awk '/^---[ \t]*$/{c++; next} c==1 && /^description:/{sub(/^description:[ \t]*/,""); print; exit}' "$f")
      # link target is project-root-relative so it resolves from the working dir,
      # regardless of where this rule file sits.
      rel=${f#"$CLAUDE_PROJECT_DIR/"}
      [ -z "$name" ] && name=$(basename "$f" .md)
      [ -z "$desc" ] && desc="(no description)"
      echo "- **[$name]($rel)** — $desc"
      FOUND=$((FOUND + 1))
    done < <(find "$ROUTINES_DIR" -name '*.md' | sort)
  fi

  if [ "$FOUND" -eq 0 ]; then
    echo "No routines defined yet."
  fi
} > "$tmp"

mv -f "$tmp" "$RULE_FILE"
trap - EXIT
exit 0
```

Then `chmod +x .claude/hooks/build-routines-rule.sh`. The generated `.claude/rules/ROUTINES.md` is deterministic and safe to commit; with no `.routines/` folder yet it simply reads "No routines defined yet." and fills in as routine files land.

**d. Wire the generators to run.** The project's `.claude/settings.json` runs both generators at `SessionStart`. **Merge — never overwrite.** Read the file (it may carry `permissions`, `enabledPlugins`, sibling hooks), then converge it on this, preserving everything else:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [
        { "type": "command", "command": "\"$CLAUDE_PROJECT_DIR/.claude/hooks/build-capabilities-rule.sh\"" },
        { "type": "command", "command": "\"$CLAUDE_PROJECT_DIR/.claude/hooks/build-routines-rule.sh\"" }
      ] }
    ]
  }
}
```

- no `settings.json` → create it with this block.
- has it, no `SessionStart` → add the `SessionStart` array.
- has `SessionStart` → append any missing command to the existing `hooks` array, keeping any siblings.
- a command already present → leave it.

Each command is the quoted, `$CLAUDE_PROJECT_DIR`-relative path so it resolves on any machine and survives paths with spaces.

Wiring the generators and settings is once per project. Every capability after the first is just step 3a — drop the `.capabilities/<ns>/` folder; the generator picks it up next session. Likewise, a new routine is just a file in `.routines/` — the loader declares it next session, no wiring touched.

## 4. Post-install — run the capability's setup step, if it declares one

Some capabilities need a one-time setup action once their files are in place — e.g. provisioning a remote workspace. Read `~/.capabilities/<name>/manifest.md`: if it has a **Post-install** section, surface its command(s) and **offer to run them** — don't auto-run, since these can mutate a remote service. Honour any caveat the section states (a step may be only partially completable until a separate dependency exists — run what you can, name what's deferred). The action is declared idempotent, so it's safe for the user to defer and re-run later.

A capability with no Post-install section needs nothing here.

## 5. Verify and report

- Run the capability's health check (`<name> doctor` or `<name> help`) to confirm the CLI resolves on PATH and finds its credentials.
- Confirm `~/.claude/tools/<name>.md` resolves and its `@./tools/<name>.md` line is in the host `CLAUDE.md` (the stub surfaces next session).
- If you're in a project, confirm `.claude/hooks/build-capabilities-rule.sh` is executable, the `SessionStart` hook names it, and running it once produces `.claude/rules/CAPABILITIES.md` with an `@`-import for the capability.
- Confirm `.claude/hooks/build-routines-rule.sh` is executable and named in the same `SessionStart` hook; running it produces `.claude/rules/ROUTINES.md` — an index line per `.routines/*.md`, or "No routines defined yet." when none exist.

Report what you placed where, and which credential keys are still empty (the breadcrumbs the user must fill).
