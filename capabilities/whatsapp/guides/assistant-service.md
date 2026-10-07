# WhatsApp — assistant service

How the bundled `assistant` service holds one account's connection, captures every message live and sends what is queued for the account, and how it is operated.

## What it does

The service is one long-running process per account. It opens the account's linked-device session through the same engine, capture path, account check and Postgres store every verb uses, and keeps it open. Each message the account receives or sends is written to the store as it arrives, with sync type `LIVE`; history-sync chunks the server pushes are stored as they are by any verb. It sends the messages `whatsapp send` queues for the account, and answers nothing on its own.

## Before it starts

- The project enables the capability explicitly: `capabilities enable whatsapp --project`. Global availability grants CLI use, not a project daemon.
- `whatsapp service init` seeds the service settings records (`capabilities/whatsapp/service/settings.json` in files mode). The settings take three keys: `connection`, the connection the service runs (null takes the registry's default), `environment`, a label the service reports, and `send_rate`, the most messages it sends a minute (1 to 120; null means 20). Any other key is refused by `start`, `run`, `doctor` and `reload`.
- The machine's store is configured (`capabilities store show`). Without it the service refuses to start and `service doctor` reports `store_not_configured`.
- The connection is an in-house one with a linked device.

## Operating it

- `whatsapp service start` launches the listener detached and returns once it has taken the account lock and published its owner record. It refuses, each with its own code, when the project has not enabled the capability (`project_enable_required`), the service is not initialized (`service_not_initialized`), the store is not configured (`store_not_configured`), or a listener already owns the account (`service_running`).
- `whatsapp service run` runs the same listener in the foreground; it is the deploy command.
- `whatsapp service status` reports state, pid, whether it is connected, uptime, the last event and message times, messages captured, messages sent and send failures, the send rate, the spool depth and the store error, reconnects and the last error.
- `whatsapp service logs` tails the listener's log. `whatsapp service doctor` proves settings, store, link and ownership without opening a second connection.
- `whatsapp service reload` validates the settings, then signals the listener alone to re-read them; a rejected reload leaves the previous settings in force. A change of connection takes effect only on restart.
- `whatsapp service stop` ends the listener cleanly; the account lock is released when it exits.

## One account, one connection

The service holds the account lock every connected verb takes, for its whole life. While it runs, `send` on that account writes its message as a pending row and returns the service's answer once the service has sent it; any other verb that would connect on that account exits 8 `session_busy`, and the message names the service and the project it runs from. Reads the store answers keep working: `status`, `health`, `chats` without `--fresh`, and `messages` when the store covers the request or with `--rounds 0`.

## Disconnects and removal

When the connection drops, the service closes the session and opens a new one after a wait that starts at 2 seconds and doubles to at most 60, reset once a connection has held for a minute. The server's offline queue drains on each reconnect, so what arrived while the wire was down is captured then. `SIGUSR1` drops the socket on purpose and takes the same path, which is how a reconnect is exercised.

A device removed from the phone is final: the service stops reconnecting, stays up in state `logged_out`, and `status` and `doctor` say so. The remedy is to stop it, re-link with `whatsapp pair --recreate --yes`, and start it again.

If the engine does not release its connection when a session closes, the process exits rather than let a second client exist on the device.

## Sending

The service claims the pending rows of its account oldest first and sends each, at most `send_rate` a minute; the rest wait. Each row ends `sent` with WhatsApp's id or `failed` with the reason. A row a previous run claimed and never answered for is failed at start as `interrupted_delivery_unknown` and never sent again. The sending guide holds the whole model.

## When the store is unreachable

WhatsApp hands a message to the service once. A capture the store cannot take because it cannot be reached is therefore appended to `service/capture-spool.jsonl` and written into the store from there once it answers again, and at the next start. While the spool holds anything, new captures join the back of it, so the store receives everything once and in the order it arrived; the spool is empty again as soon as the store is. A spooled capture the store refuses for its content rather than for being down is moved to `service/capture-spool.rejected.jsonl` so it cannot hold back what follows. `service status` reports the spool depth and the store error. Sending pauses while the spool holds anything.

The spool holds an outage and nothing else. A store that is unreachable when the service starts keeps the service from connecting at all, which loses nothing: WhatsApp keeps the device's queue until it connects.

## Files

The control files are machine-local, under the account's state home: `service/owner.json` (who holds the account, with the launch nonce `start` waits for), `service/health.json`, `service/daemon.pid`, `service/daemon.log`, and the capture spool while the store is unreachable. Captured messages go to the store, and to the spool only until the store answers.
