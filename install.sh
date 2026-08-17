#!/bin/sh
# capabilities bootstrap — installs ONE thing: the `capabilities` manager.
#
#   curl -fsSL https://raw.githubusercontent.com/ai-cluster-one/capabilities/main/install.sh | sh
#
# By default this installs the latest manager from `main`. Overrides:
#   CAPABILITIES_TAG     fetch a different ref
#   CAPABILITIES_HOME    registry root            (default ~/.capabilities)
#   CAPABILITIES_BIN     PATH dir for the symlink (default ~/.local/bin or
#                        ~/bin, whichever exists and is on PATH)
#   CAPABILITIES_SHA256  optionally verify the fetched manager script
#
# The installer prints its plan before acting, asks on a TTY, and is
# idempotent — re-running refreshes the manager in place.

set -eu

TAG_DEFAULT="main"

TAG="${CAPABILITIES_TAG:-$TAG_DEFAULT}"
SHA256="${CAPABILITIES_SHA256:-}"

REPO="https://raw.githubusercontent.com/ai-cluster-one/capabilities/$TAG"
CAP_HOME="${CAPABILITIES_HOME:-$HOME/.capabilities}"
MANAGER_DIR="$CAP_HOME/.manager"
MANAGER="$MANAGER_DIR/capabilities"

err() { printf '%s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || err "curl is required"
command -v uv >/dev/null 2>&1 || printf 'NOTE: `uv` is not on PATH — the manager needs it to run.\n      Install it first: https://docs.astral.sh/uv/getting-started/installation/\n' >&2

# bin dir: explicit > first of ~/.local/bin, ~/bin already on PATH > ~/.local/bin
if [ -n "${CAPABILITIES_BIN:-}" ]; then
    BIN_DIR="$CAPABILITIES_BIN"
else
    BIN_DIR="$HOME/.local/bin"
    for cand in "$HOME/.local/bin" "$HOME/bin"; do
        [ -d "$cand" ] || continue
        case ":$PATH:" in *":$cand:"*) BIN_DIR="$cand"; break;; esac
    done
fi

printf 'capabilities bootstrap — the plan:\n'
printf '  fetch    %s/bin/capabilities\n' "$REPO"
if [ -n "$SHA256" ]; then printf '  verify   sha256 %s\n' "$SHA256"; else printf '  verify   (no checksum pinned for ref %s)\n' "$TAG"; fi
printf '  place    %s\n' "$MANAGER"
printf '  symlink  %s/capabilities\n' "$BIN_DIR"
if (exec < /dev/tty) 2>/dev/null; then
    printf 'Proceed? [Y/n] '
    read -r answer < /dev/tty || answer=""
    case "$answer" in n*|N*) err "aborted";; esac
fi

tmp="$(mktemp)"
assets_tmp="$(mktemp -d)"
trap 'rm -f "$tmp"; rm -rf "$assets_tmp"' EXIT
curl -fsSL "$REPO/bin/capabilities" -o "$tmp" || err "fetch failed: $REPO/bin/capabilities"
for asset in SHEBANG.md DOCTRINE.md TEMPLATE.md SOURCES.md ROUTINES.md \
  contract/preamble.py guides/authoring.md guides/conforming.md \
  guides/contract.md guides/dev.md guides/grooming.md guides/publishing.md \
  guides/repositories.md guides/sanitizing.md; do
    mkdir -p "$assets_tmp/$(dirname "$asset")"
    curl -fsSL "$REPO/$asset" -o "$assets_tmp/$asset" || err "fetch failed: $REPO/$asset"
done

if [ -n "$SHA256" ]; then
    actual="$( (shasum -a 256 "$tmp" 2>/dev/null || sha256sum "$tmp") | cut -d' ' -f1 )"
    [ "$actual" = "$SHA256" ] || err "checksum mismatch: expected $SHA256, got $actual"
fi
head -1 "$tmp" | grep -q "uv run" || err "fetched file does not look like the manager script"

mkdir -p "$MANAGER_DIR" "$BIN_DIR"
CAPABILITIES_BOOTSTRAP_MANAGER="$MANAGER" \
CAPABILITIES_BOOTSTRAP_BIN="$BIN_DIR/capabilities" \
CAPABILITIES_BOOTSTRAP_STAGE="$tmp" \
CAPABILITIES_BOOTSTRAP_ASSETS="$assets_tmp" \
python3 - <<'PY'
import fcntl
import json
import os
import shutil
from pathlib import Path

manager = Path(os.environ["CAPABILITIES_BOOTSTRAP_MANAGER"])
link = Path(os.environ["CAPABILITIES_BOOTSTRAP_BIN"])
stage = Path(os.environ["CAPABILITIES_BOOTSTRAP_STAGE"])
assets = Path(os.environ["CAPABILITIES_BOOTSTRAP_ASSETS"])
state_home = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
lock_path = state_home / "capabilities" / "manager-mutation.lock"
lock_path.parent.mkdir(parents=True, exist_ok=True)
with lock_path.open("a+") as lock:
    lock_path.chmod(0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    takeover_root = state_home / "telegram"
    active = []
    if takeover_root.is_dir():
        for path in takeover_root.glob("*/control/takeover.json"):
            try:
                lease = json.loads(path.read_text())
            except (OSError, ValueError):
                active.append(str(path))
                continue
            if not isinstance(lease, dict) or lease.get("state") != "restored":
                active.append(str(path))
    if active:
        raise SystemExit("manager bootstrap blocked by active Telegram takeover: " + ", ".join(active))
    manager.parent.mkdir(parents=True, exist_ok=True)
    temporary = manager.with_name(manager.name + ".bootstrap-tmp")
    shutil.copy2(stage, temporary)
    temporary.chmod(0o755)
    os.replace(temporary, manager)
    for rel in ("SHEBANG.md", "DOCTRINE.md", "TEMPLATE.md", "SOURCES.md",
                "ROUTINES.md", "contract/preamble.py", "guides/authoring.md",
                "guides/conforming.md", "guides/contract.md", "guides/dev.md",
                "guides/grooming.md", "guides/publishing.md",
                "guides/repositories.md", "guides/sanitizing.md"):
        target = manager.parent / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        staged_target = target.with_name(target.name + ".bootstrap-tmp")
        shutil.copy2(assets / rel, staged_target)
        os.replace(staged_target, target)
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary_link = link.with_name(link.name + ".bootstrap-tmp")
    temporary_link.unlink(missing_ok=True)
    temporary_link.symlink_to(manager)
    os.replace(temporary_link, link)
PY

if [ -f "./capabilities.repo.json" ] && [ -x "./bin/capabilities" ]; then
    ./bin/capabilities source register-checkout official >/dev/null
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) printf 'NOTE: %s is not on PATH — add it so `capabilities` resolves by name.\n' "$BIN_DIR" >&2;;
esac

printf 'installed: %s -> %s\n' "$BIN_DIR/capabilities" "$MANAGER"
printf 'next:      capabilities list · capabilities install <name> · capabilities init --codex (Codex context wiring) or --claude (Claude Code project)\n'
