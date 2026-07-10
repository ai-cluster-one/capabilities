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
GEMINITALK_VOICE=Kore
GEMINITALK_LANGUAGE=ru-RU
GEMINITALK_SYSTEM_PROMPT_FILE=.capabilities/geminitalk/reference/voice-context.md
```

For named connections, use `.capabilities/geminitalk/connections.json`:

```json
{
  "default": "studio",
  "connections": {
    "studio": {
      "secret_env": "GOOGLE_API_KEY",
      "model": "gemini-3.1-flash-live-preview",
      "voice": "Kore",
      "language": "ru-RU",
      "allow_capability_domain_commands": false
    }
  }
}
```

Keep `allow_capability_domain_commands` false unless you are deliberately
experimenting. The v1 agent is designed to inspect and clarify, not to perform
write-capable work by voice.
