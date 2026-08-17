# Capability source repositories

Use this guide to distinguish editable source workspaces, remote caches, installed payloads, and consuming-project envelopes.

The editable source lives at the path returned by `capabilities source path
<id>`. A remote source cache is read-only, an installed payload is manager-owned,
and a project capability envelope contains configuration rather than source.

The manager owns the sources registry. Use `source init`, `source clone`,
`source add`, and `source remove` to change it, and `source list` to inspect it.

The official integration checkout is registered from the primary checkout with:

```sh
./bin/capabilities source register-checkout official
```

A source repository is recognized by `capabilities.repo.json`, its vendored
`contract/`, and generated `.capability-source/catalog.json`.
