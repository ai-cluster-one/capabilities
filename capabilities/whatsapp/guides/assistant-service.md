# WhatsApp — assistant service

How the bundled `assistant` service holds one account's connection and captures every message live, and how it is operated.

## What it does

The service is one long-running process per account. It opens the account's linked-device session through the same engine, capture path, account check and Postgres store every verb uses, and keeps it open. Each message the account receives or sends is written to the store as it arrives, with sync type `LIVE`; history-sync chunks the server pushes are stored as they are by any verb. The service sends nothing and answers nothing.

## Before it starts

- The project enables the capability explicitly: `capabilities enable whatsapp --project`. Global availability grants CLI use, not a project daemon.
- `whatsapp service init` seeds the service settings records (`capabilities/whatsapp/service/settings.json` in files mode). The settings take two keys: `connection`, the connection the service runs (null takes the registry's default), and `environment`, a label the service reports. Any other key is refused by `start`, `run`, `doctor` and `reload`.
- The machine's store is configured (`capabilities store show`). Without it the service refuses to start and `service doctor` reports `store_not_configured`.
- The connection is an in-house one with a linked device.

## Operating it

- `whatsapp service start` launches the listener detached and returns once it has taken the account lock and published its owner record. It refuses, each with its own code, when the project has not enabled the capability (`project_enable_required`), the service is not initialized (`service_not_initialized`), the store is not configured (`store_not_configured`), or a listener already owns the account (`service_running`).
- `whatsapp service run` runs the same listener in the foreground; it is the deploy command.
- `whatsapp service status` reports state, pid, whether it is connected, uptime, the last event and message times, messages captured, reconnects and the last error.
- `whatsapp service logs` tails the listener's log. `whatsapp service doctor` proves settings, store, link and ownership without opening a second connection.
- `whatsapp service reload` validates the settings, then signals the listener alone to re-read them; a rejected reload leaves the previous settings in force. A change of connection takes effect only on restart.
- `whatsapp service stop` ends the listener cleanly; the account lock is released when it exits.

## One account, one connection

The service holds the account lock every connected verb takes, for its whole life. While it runs, a verb that would connect on that account exits 8 `session_busy`, and the message names the service and the project it runs from. Reads the store answers keep working: `status`, `health`, `chats` without `--fresh`, and `messages` when the store covers the request or with `--rounds 0`.

## Disconnects and removal

When the connection drops, the service closes the session and opens a new one after a wait that starts at 2 seconds and doubles to at most 60, reset once a connection has held for a minute. The server's offline queue drains on each reconnect, so what arrived while the wire was down is captured then. `SIGUSR1` drops the socket on purpose and takes the same path, which is how a reconnect is exercised.

A device removed from the phone is final: the service stops reconnecting, stays up in state `logged_out`, and `status` and `doctor` say so. The remedy is to stop it, re-link with `whatsapp pair --recreate --yes`, and start it again.

If the engine does not release its connection when a session closes, the process exits rather than let a second client exist on the device.

## Files

The control files are machine-local, under the account's state home: `service/owner.json` (who holds the account, with the launch nonce `start` waits for), `service/health.json`, `service/daemon.pid` and `service/daemon.log`. Captured messages go only to the store.
