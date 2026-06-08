# WhatsApp — identifiers

All fixed, **non-secret structural** values for this project's WhatsApp setup: the chats it addresses and the profiles it defines. Pure lookup; the operating model and command surface are in `whatsapp help`.

> Template note: fill these at install by discovering them against this project's own account. With a session resolved, `whatsapp chats --limit N` lists chats with their ids; record the durable ones this project reads or exports. Profile **wiring** (`base_url`, `session`, `mode`, `secret_env`) lives in `whatsapp.json`, not here — this file holds the roster + chat ids + the secret env keys to set. **No secrets here** — the API key, dashboard password, and Deepgram key live in env / the user-config dir, never in this file.

## Chats

A chat is addressed by its WhatsApp id: a DM is `<number>@c.us`, a group is `<group-id>@g.us` (`whatsapp help` documents the forms). Record the durable role each one plays for this project.

| Role | Chat id |
|---|---|
| `<the chat this project reads>` | `<number>@c.us` |
| `<the chat this project exports>` | `<group-id>@g.us` |

## Profiles

Defined only in **profile mode** (a `whatsapp.json` exists). A profile is one WhatsApp identity = a `(WAHA instance + session)`; wiring lives in `whatsapp.json`, this is the roster + what each is for. A single-instance install (global `WAHA_BASE_URL` + `WAHA_WHATSAPP_API_KEY`, no `whatsapp.json`) has no profiles — leave this empty.

| Profile | Role in this project | Mode | Secret env key (set in `.env`) |
|---|---|---|---|
| `<profile-name>` | `<what this project uses it for>` | `read` | `<the secret_env this profile names>` |

## Connection (values in env / whatsapp.json, never here)

Secrets resolve through the credential cascade `whatsapp help` documents; only the non-secret chat ids and profile roster above belong in this file. The keys to set, for reference:

| Env var | Holds | Where |
|---|---|---|
| `WAHA_BASE_URL` | the WAHA instance URL (single-instance mode) | project `.env` or `~/.config/whatsapp/credentials.env` |
| `WAHA_WHATSAPP_API_KEY` (or a profile's `secret_env`) | the instance `X-Api-Key` | project `.env` or `~/.config/whatsapp/credentials.env` |
| `WAHA_DASHBOARD_USERNAME` / `WAHA_DASHBOARD_PASSWORD` | optional dashboard basic auth | project `.env` or `~/.config/whatsapp/credentials.env` |
| `DEEPGRAM_API_KEY` | Deepgram key (only for `whatsapp transcribe`) | project `.env` or `~/.config/whatsapp/credentials.env` |

Global tool: `whatsapp` on `PATH` (`whatsapp help` is the source of truth for the CLI surface).
