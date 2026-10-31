# skyscanner — change log

## 2026-10-31 — `skyscanner help` names the agent's vault in the credential cascade

Bare `skyscanner help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes. The `oxylabs` guide says the same: `connection add` keeps the Oxylabs password in the agent's vault by default, in the project's `.env.local` with `--file`, and in `credentials.env` with `--machine`.
