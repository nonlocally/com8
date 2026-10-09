#!/usr/bin/env node
// Exercise the MCP contract against isolated state and a fake Codex executable.
// No live sessions, real messages, registry server, or user settings are used.
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import os from "node:os";
import path from "node:path";
import { communicateCli, packageVersion } from "../src/paths.mjs";

const pkgDir = fileURLToPath(new URL("..", import.meta.url));
const taskHome = process.env.COMM_MCP_TEST_PINNED_HOME || mkdtempSync(path.join(os.tmpdir(), "comm-mcp-"));
const env = { ...process.env, HOME: taskHome, COMM_STATE: path.join(taskHome, "state"),
  COMM_BUS_PORT: "0", COMMUNICATE_DATA: process.env.COMM_MCP_TEST_DATA || path.join(taskHome, "data"),
  PATH: path.join(taskHome, "bin") + path.delimiter + process.env.PATH };
for (const key of ["CODEX_HOME", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "COMM_CODEX_INDEX",
  "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_MESSAGING_SOCKET", "COMMUNICATE_HOME"]) delete env[key];
mkdirSync(path.join(taskHome, ".codex"), { recursive: true });
mkdirSync(path.join(taskHome, "bin"), { recursive: true });
writeFileSync(path.join(taskHome, "bin", "tailscale"), '#!/bin/sh\nprintf \'%s\\n\' \'{"Self":{"HostName":"mcp-fixture","DNSName":"mcp-fixture.test.ts.net."}}\'\n', { mode: 0o755 });
const senderThread = "11111111-1111-4111-8111-111111111111";
const recipientThread = "22222222-2222-4222-8222-222222222222";
writeFileSync(path.join(taskHome, ".codex", "session_index.jsonl"),
  [{ id: senderThread, thread_name: "sender" }, { id: recipientThread, thread_name: "recipient" }]
    .map((v) => JSON.stringify(v)).join("\n") + "\n");
writeFileSync(path.join(taskHome, "bin", "codex"), `#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:] == ["queue", "--help"]:
    raise SystemExit(0)
if len(sys.argv) < 2 or sys.argv[1] != "queue":
    raise SystemExit("test refuses headless session creation")
with open(os.path.join(os.environ["HOME"], "queued.jsonl"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
`, { mode: 0o755 });

// Override the entry point to test a packed artifact with the same protocol checks.
const entry = process.env.COMM_MCP_TEST_ENTRY || path.join(pkgDir, "src", "cli.mjs");
const descriptor = process.env.COMM_MCP_TEST_DESCRIPTOR
  ? JSON.parse(readFileSync(process.env.COMM_MCP_TEST_DESCRIPTOR)).mcpServers.communicate : null;
// Real client hosts filter their MCP environment. Only the descriptor may carry
// installation paths and its isolated port; retain PATH for fake providers.
const child = spawn(descriptor?.command || process.env.COMM_MCP_TEST_COMMAND || "node",
  descriptor?.args || [entry, "serve"], { env: descriptor
    ? { HOME: taskHome, PATH: env.PATH, ...descriptor.env } : env,
    stdio: ["pipe", "pipe", "pipe"] });
let buf = "", stderr = ""; const pending = new Map();
child.stderr.on("data", (d) => { stderr += d; });
child.stdout.on("data", (d) => {
  buf += d;
  let i; while ((i = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, i); buf = buf.slice(i + 1);
    if (!line.trim()) continue;
    const msg = JSON.parse(line);
    if (msg.id !== undefined && pending.has(msg.id)) {
      const { resolve, timer } = pending.get(msg.id);
      clearTimeout(timer); pending.delete(msg.id); resolve(msg);
    }
  }
});
let nextId = 0;
const rpc = (method, params) => new Promise((resolve, reject) => {
  const id = ++nextId;
  const timer = setTimeout(() => { pending.delete(id); reject(new Error(`timeout: ${method}\n${stderr}`)); }, 30000);
  pending.set(id, { resolve, timer });
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
});
const notify = (method, params) => child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
const call = async (name, args = {}, errorExpected = false) => {
  const result = await rpc("tools/call", { name, arguments: args });
  if (result.error) throw new Error(`${name}: ${JSON.stringify(result.error)}`);
  const output = result.result.content?.[0]?.text ?? "";
  if (Boolean(result.result.isError) !== errorExpected) throw new Error(`${name}: ${output}`);
  return output;
};
const jsonCall = async (...args) => JSON.parse(await call(...args));
const assert = (ok, message) => { if (!ok) throw new Error(message); };
const EXPECT = ["agents_list", "whereis", "route", "send", "codex_queue", "codex_ask", "status", "ask", "card_set",
  "bus_register", "bus_list", "bus_agents", "bus_leave", "bus_send", "bus_receipt", "bus_status", "bus_dashboard", "bus_create", "bus_invite", "bus_device", "bus_reply",
  "com8_status", "com8_start", "com8_agents", "com8_claim", "com8_release", "com8_send", "com8_ask", "com8_reply", "com8_inbox", "com8_wait",
  "com8_spawn", "com8_restart", "com8_seats", "com8_seat_spawn", "com8_seat_send", "com8_seat_read", "com8_seat_state", "com8_seat_bind", "com8_seat_interrupt", "com8_seat_kill", "com8_model_list", "com8_model_doctor"];
let failed = false;
try {
  const init = await rpc("initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "smoke", version: "0" } });
  assert(init.result?.serverInfo?.name === "communicate", "bad serverInfo");
  assert(init.result?.serverInfo?.version === packageVersion, "MCP server version must match package version");
  assert(/register yourself on the bus/.test(init.result?.instructions ?? ""), "registration instructions missing");
  notify("notifications/initialized", {});
  const list = await rpc("tools/list", {});
  assert(JSON.stringify(list.result.tools.map((t) => t.name)) === JSON.stringify(EXPECT), "tool list mismatch");
  assert(/NAME|no reachable agents/.test(await call("agents_list")), "legacy agents_list output");
  const fresh = await jsonCall("bus_status");
  assert(fresh.configured === false && fresh.hub === null, "first-use bus_status must not invent a local hub");
  for (const hub of ["local", "https://unconnected.invalid"]) {
    const scoped = await jsonCall("bus_status", { hub });
    assert(scoped.configured === false && scoped.hub === null, "scoped first-use status must not invent an enrollment");
  }
  assert(!existsSync(path.join(env.COMM_STATE, "bus", "server.json")), "first-use bus_status started a local broker");
  assert(!existsSync(path.join(env.COMM_STATE, "bus", "worker.json")), "first-use bus_status started a worker");
  for (const [tool, args] of [
    ["bus_list", {}],
    ["bus_agents", { bus: "general" }],
    ["bus_register", { kind: "codex", session: senderThread }],
    ["bus_dashboard", {}],
    ["bus_create", { name: "must-not-exist" }],
    ["bus_invite", { bus: "general", url: "https://hub.example", out: path.join(taskHome, "never-written") }],
    ["bus_leave", { target: "unknown", bus: "general" }],
  ]) {
    assert(/not connected/.test(await call(tool, { ...args, hub: "https://unconnected.invalid" }, true)),
      `${tool} must reject an unconnected explicit hub instead of choosing a local broker`);
  }
  assert(!existsSync(path.join(taskHome, "never-written")), "unconnected-hub bus_invite wrote an invitation file");
  assert(!existsSync(path.join(env.COMM_STATE, "bus", "server.json")), "explicit-hub operations started a fallback local broker");
  assert(!existsSync(path.join(env.COMM_STATE, "bus", "registrations.json")), "unconnected-hub registration created an adapter");
  assert(/cannot identify this session/.test(await call("bus_register", {}, true)), "missing self must fail without creating an agent");
  await call("bus_create", { name: "photonics", hub: "local" });
  const sender = await jsonCall("bus_register", { bus: "photonics", kind: "codex", session: senderThread, description: "Sender fixture", hub: "local" });
  const recipient = await jsonCall("bus_register", { bus: "photonics", kind: "codex", session: recipientThread });
  assert(sender.id && recipient.id && sender.id !== recipient.id, "distinct current-session registrations");
  const roster = await jsonCall("bus_agents", { bus: "photonics" });
  assert(JSON.stringify(roster).includes(sender.id) && JSON.stringify(roster).includes(recipient.id), "registrations missing from selected bus");
  const general = await jsonCall("bus_agents", { bus: "general" });
  assert(!JSON.stringify(general).includes(sender.id), "private registration leaked into general");
  const message = "literal quotes ' \" $HOME `touch nope` $(touch nope)\nsecond line";
  const hub = (await jsonCall("bus_status")).hub;
  const selectedBefore = readFileSync(path.join(env.COMM_STATE, "bus", "client.json"), "utf8");
  assert((await jsonCall("bus_status", { hub })).hub === hub, "bus_status did not inspect the explicit connected hub");
  assert(JSON.stringify(await jsonCall("bus_list", { hub })).includes("photonics"), "bus_list did not reach the explicit connected hub");
  assert(JSON.stringify(await jsonCall("bus_agents", { hub, bus: "photonics" })).includes(sender.id), "bus_agents did not reach the explicit connected hub");
  const unconnected = await jsonCall("bus_status", { hub: "https://unconnected.invalid" });
  assert(unconnected.configured === false && unconnected.hub === null, "explicit-hub status incorrectly reported the configured default");
  for (const [tool, args] of [
    ["bus_list", {}], ["bus_agents", { bus: "photonics" }], ["bus_dashboard", {}],
    ["bus_create", { name: "must-not-exist" }], ["bus_leave", { target: recipient.id, bus: "photonics" }],
  ]) {
    assert(/not connected/.test(await call(tool, { ...args, hub: "https://unconnected.invalid" }, true)),
      `${tool} must not fall back to an existing default broker`);
  }
  assert(readFileSync(path.join(env.COMM_STATE, "bus", "client.json"), "utf8") === selectedBefore,
    "scoped status/discovery changed the saved broker selection");
  const device = await jsonCall("bus_device", { name: "MCP device fixture", hub });
  assert(device.device === "MCP device fixture", "bus_device failed to update this device label");
  assert(device.device_id === sender.device_id && device.user === sender.user, "device update changed enrollment identity or account");
  assert(device.device_metadata?.tailscale_hostname === "mcp-fixture", "device metadata was not refreshed from Self");
  const invitationFile = path.join(taskHome, "invitation-fixture");
  const issued = await call("bus_invite", { bus: "photonics", url: "https://hub.example", out: invitationFile, hub });
  assert(issued.includes(invitationFile) && /--invite-stdin/.test(issued), "bus_invite must say where the invitation went");
  assert(!issued.includes("commbus1."), "bus_invite returned the invitation code into the transcript");
  assert((statSync(invitationFile).mode & 0o777) === 0o600, "bus_invite file must be private");
  assert(readFileSync(invitationFile, "utf8").startsWith("commbus1."), "bus_invite file must hold the invitation");
  assert(/absolute/.test(await call("bus_invite", { bus: "photonics", url: "https://hub.example", out: "relative-file", hub }, true)),
    "bus_invite must refuse a relative output path");
  assert(/new file/.test(await call("bus_invite", { bus: "photonics", url: "https://hub.example", out: invitationFile, hub }, true)),
    "bus_invite must never replace an existing file");
  assert(/not connected/.test(await call("bus_send", {
    target: recipient.id, from: sender.id, bus: "photonics", message, hub: "https://unconnected.invalid",
  }, true)), "bus_send must forward the selected hub before the subcommand");
  assert(/not connected/.test(await call("bus_receipt", {
    id: "unused-receipt", hub: "https://unconnected.invalid",
  }, true)), "bus_receipt must forward the selected hub before the subcommand");
  assert(!existsSync(path.join(taskHome, "queued.jsonl")), "unknown-hub send must not reach the default broker");
  const sent = await jsonCall("bus_send", { target: recipient.id, from: sender.id, bus: "photonics", message, hub });
  const receiptId = sent.id || sent.message_id || sent.receipt?.id;
  assert(receiptId, "bus_send must return receipt ID");
  for (let i = 0; i < 40 && !existsSync(path.join(taskHome, "queued.jsonl")); i++) {
    await new Promise((r) => setTimeout(r, 250));
  }
  assert(existsSync(path.join(taskHome, "queued.jsonl")), "worker did not queue the bus message");
  const queued = readFileSync(path.join(taskHome, "queued.jsonl"), "utf8").trim().split("\n").map(JSON.parse);
  assert(queued.some((args) => args.some((arg) => arg.includes(recipientThread)) && args.some((arg) => arg.includes(message))), "MCP lost literal message or exact target thread");
  await call("bus_receipt", { id: receiptId, hub });
  assert(/not connected/.test(await call("bus_reply", { id: receiptId, from: recipient.id, message: "wrong hub", hub: "https://unconnected.invalid" }, true)), "bus_reply must preserve operation-specific hub");
  const reply = await jsonCall("bus_reply", { id: receiptId, from: recipient.id, message: "MCP scoped reply", hub });
  assert(reply.conversation_expires_at === sent.conversation_expires_at, "reply changed fixed conversation deadline");
  let replyQueued = false;
  for (let i = 0; i < 40; i++) {
    replyQueued = readFileSync(path.join(taskHome, "queued.jsonl"), "utf8").split("\n").some((line) => line.includes("MCP scoped reply") && line.includes(senderThread));
    if (replyQueued) break;
    await new Promise((r) => setTimeout(r, 250));
  }
  assert(replyQueued, "scoped MCP reply did not queue to original sender");
  assert((await jsonCall("bus_status")).hub === hub, "operation-specific hub changed the default connection");
  const dashboard = await call("bus_dashboard", { hub });
  assert(/^http:\/\/127\.0\.0\.1:\d+\/#token=\S+\s*$/.test(dashboard), "dashboard must return authenticated loopback URL");
  const page = await fetch(dashboard.split("#")[0]);
  assert(page.status === 200 && (await page.text()).includes("<html"), "dashboard asset missing");
  await call("bus_leave", { target: recipient.id, bus: "photonics", hub });
  const after = await jsonCall("bus_agents", { bus: "photonics" });
  assert(!JSON.stringify(after).includes(recipient.id), "leave did not remove membership");
  await call("bus_status");
  console.log(`PASS: mcp-smoke — ${EXPECT.length} tools, current-session registration, scope, literal send/queue, receipt, dashboard, leave`);
} catch (e) {
  failed = true; console.error("FAIL: " + e.message);
} finally {
  child.kill();
  for (const { timer } of pending.values()) clearTimeout(timer);
  spawnSync(communicateCli, ["bus", "stop"], { env, encoding: "utf8", timeout: 15000 });
  if (!process.env.COMM_MCP_TEST_PINNED_HOME) rmSync(taskHome, { recursive: true, force: true });
}
process.exit(failed ? 1 : 0);
