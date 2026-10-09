# slack — change log

## 2026-10-09 — The service reads its settings straight from files

The service reads its settings from the project's `capabilities/slack/service/settings.json` over the config home's `slack/service/settings.json`, key by key, with the same values as before, and `service/store.py` is no longer shipped.
