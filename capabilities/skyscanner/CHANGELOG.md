# skyscanner — change log

## 2026-11-22 — `skyscanner help` is the menu, and `skyscanner help <verb>` one verb's whole contract

`skyscanner help` printed the whole reference, about 21 KB with flags on 50 of its lines, and `skyscanner help <anything>`, an unknown word included, printed it again at exit 0. It is now the short complete menu of SHEBANG.md, "Agent-first help is the surface", about 5 KB: what the capability is, the model in brief, where the connection model lives, every verb on one line saying what it answers, the write gate, the quirks and the exit codes, then the contract verbs the library renders, with no flag on any line. `skyscanner help <verb>` prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, the text the old help held for it, its output, exit codes and an example - for `doctor`, `connections`, `places`, `search` and `calendar`. The whole site rotation and bot-protection model, SITES AND THE BLOCK, and the exit-code table are on the pages of `doctor`, `places`, `search` and `calendar`; the `--market`, `--currency` and `--locale` overrides and connection selection are on the pages of `places`, `search` and `calendar`. CONNECTIONS moved to `help connections`, after the library's "Where connections live" and "Where a secret resolves", and the declared connection fields are the library's `help connection`; the old contract commands list is the library's, and the examples moved to the pages of their verbs. An unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `skyscanner help`. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, answer, JSON key or exit code changes.

## 2026-11-16 — `skyscanner` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), Skyscanner, and Estonian (`et`), Skyscanner, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `skyscanner` declares its title, Skyscanner

The manifest carries `title`, `Skyscanner`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `skyscanner`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-11-07 — The site rotation state made under a connection's former name is found again under its id

The site rotation state (`domains.json`) is kept under `<state>/<connection id>/`. Where that does not exist and the store's map names the connection's former name, the folder made under that name is moved to the id once, saying so on stderr, so what each site last answered survives the store's move to connection ids. Declares `capabilities-contract>=0.44,<1`, the library that keeps the map and moves the folder.

`doctor` names a connection by its label where its domains or Oxylabs settings are malformed or its password variable is unset, where it named the id.

## 2026-10-31 — `skyscanner` carries its own icon

The bundle holds its icon as `icon.png`, Skyscanner's own symbol, taken unaltered from the square PNG Skyscanner publishes on its partner site (`https://www.partners.skyscanner.net/hubfs/Skyscanner_Logo_Symbol_SkyBlue_RGB-202x202.png`, 202 x 202). The mark is a trademark of Skyscanner, used here only to identify the service the capability reaches. `capabilities icon skyscanner` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-31 — `skyscanner help` names the agent's vault in the credential cascade

Bare `skyscanner help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes. The `oxylabs` guide says the same: `connection add` keeps the Oxylabs password in the agent's vault by default, in the project's `.env.local` with `--file`, and in `credentials.env` with `--machine`.
