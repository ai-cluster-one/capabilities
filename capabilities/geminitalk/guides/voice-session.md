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

The system prompt keeps the assistant in a read-only project-companion role. It
can inspect files, search text, and call safe capability contract commands. It
must redirect code edits, installs, commits, deploys, sends, calls, and other
external writes back to the main Codex text conversation.

Use `geminitalk text "question"` when testing on a headless machine or when audio
devices are unavailable.
