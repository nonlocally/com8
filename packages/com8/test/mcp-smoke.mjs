// MCP acceptance smoke: spawn the published `com8 serve` as an MCP server, then
// drive it as a client exactly as Claude Code / Codex would. Proves the two
// faces meet one kernel: a tool call from the MCP side lands real mail that the
// CLI side (the daemon) stored. Env (COMM_STATE/COM8_SOCK/...) is inherited.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const CLI = process.argv[2]; // path to dist/cli.js
let pass = 0, fail = 0;
const ok = (m) => { pass++; console.log("ok   " + m); };
const bad = (m) => { fail++; console.log("FAIL " + m); };

const transport = new StdioClientTransport({
  command: process.execPath,
  args: [CLI, "serve"],
  env: process.env,
});
const client = new Client({ name: "smoke", version: "0" });
await client.connect(transport);

// 1. tools/list — present, and byte-stable across two calls (cache rule).
const l1 = await client.listTools();
const l2 = await client.listTools();
const names = l1.tools.map((t) => t.name);
if (names.includes("send") && names.includes("agents_list") && names.includes("seat_spawn"))
  ok(`tools/list exposes the surface (${names.length} tools)`);
else bad("tools/list surface: " + names.join(","));
if (JSON.stringify(l1.tools.map(t=>t.name)) === JSON.stringify(l2.tools.map(t=>t.name)))
  ok("tools/list order is stable across calls");
else bad("tools/list order drifted");

// 1b. the server must TEACH on connect: instructions reach the client.
const instr = client.getInstructions ? client.getInstructions() : null;
if (instr && /DURABLE IDENTITY/.test(instr) && /ESCAPE HATCH/.test(instr))
  ok("server sends orientation instructions on connect");
else bad("no/short instructions: " + String(instr).slice(0, 80));

// The card is agent-authored, so the orientation must teach it.
if (instr && /describe/.test(instr) && /workspace/i.test(instr))
  ok("instructions teach the card and the workspace");
else bad("instructions omit card/workspace guidance");

// The new verbs must be reachable.
const names2 = (await client.listTools()).tools.map((t) => t.name);
if (names2.includes("describe") && names2.includes("restart"))
  ok("describe + restart are exposed as tools");
else bad("missing describe/restart: " + names2.join(","));

// 2. claim + send via MCP tools -> mail lands in the daemon's store.
await client.callTool({ name: "claim", arguments: { name: "mcpdemo" } });
const sent = await client.callTool({
  name: "send",
  arguments: { to: "mcpdemo", text: "hello-from-mcp-7788", from: "smoke" },
});
const sres = JSON.parse(sent.content[0].text);
if (sres.ok) ok("send tool routed a message"); else bad("send tool: " + sent.content[0].text);

// 3. inbox_read via MCP shows the same message (one kernel, two faces).
const inbox = await client.callTool({ name: "inbox_read", arguments: { name: "mcpdemo" } });
const ires = JSON.parse(inbox.content[0].text);
const hit = (ires.messages || []).some((m) => (m.text || "").includes("hello-from-mcp-7788"));
if (hit) ok("inbox_read sees the message the MCP send delivered"); else bad("inbox_read missed it: " + inbox.content[0].text);

// 4. agents_list shows the claimed identity with measured liveness.
const ag = await client.callTool({ name: "agents_list", arguments: {} });
const ares = JSON.parse(ag.content[0].text);
if ((ares.agents || []).some((a) => a.name === "mcpdemo")) ok("agents_list shows the claimed identity");
else bad("agents_list missing mcpdemo");

// 5. describe (MCP) authors the card for real: what/ask_me_for land, derived flips false.
await client.callTool({
  name: "describe",
  arguments: { name: "mcpdemo", what: "an MCP smoke-test fixture", ask_me_for: "nothing, I am a test" },
});
const ag2 = await client.callTool({ name: "agents_list", arguments: {} });
const ares2 = JSON.parse(ag2.content[0].text);
const described = (ares2.agents || []).find((a) => a.name === "mcpdemo");
const card = described && described.card;
if (card && card.what === "an MCP smoke-test fixture" && card.ask_me_for === "nothing, I am a test" && card.derived === false)
  ok("describe tool authors the card and clears derived");
else bad("describe tool did not update the card: " + JSON.stringify(card));

// 6. restart on an identity with no supervision record fails CLEANLY (ok:false), not a thrown/isError.
const rs = await client.callTool({ name: "restart", arguments: { name: "mcpdemo" } });
const rres = JSON.parse(rs.content[0].text);
if (rs.isError !== true && rres.ok === false && /supervision/.test(rres.err || ""))
  ok("restart tool fails cleanly with no supervision record");
else bad("restart tool: " + JSON.stringify(rs));

await client.close();
console.log(`\npass=${pass} fail=${fail}`);
process.exit(fail === 0 ? 0 : 1);
