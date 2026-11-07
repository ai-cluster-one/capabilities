# skyscanner — change log

## 2026-11-07 — The site rotation state made under a connection's former name is found again under its id

The site rotation state (`domains.json`) is kept under `<state>/<connection id>/`. Where that does not exist and the store's map names the connection's former name, the folder made under that name is moved to the id once, saying so on stderr, so what each site last answered survives the store's move to connection ids. Declares `capabilities-contract>=0.44,<1`, the library that keeps the map and moves the folder.

`doctor` names a connection by its label where its domains or Oxylabs settings are malformed or its password variable is unset, where it named the id.

## 2026-10-31 — `skyscanner` carries its own icon

The bundle holds its icon as `icon.png`, Skyscanner's own symbol, taken unaltered from the square PNG Skyscanner publishes on its partner site (`https://www.partners.skyscanner.net/hubfs/Skyscanner_Logo_Symbol_SkyBlue_RGB-202x202.png`, 202 x 202). The mark is a trademark of Skyscanner, used here only to identify the service the capability reaches. `capabilities icon skyscanner` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-31 — `skyscanner help` names the agent's vault in the credential cascade

Bare `skyscanner help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes. The `oxylabs` guide says the same: `connection add` keeps the Oxylabs password in the agent's vault by default, in the project's `.env.local` with `--file`, and in `credentials.env` with `--machine`.
