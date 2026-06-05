// example_box_command — the minimal SSH-dispatch skeleton.
//
// Opens ONE SSH connection to the agent box and runs a single CLI command there
// via `docker exec`, returning its output. This is the shape every script copies:
// read the SSH vars, resolve the container by image, exec a box CLI, read stdout.
//
// Adapt: <AGENT_IMAGE>, the f/<namespace>/agent_ssh_* variable paths, and the command.
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
