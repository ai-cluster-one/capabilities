# callva — change log

## 2026-11-21 — `callva help` is the menu, and `callva help <verb>` one verb's whole contract

`callva help` is the short complete menu of SHEBANG.md, "Agent-first help is the surface": what callva is, the `agents config` caveat, how connections, the write gate and the headless mode are reached, every verb under Agents and their speech stack, Calls, Records, Automations and schedules, and Project and tenant - a resource's reads on one line and its writes, marked WRITE, on another - then the I/O, the exit codes and the contract verbs the library renders, with no flag on any line. `callva help <verb>` takes the whole verb path (`help agents update`, `help field-groups add-field`, `help livekit connection`) and prints that verb's whole contract from `HELP_VERBS` - its synopsis with every flag, the text the old help held for it, the route `callva routes` declares and its write or secret gate, its exit codes and an example - followed on every domain verb by the conventions, the global options, the output shape, the I/O contract and the exit-code table; a group (`help agents`) lists its verbs, and an unknown path exits 6 `unknown_verb` with nothing on stdout and the hint to run `callva help`. CONNECTIONS moved to `help connections`, after the library's connections home and secret cascade, doctor's text to `help doctor`, HEADLESS to `help headless`, and the identifiers envelope's location to `help ids`; each resource's notes (presets, tools, the PUT-and-PATCH rule, issues and their stack, knowledge, transcripts, triggers, projects and phone numbers) are on the pages of its verbs, and the issue stack examples stay in the menu too. `help` after a global option and `help` under `CALLVA_HEADLESS` answer the same layered help. The capability binds `capabilities-contract` 0.50 or later, which dispatches the layered help. No verb, flag, JSON key, output or exit code changes.

## 2026-11-21 — `callva` reaches a call's tool calls, the call allowance, the LiveKit connection and webhook-secret rotation, and schedules use their canonical path

New verbs: `calls tool-calls <call_id>` lists the tools the voice agent called in a call, read from its transcript, for a key holding the `tool_calls.read` permission; `allowance` answers the tenant's monthly call allowance; `livekit connection` answers whether the tenant's LiveKit is reachable and its URL, for a manually issued tenant-wide key; `settings rotate-webhook-secret` replaces the project's agent webhook signing secret. The rotation is write-gated, and `callva routes` marks it `secret`, so no `CALLVA_HEADLESS` mode runs it. Every `schedules` verb now calls `/external/schedules`, and `schedules executions` calls `/executions`; arguments and output are unchanged. The help says that a version-2 transcript carries tool calls only for a key holding `tool_calls.read`, that `agents update --data` carries `test_variables`, and that `projects list`, `phone-numbers list` and `providers list` answer for the whole tenant when read with a tenant-wide key. The help for `agents config` says that since 2026-11-04 the platform may refuse an ordinary tenant API key on `POST /config/livekit` where it has a worker key configured, and that the External API offers no replacement; the verb itself is unchanged.

## 2026-11-16 — `callva` declares its title and summary in Russian and Estonian

The manifest carries `translations`, from the `TRANSLATIONS` constant: the title and summary in Russian (`ru`), CallVA, and Estonian (`et`), CallVA, each as `title` and `description`. English `title` and `summary` stay the source and the fallback. The capability binds `capabilities-contract` 0.49 or later, which emits them. Nothing else changes.

## 2026-11-10 — `callva` declares its title, CallVA

The manifest carries `title`, `CallVA`, the capability's human name, fixed here and never renamed, from the `TITLE` constant. The capability binds `capabilities-contract` 0.48 or later, which emits it. It is still addressed as `callva`, and its `icon.png` is unchanged. Nothing else changes.

## 2026-11-04 — `callva issues` carries the optional `stack` field

`issues create`, `update` and `put` send `stack` in their payload like any other field, `issues list -f stack=<value>` filters by it, and the list summary shows it where the platform returns it. The help for these verbs describes the standard: a plain string `preset/model/speech/worker`. No route changes.

## 2026-11-04 — `callva` creates agents, projects and phone numbers, binds a number to an agent, and reaches issues, knowledge and the extraction schema

New verbs: `agents create`, `projects create|update|put|delete`, `phone-numbers create|update|delete|bind`, `issues list|get|create|update|put|delete`, `knowledge list|get|create|update|put|delete` and `fields extraction-schema`. Those that change the platform are write-gated and `callva routes` declares them. `agents update` takes `--custom-webhook-events`, a comma-separated list that replaces the event types the agent's custom webhook forwards, where `""` forwards nothing. `phone-numbers bind <id>` takes the agent or `--unbind` and refuses neither, because the API reads a missing agent as an unbind.

## 2026-11-03 — `CALLVA_HEADLESS=read-write` lets the credential's own scopes bound writes, and no headless mode touches a local path

`CALLVA_HEADLESS=read-write` is `read-only` plus the verbs `callva routes` marks `writes`, which the platform bounds by the credential's scopes and `CAPABILITIES_READ_ONLY` still closes. Everything else stays refused: verbs marked `secret`, connection flags, anything undeclared, and the same `headless_refused` wherever a project, the machine's capability configuration or a store resolves. In both modes `--prompt-file`, `--config-file`, `--content-file`, `--inputs-file`, `--data-file`, `--file`, `--args-file` and `--output` now exit 4 `headless_refused`, so `recordings stream --output`, the one of them `read-only` used to run, is refused there too and a recording goes to stdout. Without the variable nothing changes. `deviations.md` records why.

## 2026-11-03 — `CALLVA_HEADLESS=read-only` serves a process holding one credential and nothing else

With `CALLVA_HEADLESS=read-only` and `CALLVA_API_KEY` (optionally `CALLVA_API_URL`) in its environment, a process with no project, store or manager runs every verb `callva routes` marks neither `writes` nor `secret`, plus `help`, `stub`, `manifest`, `routes` and `doctor`. Every other verb exits 4, `--connection`, `--key-env` and `--api-url` are refused, and the mode exits 4 `headless_refused` wherever a project, the machine's capability configuration or a store resolves. Without the variable nothing changes. `deviations.md` records why.

## 2026-11-03 — `callva` reaches record actions, saved views and one secret setting, and says which route each verb calls

New verbs: `calls actions`, `calls run-action`, `calls action-runs`, `actions list|get|create|update|put|delete`, `ui-views list|get|create|update|put|delete|set-default`, `recordings purge` and `settings reveal`. Those that change the platform are write-gated. `update` sends PATCH and `put` sends PUT, which the API treats alike as a partial update. `recordings purge` drops the platform's cached copy of a recording, and the recording itself stays.

`settings reveal <definition_id>` prints one setting's value raw on stdout with a trailing newline and nothing else, and exits 3 `not_set` when it holds none. It writes nothing but a read-only connection refuses it with exit 4 `read_only`, and so does the read-only switch.

`callva routes` prints a JSON map of every API verb: `verb`, `method`, `path`, `writes` and `secret`. It is local, uses no connection and runs outside a project too. `tests/test_routes.py` runs every verb and holds the map to the call it makes.

`agents default` is described as what it does: it only reads, answering the project's default agent, else its oldest, and exit 3 when it has none.

## 2026-10-31 — `callva help` names the agent's vault in the credential cascade

Bare `callva help` no longer lists where a secret resolves by hand, a list that stopped at the process environment and left out the agent's vault; it now points to the contract-rendered `Where a secret resolves` section at the end of the help, which names the vault as the cascade's last tier. Nothing else changes.

## 2026-10-30 — `callva` carries its own icon

The bundle holds its icon as `icon.png`; `capabilities icon callva` answers its path and size, and `capabilities list` and `inventory` mark it with `icon: true`. Nothing else changes.

## 2026-10-13 — Outside any project, `callva connections` and `callva doctor` read the machine's own connections

`callva` declares `connections` and `doctor` as machine reads. Run outside any project, `callva connections` lists the connections declared globally whose grant is not switched off, each under `machine_reads` with the scope `machine`, and `callva doctor` checks their health, where both used to refuse with `connection_not_granted`. Each such connection is read-only there: a write through it exits 4 `read_only`, and every other verb outside a project is refused the machine's connections as before. Inside a project nothing changes; its grants alone decide.

## 2026-10-13 — `callva` declares what a person is asked for, and writes it through the contract

`callva manifest` now declares a connection's fields under `connections.fields` (`api_url`, `secret_env`), each with its label, help, type, whether it is required and whether it is a secret, and `callva connection add <id>` writes one connection from them: non-secret fields on argv with `--field KEY=VALUE` or in a JSON object on stdin with `--stdin`, a secret only on stdin, written to the scope's `.env.local` or `credentials.env` under its own env key. `callva connection remove <id>` takes it out again with the secrets it wrote. Bare `callva help` ends with a section rendered from these declarations. Nothing else changes.
