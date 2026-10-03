# deployment — change log

## 2026-10-03 — Container images install their capabilities allowed

The generated Dockerfile installs each capability in `deployment/capabilities.lock` with `capabilities install <name> --allow`, because a capability new to a machine otherwise arrives quarantined and an image is one agent's whole environment. Rebuild an image with `deployment sync` to pick it up.
