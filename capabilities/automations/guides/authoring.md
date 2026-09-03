# Authoring automations

Run `automations service init` once in the consuming project, then treat the generated config and scripts as ordinary versioned project files.

Each `[[automations]]` entry names a stable id and a script path relative to the project root. Declare either a numeric five-field cron `schedule`, an `every_seconds` interval, or neither for a manual-only automation. Use `environments` to keep production schedules from running on a developer machine.

Scripts inherit the service environment, including values loaded from the project's `.env` and `.env.local`, so credentials remain in environment variables rather than committed config. A script receives its run identity and project paths through the `AUTOMATION_*` variables listed by `automations help`. Exit zero for success and non-zero for failure; stdout and stderr are captured into the run log.

Use `overlap = "skip"` for polling and reconciliation work where a second occurrence adds no value. Use `overlap = "queue"` only when every occurrence must eventually execute, and set a bounded `max_pending`.

## Scripts that need judgement

A script computes; when it needs an agent to judge, compare, or write prose, it runs one turn through `automations agent` and branches on the JSON that comes back. The capability owns engine selection, binary resolution, the mode fence and output normalisation, so a script never assembles an engine command itself.

Declare profiles under `[agents]`. `sonnet`, `opus` and `haiku` ship with the capability and need no configuration; `[agents.workers.<name>]` adds a profile or retunes a shipped one field by field. A profile names `engine` (`claude` or `codex`), `model`, `effort`, `mode`, `timeout_seconds`, and `service_tier` for codex. Set `agents.default` to the profile a script gets when it names none.

`mode = "read"` fences the turn to reading the project. `mode = "write"` lets the turn change files, which a scheduled job does with nobody watching, so it is declared per profile and is never the shipped default. Give a judgement-only automation a read profile and the question stops being whether the prompt was careful enough.

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

Delivery stays in the script whatever else moves. Send the result yourself; handing delivery to the agent lets a judgement be reached and then dropped, and a dropped judgement looks exactly like a quiet run.

Where detection sits is decided by the surface, not by taste. A surface that diffs - an API with stable fields, a release feed, a page whose bytes mean something - is compared in ordinary code, and a turn runs only when something moved, so a quiet period costs nothing. A surface that does not diff - a site rewritten between visits, an application that renders through JavaScript and publishes no feed, prose whose meaning is the signal - is read by the agent on every run, because a matcher written against today's markup is a guess about tomorrow's.

Getting that backwards is expensive in both directions. Code against a surface that does not diff goes blind exactly where the answer was, and reports the blindness as silence. An agent against a surface that does diff pays a turn to re-derive what comparing two fields already knew.

The recurring shape of this choice is a monitor; `automations guide monitors` owns what one has to get right.
