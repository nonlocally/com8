#!/usr/bin/env node
// Actual MCP -> existing CLI -> isolated durable daemon. No real agent/model.
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, mkdirSync, realpathSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const entry = process.env.COM8_MCP_TEST_ENTRY || path.join(root, "packages/communicate/src/cli.mjs");
const cli = process.env.COM8_TEST_CLI || path.join(root, "bin/communicate");
const temp = realpathSync(mkdtempSync(path.join(os.tmpdir(), "com8-mcp-core-")));
const env = { ...process.env, HOME: temp, COMM_STATE: path.join(temp, "state"),
  COMMUNICATE_DATA: path.join(temp, "data"), COM8_SOCK_DIR: path.join(temp, "sockets"),
  COM8_SESSIONS_DIR: path.join(temp, "sessions"), COM8_SELF: "fixture", COM8_TICK: "1",
  COM8_MODEL_CONFIG: path.join(temp, "models") };
env.COM8_TMUX_SOCKET = "com8-mcp-seat-" + path.basename(temp);
env.COM8_SEAT_SESSION = "owned-fixture";
for (const key of ["CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
  "CODEX_THREAD_ID", "CODEX_SESSION_ID", "COMMUNICATE_HOME", "COM8_SOCK"]) delete env[key];
mkdirSync(env.COM8_SESSIONS_DIR);
const child = spawn(process.execPath, [entry, "serve"], { env, stdio: ["pipe", "pipe", "pipe"] });
let buffer = "", stderr = "", next = 0;
const pending = new Map();
child.stderr.on("data", (part) => { stderr += part; });
child.stdout.on("data", (part) => {
  buffer += part;
  let boundary;
  while ((boundary = buffer.indexOf("\n")) !== -1) {
    const line = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 1);
    if (!line.trim()) continue;
    const message = JSON.parse(line), callback = pending.get(message.id);
    if (callback) { clearTimeout(callback.timer); pending.delete(message.id); callback.resolve(message); }
  }
});
const rpc = (method, params) => new Promise((resolve, reject) => {
  const id = ++next;
  const timer = setTimeout(() => { pending.delete(id); reject(Error(`${method} timed out: ${stderr}`)); }, 20000);
  pending.set(id, { resolve, timer });
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
});
async function call(name, args = {}) {
  const response = await rpc("tools/call", { name, arguments: args });
  assert(!response.error, JSON.stringify(response.error));
  const output = response.result.content?.[0]?.text || "";
  assert(!response.result.isError, `${name}: ${output}`);
  return output;
}

try {
  const init = await rpc("initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "core-fixture", version: "1" } });
  assert.equal(init.result.serverInfo.name, "communicate");
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");
  const tools = (await rpc("tools/list", {})).result.tools;
  const names = tools.map((tool) => tool.name);
  for (const name of ["bus_dashboard", "bus_invite", "bus_reply", "route", "com8_claim", "com8_seat_bind", "com8_model_list", "com8_model_doctor"]) assert(names.includes(name));
  assert(tools.find((tool) => tool.name === "com8_spawn").inputSchema.properties.model_connection);
  assert.deepEqual(JSON.parse(await call("com8_model_list")).connections, []);
  const invalidModel = await rpc("tools/call", { name: "com8_spawn", arguments: { name: "never-created", cli: "codex", model_connection: "missing" } });
  assert(invalidModel.result.isError, "missing model connection must fail before creation");
  for (const args of [{}, { hub: "https://bus.nonlocally.org" }]) {
    const status = JSON.parse(await call("bus_status", args));
    assert.equal(status.configured, false, "fresh status must not invent enrollment");
    assert.equal(status.hub, null, "fresh status must not select a fallback hub");
  }
  for (const file of ["server.json", "worker.json"]) {
    assert(!existsSync(path.join(env.COMM_STATE, "bus", file)), "status inspection must not start bus services");
  }
  await call("com8_start");
  const seat = JSON.parse(await call("com8_seat_spawn", { command: "bash --norc --noprofile", cwd: temp }));
  assert.match(seat.seat, /^%\d+$/);
  assert(path.isAbsolute(seat.tmux_server.socket_path), "seat response identifies its exact server");
  assert(Number.isInteger(seat.tmux_server.pid) && seat.tmux_server.pid > 0);
  assert.equal((await call("com8_seat_state", { seat: seat.seat })).trim(), "idle",
    "MCP JSON option must not become a Bash flag and kill the pane");
  const seats = JSON.parse(await call("com8_seats"));
  assert.deepEqual(seats.tmux_server, seat.tmux_server);
  assert(seats.seats.some((entry) => entry.seat === seat.seat));
  await call("com8_seat_kill", { seat: seat.seat });
  assert(!JSON.parse(await call("com8_agents")).agents.some((agent) => agent.name === "never-created"));
  await call("com8_claim", { name: "alice" });
  await call("com8_claim", { name: "bob" });
  await call("com8_send", { target: "bob", from: "alice", message: "--from" });
  const inbox = (await call("com8_inbox", { name: "bob" })).trim().split("\n").map(JSON.parse);
  assert(inbox.some((item) => item.text === "--from"), "option-shaped message must stay literal");
  const ask = call("com8_ask", { target: "bob", from: "alice", message: "--timeout", timeout: 8 });
  let token;
  for (let i = 0; i < 40 && !token; i++) {
    const mail = await call("com8_inbox", { name: "bob" });
    token = mail.match(/com8 reply ([^\s\\]+)/)?.[1];
    if (!token) await new Promise((resolve) => setTimeout(resolve, 100));
  }
  assert(token, "pending ask must not block other MCP calls");
  await call("com8_reply", { token, from: "bob", message: "--from" });
  const reply = JSON.parse(await ask);
  assert.equal(reply.reply, "--from");
  assert(reply.corr, "explicit reply correlation retained");
  assert(JSON.parse(await call("com8_agents")).agents.some((agent) => agent.name === "bob"));
  console.log("PASS: combined MCP namespaces, literal messages, durable inbox and concurrent correlated reply");
} finally {
  child.stdin.end();
  child.kill();
  for (const { timer } of pending.values()) clearTimeout(timer);
  spawnSync(cli, ["com8", "stop"], { env, encoding: "utf8", timeout: 15000 });
  spawnSync("tmux", ["-L", env.COM8_TMUX_SOCKET, "kill-server"], { env, encoding: "utf8", timeout: 5000 });
  rmSync(temp, { recursive: true, force: true });
}
