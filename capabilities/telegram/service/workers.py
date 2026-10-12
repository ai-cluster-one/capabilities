"""What a worker turn is handed, and what its ending is called.

Every harness turn the daemon starts - a dialogue reply, a durable job, a voice
task, the post-call summary, the doctor's probe - runs through
callva-harness-runner, which launches the harness, reads its report and names
a failure by kind. This module owns the two things the product says on top of
that: the environment a worker starts from, and the sentence a person is given
when a turn ends badly. Nothing here imports the runner, reads settings, the
store or the network, so the CLI loads it without standing up a daemon.
"""
import os
import signal

# How long a worker turn may take when the project says nothing, in seconds.
# The doctor's round-trip is held to the same window a turn is, read from the
# same setting.
DEFAULT_WORKER_TIMEOUT = 90

WORKER_ENV_DROP = ("TELEGRAM_SERVICE_LAUNCH_NONCE", "SSH_AUTH_SOCK")
WORKER_ENV_DROP_PREFIXES = ("CLAUDE_CODE_", "CLAUDECODE", "VSCODE_")


def scrub_worker_env(environ):
    """A copy of `environ` fit to hand a worker binary.

    The launch nonce is the daemon's proof of ownership and the agent-channel
    keys are a way into another editor session; neither has business in a
    child. The forwarded ssh agent is a credential the worker never asked for.
    """
    env = dict(environ)
    for name in WORKER_ENV_DROP:
        env.pop(name, None)
    for name in [key for key in env if key.startswith(WORKER_ENV_DROP_PREFIXES)]:
        env.pop(name, None)
    return env


def _kill_process_group(proc):
    """Kill the process group created for a worker, even if its leader already exited."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False


class WorkerFailed(RuntimeError):
    """A harness turn that ended without a completed answer.

    `kind` is the runner's name for the ending (`quota`, `not_authenticated`,
    `timeout`, `cancelled`, `crash`, `error`, ...), which is what a caller
    branches on: a spent quota pauses the queue rather than failing the job.
    `reason` is the harness's own sentence."""

    def __init__(self, message, kind=None, reason=""):
        super().__init__(message)
        self.kind = kind
        self.reason = str(reason or "")


class WorkerModelRefused(RuntimeError):
    """The worker binary will not run the model it was configured with.

    This ending is named because nothing about it is worth another attempt: the
    same binary refuses the same model the same way every time, and no amount of
    starting over changes a setting. It is also the one failure that happens
    before any work does, so there is nothing half-done to protect.

    It carries two texts and keeps them apart, because they are read by
    different people. `str(...)` is the notice — the product's own words, and
    the whole of what a caller on a call is told, since that path speaks
    whatever it is handed to whoever is on the line. The engine's own sentence
    stays on `reason`, for the surfaces that answer to an operator: naming the
    ending is what was missing, and it is not worth having if the price is the
    only text that says what actually refused.
    """

    kind = "model_refused"

    def __init__(self, notice, reason=""):
        super().__init__(notice)
        self.notice = notice
        self.reason = str(reason or "")


class WorkerProfileRefused(WorkerModelRefused):
    """The profile a turn names does not resolve or fit, so nothing ran.

    It ends a turn the way a refused model does - nothing ran, and running the
    same turn again is refused the same way until a setting or a profile
    changes - so it is that ending, and its notice names the profile and the
    problem."""

    kind = "profile_refused"


def model_refusal_notice(binary, model):
    """What a person is told when the binary will not run the model.

    The binary and the model are the whole of the fact, and the fact is that
    these two will not run together — both taken from what the run was
    configured with rather than from the refusal's own prose, which names them
    in whatever shape the provider chose and often in none a person can act on.

    Which of the two is the one to move is not something the daemon knows. The
    same refusal is returned for a model that does not exist and for a binary
    too old for one that does, so the notice states the pairing and stops
    rather than sending a person to the setting that may not be holding it.
    """
    named = f'the model "{model}"' if model else "its default model"
    return (f"{binary} will not run {named}. Nothing ran, and a retry is "
            "refused the same way.")


def worker_failure(binary, model, kind, reason):
    """The exception a turn the harness did not complete raises, named by the
    kind of ending the runner gave it: a refused model is the product's notice
    with the harness's sentence kept under it; anything else is the harness's
    sentence."""
    if str(kind) == "model_refused":
        return WorkerModelRefused(model_refusal_notice(binary, model), reason)
    return WorkerFailed(f"{binary} worker failed: {str(reason)[:500]}", str(kind or "error"),
                        reason)


def worker_diagnosis(exc, fallback):
    """What an operator is told about a worker that ended badly.

    A named ending keeps the engine's own words *under* the name rather than in
    place of it. The name is the classification, which an operator has no other
    way to get; the engine's sentence is the only text that says what actually
    refused, and it is the one an operator acts on. Anything the daemon has not
    named reads exactly as it read before there was a name for anything.
    """
    if isinstance(exc, WorkerModelRefused) and exc.reason:
        # Flattened and bounded exactly as `_short_error` flattens and bounds
        # every other reason that reaches these same surfaces, so one line of
        # log stays one line of log and a row stays a row.
        said = exc.reason.replace("\n", " ")[:500]
        return f"{exc.notice} It said: {said}"
    return fallback
