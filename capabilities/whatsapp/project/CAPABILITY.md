# WhatsApp

The project's **WhatsApp access** — reaching its conversations through the `whatsapp` CLI over a self-hosted WAHA bridge. List chats, read or search a chat, look up a contact, export a chat's full history to JSON with voice/audio + photos downloaded, transcribe voice notes via Deepgram, and render an export as a markdown log. Reads only — sending is gated on a `mode: "send"` profile and not implemented yet.

> Template note: this capability installs under the **`whatsapp`** namespace (the CLI discovers `.capabilities/whatsapp/whatsapp.json` by that path). Replace this role paragraph with how *this* project actually uses WhatsApp (which chats it reads or exports, where synced data lands, whether transcription runs). Keep this file **lightweight** — role + pointers; the command surface is `whatsapp help`, not here.

## Interaction

Via the `whatsapp` CLI on `PATH` — run `whatsapp help` first (the self-documenting source of truth for the command surface, the credential cascade, the profile-discovery and override rules, the chat-id forms, the export JSON shape, and the exit codes), then `whatsapp doctor` to confirm the WAHA instance is reachable and the session is linked (exit 2 means missing/rejected creds or an unlinked session; pair it in the WAHA dashboard).

A single WAHA instance works from global creds alone (`WAHA_BASE_URL` + `WAHA_WHATSAPP_API_KEY`). For multiple identities or a project-local message store, add a [whatsapp.json](whatsapp.json) profile map (non-secret wiring; the key value stays in `.env`). See [identifiers.md](identifiers.md) for the chats and profiles this project addresses.

Synced conversations land under the capability's `messages_dir` (default `messages/`, i.e. `.capabilities/whatsapp/messages/`) and are git-ignored — data, not definition.

## Operational context (load on demand)

- [identifiers.md](identifiers.md) — the fixed values for this project: the chats it reads or exports, the profiles it defines, and the secret env keys each needs set in `.env`.
- [reference.md](reference.md) — the standing home for project-specific operational context (what each chat means here, export/transcription conventions, retention notes). Ships empty as a self-describing scaffold; populated as context accrues.

> If this project runs an automated read/export/transcribe flow (which chats sync where, on what trigger), that procedure is a **project routine** in `.routines/`, not part of this capability — point to it from the reference, don't embed it here.
