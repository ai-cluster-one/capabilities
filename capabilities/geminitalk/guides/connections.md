# geminitalk connections

`geminitalk` uses a Google AI Studio API key and a small amount of per-project
voice policy.

For the implicit default connection, set one of:

```sh
GEMINITALK_API_KEY=...
GOOGLE_API_KEY=...
GEMINI_API_KEY=...
```

Optional settings:

```sh
GEMINITALK_MODEL=gemini-3.1-flash-live-preview
GEMINITALK_VOICE=Aoede
GEMINITALK_LANGUAGE=auto
GEMINITALK_AGENT_NAME=Tessa
GEMINITALK_MAX_AGENT_SESSIONS=3
GEMINITALK_ALLOW_CAPABILITY_DOMAIN_COMMANDS=false
GEMINITALK_ALLOW_CODEX_TASKS=true
```

For named connections, use `.capabilities/geminitalk/connections.json`:

```json
{
  "default": "studio",
  "connections": {
    "studio": {
      "secret_env": "GOOGLE_API_KEY",
      "model": "gemini-3.1-flash-live-preview",
      "voice": "Aoede",
      "agent_name": "Tessa",
      "max_agent_sessions": 3,
      "language": "auto",
      "allow_capability_domain_commands": false,
      "allow_codex_tasks": true,
      "allow_write": true
    }
  }
}
```

Keep `allow_capability_domain_commands` false unless you deliberately need broad
capability domain commands. `allow_codex_tasks` defaults true and exposes the
dedicated bounded `codex_task` tool. `allow_write` also defaults true; set it
false on a connection that must remain read-only.

`voice` selects a Gemini prebuilt voice; run `geminitalk voices` for the current
list. `agent_name` controls how the spoken companion introduces itself.
`max_agent_sessions` limits concurrent background agent jobs and is clamped to
the range 1 through 16. Exact duplicate active tasks are still deduplicated.

Run `geminitalk init` once in each consuming project after `capabilities init`;
it creates `.capabilities/geminitalk/base.md` from the bundled template and
never overwrites an existing file. Edit that project copy for local voice
behavior.

`prompt_files` is an ordered array loaded fresh for every session. Omit it for
the canonical prompt stack: GeminiTalk loads `.capabilities/geminitalk/base.md`,
then also
`.codex/generated/context.md` if that generated Codex context file exists.
Explicit `prompt_files` arrays stay authoritative and are not expanded. Paths
with a `capability:` prefix resolve inside the installed GeminiTalk bundle;
other relative paths resolve from `project_root`. Files are supplied in array
order, with earlier files taking precedence when instructions conflict. Runtime
permissions and safety gates always remain authoritative.

Each prompt file is limited to 20,000 characters and the combined stack to
40,000 characters. `prompt_file` and `GEMINITALK_SYSTEM_PROMPT_FILE` remain as
legacy single-file inputs; when used, GeminiTalk prepends the default project
base/context stack automatically.
