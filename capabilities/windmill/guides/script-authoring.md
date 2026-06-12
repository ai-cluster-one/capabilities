# Windmill — script authoring (`f/<namespace>/*`)

The model for a deployable script in a consuming project — the starting point for understanding how its Windmill scripts are built. The wider operating context is the `operational` guide. Platform mechanics common to any project (deploy, timeout, lock) are in `windmill help`; a project's fixed values and variable paths live in its own identifiers envelope (`windmill ids list`).

## Prefer the box's CLIs over reimplementation

A script holds its own orchestration logic freely. The one rule: **don't re-implement a capability the agent box already exposes.** The box runs the project's capabilities as CLIs. When a script needs one of them, invoke that CLI **over SSH → `docker exec`** rather than building a fresh client (raw HTTP / SDK / service token) for the same thing. The box holds every service credential; Windmill holds exactly one secret — the SSH key.

**Discover capabilities at authoring time — never hardcode the list.** The set of CLIs on the box grows; a baked-in list goes stale. To find what's available, ssh in and inspect, then read the tool's own contract:

```
ssh -i <key> <user>@<host> "docker exec -i $(docker ps -q --filter ancestor=<AGENT_IMAGE> | head -1) ls /usr/local/bin"
ssh -i <key> <user>@<host> "docker exec -i $(docker ps -q --filter ancestor=<AGENT_IMAGE> | head -1) <tool> help"
```

Whatever the box can do, the script consumes through the box's CLI.

**This extends to reasoning, not just service calls.** A script whose step needs judgment must not re-encode that judgment in its own TypeScript — that duplicates doctrine into the harness, where it rots out of sync with the routine that owns it. Instead it **boots the box's `claude` headless with a high-level orientation prompt** and lets the agent load the routine and do the step. The harness stays thin — transport, a budget cap, optionally a session resume — and reconciles only the facts the agent can't self-measure from inside its run (the run's cost, its own session id). The second example below is this shape in code.

## The SSH bridge

Every script connects with the `ssh2` library, reading three folder-scoped variables — `agent_ssh_host`, `agent_ssh_user`, `agent_ssh_key` — then `docker exec -i`s into the container.

- **`box(inner)`** wraps a tool call as `docker exec -i $(docker ps -q --filter ancestor=<AGENT_IMAGE> | head -1) <inner>`. Resolve the container **by image**, never by name — a PaaS renames it on every redeploy.
- **`q(s)`** single-quotes a value for the host `sh -c` layer. But anything with spaces, quotes, or newlines — notes, comment bodies, an LLM prompt, JSON blobs — goes over **stdin**, never inline. Two shell layers (ssh + `sh -c`) make inline quoting a footgun.
- **No `-t`.** A TTY corrupts JSON output. Read stdout as data.

## Conventions

- **Self-contained per file.** Windmill has no cross-script imports — the SSH/quoting/`box` helpers are duplicated in each script by design. Don't factor them into a shared module.
- **Stable non-secret ids are constants at the top.** They aren't secrets, and a script can't read the project's reference files at runtime.
- **Secrets only via Windmill variables.** Read at runtime with `await wmill.getVariable("f/<namespace>/<name>")`. Never inline a key in source.
- **Bounded step, deliberate timeout.** A job runs one step to completion with no mid-execution yielding; pick `--timeout` at deploy to fit the job's real worst case (it hard-kills overruns). Never poll-and-wait inside a job.
- **Language is `bun`** by default — match what the script is written for at deploy.

## Example — the minimal SSH-dispatch skeleton

Adaptable example, not a production script — it demonstrates the one pattern this capability is about: *Windmill as a thin SSH dispatcher*. Opens ONE SSH connection to the agent box and runs a single CLI command there via `docker exec`, returning its output. This is the shape every script copies: read the SSH vars, resolve the container by image, exec a box CLI, read stdout. Adapt: `<AGENT_IMAGE>`, the `f/<namespace>/agent_ssh_*` variable paths, and the command.

```typescript
import * as wmill from "windmill-client";
import { Client } from "ssh2";

// Resolve the container by IMAGE (never by name — a PaaS renames it on redeploy).
const CID = "$(docker ps -q --filter ancestor=<AGENT_IMAGE> | head -1)";
const box = (inner: string) => `docker exec -i ${CID} ${inner}`;

async function sshRun(cmd: string, stdin = ""): Promise<{ code: number; out: string; err: string }> {
  const host = (await wmill.getVariable("f/<namespace>/agent_ssh_host")).trim();
  const user = (await wmill.getVariable("f/<namespace>/agent_ssh_user")).trim();
  const key = await wmill.getVariable("f/<namespace>/agent_ssh_key");
  return await new Promise((resolve, reject) => {
    const conn = new Client();
    conn.on("ready", () => {
      conn.exec(cmd, (e, stream) => {
        if (e) { conn.end(); return reject(e); }
        let out = "", err = "";
        stream.on("close", (code: number) => { conn.end(); resolve({ code, out, err }); });
        stream.on("data", (d: Buffer) => { out += d.toString(); });
        stream.stderr.on("data", (d: Buffer) => { err += d.toString(); });
        stream.end(stdin); // dynamic/multi-line input goes on stdin, never inline. No -t.
      });
    });
    conn.on("error", reject);
    conn.connect({ host, username: user, privateKey: key });
  });
}

// Default runs the box's own `windmill doctor`; pass any box CLI command instead.
export async function main(tool_cmd = "windmill doctor") {
  const r = await sshRun(box(tool_cmd));
  if (r.code !== 0) throw new Error(`box command failed (exit ${r.code}): ${r.err || r.out}`);
  try { return JSON.parse(r.out); } catch { return { raw: r.out.trim() }; }
}
```

## Example — drive headless Claude on the agent box

The interesting pattern: Windmill holds only the SSH key; the box holds the Claude auth and every tool. This script SSHes in and runs `claude -p` with the prompt on stdin, JSON out, and a hard budget cap — one bounded step. Resume a prior session by passing its `session_id` (an autonomous loop's worker is built from exactly this). Adapt: `<AGENT_IMAGE>`, the `f/<namespace>/agent_ssh_*` variables, the prompt, `MAX_USD`.

```typescript
import * as wmill from "windmill-client";
import { Client } from "ssh2";

const MAX_USD = 0.5;
const CID = "$(docker ps -q --filter ancestor=<AGENT_IMAGE> | head -1)";
const box = (inner: string) => `docker exec -i ${CID} ${inner}`;

async function sshRun(cmd: string, stdin = ""): Promise<{ code: number; out: string; err: string }> {
  const host = (await wmill.getVariable("f/<namespace>/agent_ssh_host")).trim();
  const user = (await wmill.getVariable("f/<namespace>/agent_ssh_user")).trim();
  const key = await wmill.getVariable("f/<namespace>/agent_ssh_key");
  return await new Promise((resolve, reject) => {
    const conn = new Client();
    conn.on("ready", () => {
      conn.exec(cmd, (e, stream) => {
        if (e) { conn.end(); return reject(e); }
        let out = "", err = "";
        stream.on("close", (code: number) => { conn.end(); resolve({ code, out, err }); });
        stream.on("data", (d: Buffer) => { out += d.toString(); });
        stream.stderr.on("data", (d: Buffer) => { err += d.toString(); });
        stream.end(stdin);
      });
    });
    conn.on("error", reject);
    conn.connect({ host, username: user, privateKey: key });
  });
}

export async function main(prompt: string, resume_session = "") {
  if (!prompt) throw new Error("prompt is required");
  // Prompt on stdin (never inline — two shell layers make quoting a footgun).
  // JSON out, no -t (a TTY corrupts JSON), budget-capped so a runaway job can't drain.
  const cmd = box(
    `claude -p --output-format json --permission-mode bypassPermissions ` +
    `--max-budget-usd ${MAX_USD}` + (resume_session ? ` --resume ${resume_session}` : ""),
  );
  const r = await sshRun(cmd, prompt);
  let parsed: any = null;
  try { parsed = JSON.parse(r.out); } catch { /* leave raw */ }
  return {
    ok: r.code === 0 && parsed != null && parsed.is_error !== true,
    session_id: parsed?.session_id,        // pass back in as resume_session to continue
    cost_usd: parsed?.total_cost_usd ?? 0,
    result: parsed?.result ?? r.out.slice(0, 1500),
  };
}
```
