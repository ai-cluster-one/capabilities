# Authoring automations

Run `automations service init` once in the consuming project. It writes `config.toml`, which holds the scheduler's own `[engine]` settings and the `[agents]` profiles, and an example script; both are ordinary versioned project files.

An automation is a definition in the store, not a line of that file: `automations set <slug> --script <path>` creates one, and every later `set` saves a new version with the fields it names. Give it a five-field cron `--schedule` and the `--timezone` it is read in, or no schedule for an automation that runs only when asked, and the `--node` that schedules it. A definition names one node, so the same automation never runs on two machines, and moving it to another node from any machine is one `set`: every daemon listening on the store takes the change up, and no file on either machine is touched. A daemon schedules the definitions whose node is the name it runs under, `AGENT_NODE_NAME` or else the host name. `automations history <slug>` shows every version and who saved it, and a run names the version it ran.

Give a definition a `--description`. It changes nothing that runs: it carries why the automation exists into `automations list`, so reading the set does not mean opening every script.

A script is either a file in the project, named by its path relative to the project root, or a body kept in the store (`--body <file>`). A body is written to a file of its own for each run and runs through its `#!` line, so it must be self-contained: it reads no file beside itself, because there is none. A script that reads a brief, a schema or a shared helper next to it stays a path until it is rewritten. A path is a file on the node that runs it, so that node's checkout has to carry it.

A project whose `config.toml` still declares `[[automations]]` is moved with `automations import --apply`, which saves each as a definition whose script stays the path it was and takes them out of the file; until then the commands that read the declaration refuse and name the import.

Scripts inherit the service environment, including values loaded from the project's `.env` and `.env.local`, so credentials remain in environment variables rather than in a definition. A script receives its run identity and project paths through the `AUTOMATION_*` variables listed by `automations help`. Exit zero for success and non-zero for failure; stdout and stderr are captured into the run log.

One run of an automation is active at a time across every node: a scheduled firing that finds one running or queued is recorded `skipped`, so polling and reconciliation work never piles up behind a slow run.

## Scripts that need judgement

A script computes; when it needs an agent to judge, compare, or write prose, it runs one turn through `automations agent` and branches on the JSON that comes back. The capability owns engine selection, binary resolution, the mode fence and output normalisation, so a script never assembles an engine command itself.

Declare profiles under `[agents]`. `sonnet`, `opus` and `haiku` ship with the capability and need no configuration; `[agents.workers.<name>]` adds a profile or retunes a shipped one field by field. A profile names `engine` (`claude` or `codex`), `model`, `effort`, `mode`, `timeout_seconds`, and `service_tier` for codex. Set `agents.default` to the profile a script gets when it names none.

`mode = "read"` fences the turn to reading the project. `mode = "write"` lets the turn change files, and `mode = "act"` drops the sandbox altogether, for a worker whose job is to reach the network and operate tools outside the project. Neither of those two is a shipped default: a scheduled job runs with nobody watching, so the authority it holds is declared per profile and read at a glance. Give a judgement-only automation a read profile and the question stops being whether the prompt was careful enough.

Every job receives `AUTOMATIONS_BIN`, the absolute path of the CLI that scheduled it, so the call needs no lookup:

```python
result = subprocess.run(
    [os.environ["AUTOMATIONS_BIN"], "agent", "--profile", "sonnet",
     "--schema", str(SCHEMA), "-"],
    input=prompt, cwd=os.environ["AUTOMATION_PROJECT_ROOT"],
    capture_output=True, text=True, timeout=700)
answer = json.loads(result.stdout)["answer"]
```

Pass `-` as the prompt and feed it on stdin; a prompt built from evidence outgrows argv quickly. Give `--schema` a JSON Schema file and `answer` arrives as a parsed object, so the script branches on fields rather than reading prose. A turn that answers prose while a schema was required fails rather than returning the one shape the script cannot use.

Decide deliberately who delivers. A script that sends the result itself can prove the judgement was not reached and then dropped; a turn that sends its own message is far more flexible, and pays for it by making a dropped judgement look exactly like a quiet run. Neither is wrong - but a job whose turn delivers owes its caller an alarm on the turn failing, because that is the only remaining difference between working and dead.

Where detection sits is decided by the surface, not by taste. A surface that diffs - an API with stable fields, a release feed, a page whose bytes mean something - is compared in ordinary code, and a turn runs only when something moved, so a quiet period costs nothing. A surface that does not diff - a site rewritten between visits, an application that renders through JavaScript and publishes no feed, prose whose meaning is the signal - is read by the agent on every run, because a matcher written against today's markup is a guess about tomorrow's.

Getting that backwards is expensive in both directions. Code against a surface that does not diff goes blind exactly where the answer was, and reports the blindness as silence. An agent against a surface that does diff pays a turn to re-derive what comparing two fields already knew.

The recurring shape of this choice is a monitor; `automations guide monitors` owns what one has to get right.
