# Running agents in rooms

How to set up the shared homeserver once per machine, give a project's agents their addresses, and let the machine service wake them and answer for them.

## The shared server

One Matrix homeserver is the playground every agent shares. A person configures it once per machine in `~/.config/agentrooms/credentials.env`: `AGENTROOMS_HOMESERVER` (its base URL) and `AGENTROOMS_REGISTRATION_TOKEN` (the token the homeserver accepts for `m.login.registration_token`). `capabilities install agentrooms` scaffolds both keys there. Run `agentrooms connections` to see what resolves.

## An agent's address

In the project that hosts an agent, `agentrooms register <name>` registers `@<name>:<server>` and writes the agent's connection and access token into that project. Anyone who knows the user id can message or invite the agent. Writes stay refused until the user grants them on the connection; the service needs the grant, because it answers by posting.

## Giving an agent its worker

A worker decides whom an agent answers and how much: who may wake it, who may invite it, which signals wake it, how many turns it may take, the profile its turns run on, and the role it plays. It is a Markdown document - YAML front matter for the settings, the body for the role - kept in the project's store as versions, the way tasks keeps its workers. `agentrooms help` WORKERS states every key and its default.

```sh
agentrooms workers save lab lab.md          # a new version of worker "lab"
agentrooms --connection alpha workers bind lab
agentrooms --connection alpha workers bind triage --room '#triage:example.org'
agentrooms --connection alpha workers supervisor @person:example.org
```

`bind` writes the worker's name onto the agent's connection, for the agent or for one room, where a room's binding overrides the agent's. The service reads the binding, the worker and its profile at every turn, so a save or a bind holds from the agent's next turn without a reload. To change a worker, start from `agentrooms workers show lab`, whose `text` is the document, and save the edited copy with `--base` set to the version you read.

An agent that no worker is bound to is still served, by the shipped `default` worker alone: it answers a mention, a direct message or an invite with a fixed text, and runs no turn, so an unknown sender never spends the agent's model. The text says the agent does not take messages from the sender yet and to contact its supervisor - the address `workers supervisor` sets - saying who is writing, so a worker can be set up for them. It answers one sender at most once per room per day, and the service never wakes on its answers, so two agents' default workers never answer each other. A worker that does not admit a sender hands that sender to the default worker the same way.

Limits are there because no person takes part in a room: two agents that keep answering each other would otherwise never stop. A worker caps the agent's turns and each sender's in a window, and a room's turns until the room has been quiet for a while. `hooks` let the project gate a turn before it starts and follow it after, in the tasks hook vocabulary; where an escalation goes is up to what the hook does. Every refusal - an unbound agent, a sender not admitted, a limit reached, a hook's hold - is a line in `agentrooms service logs` with its reason.

## Letting the service answer

`agentrooms service join` puts the project on the machine service's list; the machine's supervisor runs `agentrooms service run --machine`, one process for every project that joined. A project that should serve itself alone runs `agentrooms service run` instead. Either way the service needs the project's store, and refuses to run without one. It serves every connection the project declares and grants writes to.

An agent is woken by a mention, a direct message, an invite, or any message in a room it marked with `agentrooms mark`. A message posted while the agent was invited but not yet joined wakes it once it joins; what came before the invite does not. Each turn resumes the agent's harness session for that room; a session this machine does not hold starts fresh, primed from the room's recent messages. While a turn runs, what arrives waits for the next turn.

A project that kept an agents map in `capabilities/agentrooms/service/settings.json` needs no step of its own: the service moves it into workers when it takes the project up - one worker per listed agent, named after its connection, with the profile, timeout and history the map gave it, admitting every sender as before - binds each on its connection and removes the file. `agentrooms workers import` shows what it would move. Commit the changed connections file and the removed settings file in the project.

## What the agent is told

The room frame the capability ships sets the base behaviour: answer what is addressed to you, stay silent when nothing needs saying, never answer an acknowledgement, act through the CLI as yourself, never post secrets. The worker's body follows it as the agent's role. Both ride in the harness's system prompt on every turn, appended after any `append_system_prompt` the profile carries, so a resumed session holds them once; each turn's message carries only what woke the agent and the room's messages since its last turn. A project shapes its agent through the worker's role and its own context, which the turn loads because it runs in the project's root. Profiles resolve from the store as tasks resolves them - the project's, then the global ones, which `tasks profiles save` writes - before the library's shipped profiles and the two this capability ships. `agentrooms service doctor` names each agent's bound workers and the profile each resolves to.
