// example_claude_job — drive headless Claude on the agent box.
//
// The interesting pattern: Windmill holds only the SSH key; the box holds the Claude
// auth and every tool. This script SSHes in and runs `claude -p` with the prompt on
// stdin, JSON out, and a hard budget cap — one bounded step. Resume a prior session by
// passing its session_id (the autonomous loop's worker is built from exactly this).
//
// Adapt: <AGENT_IMAGE>, the f/<namespace>/agent_ssh_* variables, the prompt, MAX_USD.
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
