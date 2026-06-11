# whatsapp — deviations

This file's sole purpose is to hold `whatsapp`'s deliberate, justified departures from the [SHEBANG](../../SHEBANG.md) defaults, kept apart so an audit reads them as choices, not drift (DOCTRINE — *[Deviations are allowed — and recorded](../../DOCTRINE.md#deviations-are-allowed--and-recorded)*). Each realizes the *intent* of the pattern in this capability's terms.

## Profile-indirect credentials; process env over file

The flat-key cascade is realized through a profile, the way the **mailbox** capability does it — because one machine may face several WhatsApp identities, each a `(WAHA instance + session)` with its own key. The project-side capability settings (`.capabilities/whatsapp/settings.json`, committed, non-secret wiring) maps each profile to its `base_url`, `session`, `mode`, optional `number`, and a `secret_env` — the **name** of the env var holding that instance's `X-Api-Key`. The CLI discovers the config by walking up from `$CLAUDE_PROJECT_DIR`/cwd to `.capabilities/whatsapp/settings.json` (`$WHATSAPP_CONFIG` overrides the path), selects a profile (`--profile`, else `$WHATSAPP_PROFILE`, else the config's `"default"` key, else the lone one), and resolves the secret by the name the profile gave.

The cascade's tiers and order are preserved (flag for non-secret overrides → project `.env(.local)` → user config → process env), but what they resolve is the **named** key, and the env-file load is **no-override** — a value already in the process environment is not replaced — so an injected secret wins over the file on a deployed box. With no settings file anywhere, the CLI falls back to single-instance env (`WAHA_BASE_URL` + `WAHA_WHATSAPP_API_KEY`), which keeps a one-instance install usable from any project without a config file.

The intent holds: deterministic resolution, identity-free code (the instance URL, key, session, and number all resolve from config/env — none baked in), secrets never on `argv` and never in a committed file.

## Planned, mode-gated send

`whatsapp` is a read tool today: `chats`, `messages`, `contact`, `export`, `transcribe`, and `render` cover its surface. `send` is present as a **documented placeholder** — it resolves config, passes the gate, and exits `6` (`not_implemented`) — marking where outbound messaging will land once the agent has its own send-capable WhatsApp identity.

Sending is gated on a profile's `mode`: only a `mode: "send"` profile may transmit, and the gate (`_require_send`) is enforced *before* any write, so a `read` identity is structurally incapable of sending even after `send` is implemented. This is recorded so an audit reads the unimplemented command as a deliberate, gated reservation — not an incomplete surface to fill. The intent holds: a stable exit-code contract (the placeholder and the gate both exit `6`, never the runtime's `1`), and a clear failure that names the remediation.
