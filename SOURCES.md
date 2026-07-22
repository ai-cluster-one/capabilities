# Capability source repositories

A managed capability has one editable source and three derived copies. Keep
these roles separate:

| Role | Canonical path | Editable |
|---|---|---|
| Author workspace | `~/capabilities-sources/<source-id>/` | yes |
| Remote source cache | `~/.cache/capabilities/sources/<source-id>/` | no |
| Installed payload | `~/.capabilities/<name>/` | no |
| Consuming-project envelope | `<project>/capabilities/<name>/` | configuration only |

The workspace root is fixed by the manager. Source commits and pushes happen
only there. Registry payloads, remote caches, and project envelopes are never
capability source code.

The manager owns the source registry at
`~/.config/capabilities/sources.json` (or the platform's `XDG_CONFIG_HOME`
equivalent). Its stable shape is:

```json
{
  "sources": {
    "team": {
      "kind": "git",
      "url": "git@github.com:alice/team-capabilities.git",
      "ref": "main"
    }
  }
}
```

Use `source init`, `source clone`, `source add`, and `source remove` to change
it rather than editing it. `capabilities source list` is the supported way to
inspect both this registry and the built-in `official` source.

## Create and publish a source

```sh
capabilities source init personal \
  --remote git@github.com:alice/my-capabilities.git
cd ~/capabilities-sources/personal
capabilities new my-service --source personal
```

`source init` initializes git, registers the source, vendors the current
authoring contract, and creates host-readable authoring instructions. `new`
creates the canonical `capabilities/<name>/bin/<name>` bundle and stamps the
generated contract regions. Never edit those fenced regions by hand.

Before a commit:

```sh
capabilities source index personal
capabilities source check personal
capabilities install my-service --source personal
git add .
git commit -m "Add my-service capability"
git push -u origin main
```

The manager does not commit or push. It makes the source deterministic,
validates it, and records installation provenance.

## Consume a source

```sh
capabilities source add personal \
  git@github.com:alice/my-capabilities.git --ref main
capabilities source refresh personal
capabilities search --source personal
capabilities install my-service --source personal
```

Remote repositories use the caller's existing git/SSH authentication. The
manager stores no GitHub token. `search` reads the checked-in generated catalog
and never executes capability code. Installation independently checks the
catalog hash and runs the full capability audit in staging before atomically
replacing an installed payload.

To author an existing source on another machine, clone it into the same
canonical workspace shape instead of editing the manager cache:

```sh
capabilities source clone personal \
  git@github.com:alice/my-capabilities.git --ref main
capabilities source path personal
```

## Contract ownership

The source repository vendors `contract/preamble.py`; `contract/contract.json`
pins its checksum. The manager owns generated regions:

```sh
capabilities source sync personal
```

This deterministically updates the vendored authoring kit and restamps existing regions. There is
no force or skip-conformance flag. A tool that does not follow this contract is
still free to exist and run as an ordinary CLI, but the capabilities manager
will not install it as a managed capability.

## Repository shape

```text
capabilities.repo.json
AUTHORING.md
AGENTS.md
CLAUDE.md
contract/
  preamble.py
  contract.json
capabilities/
  <name>/
    bin/<name>
    guides/          optional
    service/         optional
    deviations.md   optional
.capability-source/
  catalog.json       generated
```

The executable manifest remains the source of truth. `catalog.json` is only a
generated discovery index and is rejected when its summaries or payload hashes
are stale.
