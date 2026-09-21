"""OpenAI GPT Live as a speech stack for direct Telegram calls.

GPT Live is two models in one session: a speaking model that holds the voice,
the pauses and the interruptions, and a backend that reasons. Under client
delegation the backend is ours — the project's own worker — so this provider
takes the voice and nothing else.

What that costs is the tool channel: with `delegation: {"type": "client"}` the
protocol has nowhere to declare a tool, no event to receive a call on, and no
event to return a result through. The model asks for help by handing a turn
over, and the answer goes back as prose the model paraphrases aloud. `TOOL_NAMES`
is therefore empty, and a settings file naming tools is not an error here — the
tools simply have no surface to be declared on, and the worker brings its own.
"""

from __future__ import annotations

from .session import (  # noqa: F401
    AGENT_RATE,
    CALLER_RATE,
    DEFAULT_MODEL,
    DEFAULT_VOICE,
    VoiceCallSession,
)

NAME = "gptlive"

# Client delegation has no tool channel at all; see the module docstring.
TOOL_NAMES: tuple[str, ...] = ()

# How this provider reaches the worker. Not through a tool it may or may not be
# given: handing a turn over IS the mechanism, so the task runner is not
# optional here and is not governed by the tool set.
DELEGATES_TO_WORKER = True
