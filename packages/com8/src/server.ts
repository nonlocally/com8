// server.ts — the MCP face. Each tool is a 1:1 projection onto a daemon op via
// kernel.call(); ZERO fabric logic lives here ("two faces, one kernel"). Tools
// are registered from ONE fixed, append-only array so tools/list is byte-stable
// across restarts (the 2026-07-28 cacheable-list rule; backward-benign today).
// Descriptions STEER: messaging is the default plane; seats are the explicit
// interactive escape hatch for surfaces you cannot mailbox.
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { call } from "./kernel.js";

const LONG = 10_000; // extra socket slack for long-poll ops

function text(obj: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(obj, null, 2) }] };
}

// The fixed tool array. ORDER IS STABLE — append only.
type Tool = {
  name: string;
  description: string;
  schema: z.ZodRawShape;
  run: (a: any) => Promise<any>;
};

const TOOLS: Tool[] = [
  {
    name: "agents_list",
    description:
      "The roster: every agent with measured liveness (probed/reported). Start here. Prefer send/ask — they work even when the target is not running.",
    schema: {},
    run: () => call({ op: "agents" }),
  },
  {
    name: "whoami",
    description: "This device and the identities claimed on it.",
    schema: {},
    run: async () => {
      const st = await call({ op: "status" });
      return { device: st?.self?.device,
               user: st?.self?.user?.handle ?? null,
               version: st?.self?.version ?? null,
               identities: Object.keys(st?.identities || {}) };
    },
  },
  {
    name: "send",
    description:
      "Deliver a message to an agent by name, or name@device across a link. Durable store-and-forward: the target need not be alive. The DEFAULT way to talk to any agent.",
    schema: {
      to: z.string().describe("agent name, or name@device"),
      text: z.string(),
      from: z.string().optional().describe("your identity (attribution + reply address)"),
    },
    run: (a) => call({ op: "send", to: a.to, text: a.text, from: a.from || "mcp" }),
  },
  {
    name: "ask",
    description:
      "Send a question and block until the correlated reply arrives (or timeout). Use send for fire-and-forget.",
    schema: {
      to: z.string(),
      text: z.string(),
      from: z.string().optional(),
      timeout_s: z.number().optional().default(60),
    },
    run: (a) =>
      call(
        { op: "ask", to: a.to, text: a.text, from: a.from || "asker", timeout: a.timeout_s ?? 60 },
        { timeoutMs: (a.timeout_s ?? 60) * 1000 + LONG },
      ),
  },
  {
    name: "inbox_read",
    description: "Read an agent's mailbox (durable JSONL). Non-destructive.",
    schema: {
      name: z.string(),
      tail: z.number().optional().default(50),
      after_msg_id: z.string().optional(),
    },
    run: (a) => call({ op: "inbox", name: a.name, tail: a.tail ?? 50, after_msg_id: a.after_msg_id }),
  },
  {
    name: "wait_for_message",
    description:
      "Block until a NEW message lands for name, or timeout. Your inbound wake when your runtime has no socket push — loop on this. (Claude sessions get native socket wake and rarely need it.)",
    schema: {
      name: z.string(),
      timeout_s: z.number().optional().default(60),
      after_msg_id: z.string().optional(),
    },
    run: (a) =>
      call(
        { op: "wait", name: a.name, timeout: a.timeout_s ?? 60, after_msg_id: a.after_msg_id },
        { timeoutMs: (a.timeout_s ?? 60) * 1000 + LONG },
      ),
  },
  {
    name: "claim",
    description:
      "Claim a durable identity on this device: a stable socket, a sweep-proof roster entry, and a mailbox that survives the process behind it.",
    schema: {
      name: z.string(),
      cwd: z.string().optional().describe("the directory this identity works in — recorded with its git ref"),
      worktree: z.boolean().optional().describe("create a dedicated git worktree + branch for this agent"),
    },
    run: (a) => call({ op: "claim", name: a.name, cwd: a.cwd, worktree: !!a.worktree }, { timeoutMs: 90_000 }),
  },
  {
    name: "release",
    description: "Release a claimed identity.",
    schema: { name: z.string() },
    run: (a) => call({ op: "release", name: a.name }),
  },
  {
    name: "group_send",
    description: "Send one message to several agents at once.",
    schema: { names: z.array(z.string()), text: z.string(), from: z.string().optional() },
    run: (a) => call({ op: "group", names: a.names, text: a.text, from: a.from || "mcp" }),
  },
  {
    name: "notify",
    description:
      "Summon the human with a durable reason. Only when you are blocked on a decision only they can make.",
    schema: { reason: z.string(), from: z.string().optional() },
    run: (a) => call({ op: "notify", reason: a.reason, from: a.from || "mcp" }),
  },
  // --- seats: the interactive escape hatch (cluster shells, REPLs, TUIs) ---
  {
    name: "seat_ls",
    description:
      "List interactive seats (terminal surfaces). Seats drive surfaces you cannot mailbox — cluster shells, REPLs, TUIs — NOT agent↔agent talk; use send/ask for that.",
    schema: {},
    run: () => call({ op: "seat", sub: "ls" }),
  },
  {
    name: "seat_spawn",
    description: "Create a seat running a command (optionally on a linked device with --device).",
    schema: { cmd: z.string(), cwd: z.string().optional(), device: z.string().optional() },
    run: (a) => call({ op: "seat", sub: "spawn", cmd: a.cmd, cwd: a.cwd, device: a.device }, { timeoutMs: 40_000 }),
  },
  {
    name: "seat_send",
    description: "Type into a seat (sanitized; separate verified Enter). seat may be device:pane.",
    schema: { seat: z.string(), text: z.string() },
    run: (a) => call({ op: "seat", sub: "send", seat: a.seat, text: a.text }, { timeoutMs: 30_000 }),
  },
  {
    name: "seat_read",
    description: "Read a seat's screen (secrets redacted unless raw=true).",
    schema: { seat: z.string(), lines: z.number().optional().default(40), raw: z.boolean().optional() },
    run: (a) => call({ op: "seat", sub: "read", seat: a.seat, lines: a.lines ?? 40, raw: !!a.raw }, { timeoutMs: 30_000 }),
  },
  {
    name: "seat_state",
    description: "One word: working/idle/booting/approval/dead. The non-blocking done-check — never poll seat_read for this.",
    schema: { seat: z.string() },
    run: (a) => call({ op: "seat", sub: "state", seat: a.seat }, { timeoutMs: 30_000 }),
  },
  {
    name: "seat_wait",
    description: "Block until a seat settles to idle (or timeout); returns final state + tail.",
    schema: { seat: z.string(), timeout_s: z.number().optional().default(120) },
    run: (a) =>
      call({ op: "seat", sub: "wait", seat: a.seat, timeout: a.timeout_s ?? 120 }, { timeoutMs: (a.timeout_s ?? 120) * 1000 + LONG }),
  },
  {
    name: "spawn",
    description:
      "Create an agent: claim a durable identity, launch the CLI in a seat, bind them, and (for claude) adopt via rename-sync. The agent is then reachable by mail AND watchable in a seat.",
    schema: {
      name: z.string(),
      cli: z.enum(["claude", "codex"]).optional(),
      cmd: z.string().optional().describe("raw command, if not using cli"),
      cwd: z.string().optional(),
      worktree: z.boolean().optional().describe("create a dedicated git worktree + branch for this agent"),
    },
    run: (a) => {
      let cmd = a.cmd || (a.cli === "codex" ? (process.env.COM8_CODEX_CMD || "codex") : (process.env.COM8_CLAUDE_CMD || "claude"));
      const adopt = a.cli !== "codex" && !a.cmd; // claude default adopts
      // A com8-spawned claude worker must receive com8 mail as turns without a
      // held-message dialog — scoped to this agent, never the user's settings.
      if (adopt && !cmd.includes("crossSessionInbound"))
        cmd += " --settings '" + JSON.stringify({ crossSessionInbound: "accept" }) + "'";
      return call({ op: "spawn", name: a.name, cmd, cwd: a.cwd, adopt,
                    cli: a.cli, worktree: !!a.worktree },
                  { timeoutMs: 90_000 });
    },
  },
  {
    name: "status",
    description: "Full fabric status: sockets, identities, links, queues — measured, provenance-labelled.",
    schema: {},
    run: () => call({ op: "status" }),
  },
  // --- appended (never reorder: tools/list must stay byte-stable) ---
  {
    name: "seat_respond",
    description:
      "Answer a TUI approval prompt in a seat. Fail-closed: refuses unless a real menu with a clearly-matching option is on screen; never presses Enter on an unknown default.",
    schema: { seat: z.string(), decision: z.enum(["allow", "deny"]).optional() },
    run: (a) =>
      call({ op: "seat", sub: "respond", seat: a.seat, decision: a.decision || "allow" }, { timeoutMs: 30_000 }),
  },
  {
    name: "seat_bind",
    description: "Bind a seat to a claimed identity, so the roster shows both its mailbox and its surface.",
    schema: { seat: z.string(), name: z.string() },
    run: (a) => call({ op: "seat", sub: "bind", seat: a.seat, name: a.name }),
  },
  {
    name: "fan",
    description:
      "Spawn N agents at once and get an addressable group back (prefix-1..N). Brief them with group_send.",
    schema: {
      n: z.number().describe("how many, 1-32"),
      prefix: z.string().describe("names become <prefix>-1 .. <prefix>-N"),
      cli: z.enum(["claude", "codex"]).optional(),
      cwd: z.string().optional(),
    },
    run: (a) => {
      const cmd = a.cli === "codex" ? (process.env.COM8_CODEX_CMD || "codex")
                                    : (process.env.COM8_CLAUDE_CMD || "claude");
      const adopt = a.cli !== "codex";
      return call({ op: "fan", n: a.n, prefix: a.prefix, cmd, cwd: a.cwd, adopt }, { timeoutMs: 120_000 });
    },
  },
  {
    name: "consult",
    description:
      "Ask ANOTHER model one question: spawn-or-reuse a private peer (e.g. codex) and return its answer. Use for a second opinion on a hard judgment call.",
    schema: {
      cli: z.enum(["claude", "codex"]),
      text: z.string(),
      timeout_s: z.number().optional().default(120),
    },
    run: (a) => {
      const cmd = a.cli === "codex" ? (process.env.COM8_CODEX_CMD || "codex")
                                    : (process.env.COM8_CLAUDE_CMD || "claude");
      return call(
        { op: "consult", name: "consult-" + a.cli, cmd, text: a.text,
          timeout: a.timeout_s ?? 120, adopt: a.cli !== "codex" },
        { timeoutMs: (a.timeout_s ?? 120) * 1000 + 30_000 },
      );
    },
  },
  {
    name: "move",
    description:
      "Relocate an agent-being to another device: its transcript, its mailbox, and its identity claim — with the address alive throughout (mail sent mid-move follows it). Refuses a LIVE session unless fork+as is given.",
    schema: {
      name: z.string(),
      device: z.string(),
      as: z.string().optional().describe("rename on arrival (required with fork)"),
      fork: z.boolean().optional().describe("copy instead of move (origin keeps its claim)"),
      spawn: z.boolean().optional().describe("resume it in a seat on arrival"),
      dry_run: z.boolean().optional(),
      allow_missing_workspace: z.boolean().optional().describe(
        "override the default refusal when the target device has no workspace at the recorded path — proceeds anyway, with the agent arriving with a memory of a repo that is not there"),
    },
    run: (a) =>
      call({ op: "move", name: a.name, device: a.device, as: a.as,
             fork: !!a.fork, spawn: !!a.spawn, dry_run: !!a.dry_run,
             allow_missing_workspace: !!a.allow_missing_workspace },
           { timeoutMs: 180_000 }),
  },
  {
    name: "reply",
    description:
      "Answer a question you received via ask. The message you got carries a return token — pass it here with your answer, and the asker's blocked call resolves. This is how a worker records a CONFIRMED result.",
    schema: {
      token: z.string().describe("the return token from the incoming message"),
      text: z.string(),
      from: z.string().optional(),
    },
    run: (a) => call({ op: "reply", token: a.token, text: a.text, from: a.from || "" }),
  },
  {
    name: "seat_stop",
    description:
      "Interrupt whatever a seat is running (sends Escape). Use when a seat is stuck or working on the wrong thing; it does not kill the seat.",
    schema: { seat: z.string() },
    run: (a) => call({ op: "seat", sub: "interrupt", seat: a.seat }, { timeoutMs: 20_000 }),
  },
  {
    name: "link_status",
    description: "Device links: endpoint, kind (device/fleet), seat grant, queue depth, dead-letters, last error.",
    schema: {},
    run: async () => {
      const st = await call({ op: "status" });
      return { device: st?.self?.device, links: st?.links || {} };
    },
  },
  {
    name: "describe",
    description:
      "Set your own card: what you are and what peers should ask you for. Other agents read cards to choose whom to message, so be specific and honest. Derived cards are a first guess — replace yours the first time you know better.",
    schema: {
      name: z.string().describe("the identity to describe (usually your own)"),
      what: z.string().optional(),
      ask_me_for: z.string().optional(),
    },
    run: (a) => call({ op: "describe", name: a.name, what: a.what, ask_me_for: a.ask_me_for }),
  },
  {
    name: "restart",
    description:
      "Bring a spawned agent back using its supervision record (the command, cli and cwd captured at spawn). Use when a seat died or the agent is wedged; it does not lose the mailbox.",
    schema: { name: z.string() },
    run: (a) => call({ op: "restart", name: a.name }, { timeoutMs: 60_000 }),
  },
];

// Shown to the model the moment an MCP client connects. Without it an agent
// gets 24 tool names and no theory of the fabric — written from observed
// confusion, not aspiration.
const INSTRUCTIONS = `com8 gives you a DURABLE IDENTITY and a MAILBOX that outlive your process.

Start with whoami (who am I here?) and agents_list (who else exists, with MEASURED
liveness). If you have held mail, inbox_read shows it.

TWO PLANES — pick the right one:
• MESSAGES are the default for agent-to-agent. send/ask reach an agent BY NAME even
  if it is not running: mail is stored durably and delivered as a turn when it wakes.
  Never poll for an agent to come up — just send. Use ask when you need the answer
  (it blocks for a correlated reply); use send to fire and forget; group_send for many.
  Address another device as name@device.
• SEATS are the ESCAPE HATCH for surfaces you CANNOT mailbox — a cluster login shell,
  a REPL, a TUI. seat_state is the non-blocking done-check (one word); never scrape
  seat_read to find out whether something finished. seat_wait blocks until idle.
  seat_respond is fail-closed: it refuses rather than guess at an approval prompt.
  Do NOT drive another agent through a seat — that is what send/ask are for.

CREATING AGENTS: spawn claims an identity, launches the CLI in a seat, binds them, and
adopts the name — the result is reachable by mail AND watchable. fan spawns a team,
consult asks another model one question. move relocates an agent (transcript + mailbox
+ identity) to another device with its address alive the whole way.

WHEN YOU ARE BLOCKED on a decision only the human can make, call notify with a real
reason — do not stall silently.

YOUR IDENTITY HAS FOUR PARTS, and you can fill two of them in:
• workspace — the directory and git ref you work on. Set it when you are created
  (spawn/claim take cwd); the fabric records the commit so you can be restarted
  or relocated honestly.
• card — one line saying what you ARE and what to ask you for. It is derived from
  your workspace when you are created; if it is wrong or empty, fix it with
  describe. This is how other agents find you: they read cards to decide whom to
  message before they know how. A good card is specific ("proof automation over
  the PhysLean corpus; ask me for tactic suggestions and proof state"), not
  generic ("a helpful assistant").
The other two — place (where you run) and surface (your terminal, if any) — are
measured for you; never assert them.

Not exposed here on purpose: federation, grants, and linking are human trust decisions
made on the CLI, never by an agent.`;

export function buildServer(): McpServer {
  const server = new McpServer({ name: "com8", version: "0.4.0" }, { instructions: INSTRUCTIONS });
  for (const t of TOOLS) {
    server.registerTool(
      t.name,
      { description: t.description, inputSchema: t.schema },
      async (args: any) => {
        try {
          return text(await t.run(args || {}));
        } catch (e: any) {
          return { content: [{ type: "text" as const, text: JSON.stringify({ ok: false, err: String(e?.message || e) }) }], isError: true };
        }
      },
    );
  }
  return server;
}
