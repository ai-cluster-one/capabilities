# geminitalk voice session

Run:

```sh
geminitalk doctor
geminitalk talk
```

`geminitalk talk` opens a Gemini Live API session with native audio output. It sends
microphone audio as raw 16-bit PCM at 16 kHz and plays model audio at 24 kHz.
Transcripts and tool calls are mirrored to stderr so a developer can see what
the voice loop is doing.

On macOS, microphone capture and model playback run continuously through Apple's
Voice Processing audio engine. There is no Python playback queue and neither
direction gates the other. Gemini performs speech activity detection. A server
`interrupted` event only clears the obsolete playback tail.

The assistant can inspect files, search text, and call safe capability contract
commands. When `allow_codex_tasks` is enabled, it can delegate an explicit
research or implementation task to headless Codex in the current project. The
tool returns a job id immediately, keeps at most one Codex job active, and lets
the voice conversation continue. When Codex finishes, the runtime injects the
bounded final answer, changed-file list, and small trace metadata into an idle
Gemini turn so the result is announced automatically.

When launched from an active Codex thread, `codex_portal` emits a structured
message to that active turn. Text written to the talk process stdin is sent to
Gemini as realtime text input. This relay moves explicit messages only; it does
not mirror either conversation history.

`end_session` is accepted only when the current user turn contains a direct
request to end the live conversation. Gemini gives a brief goodbye and the
runtime closes after that turn. Ctrl-C remains the unconditional local stop.

Use `geminitalk text "question"` when testing on a headless machine or when audio
devices are unavailable.
