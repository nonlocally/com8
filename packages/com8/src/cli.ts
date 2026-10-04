#!/usr/bin/env node
// Keep the legacy com8 MCP face compatible; all installation and CLI behavior
// comes from the combined COM8 artifact, not a second lifecycle implementation.
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { buildServer } from "./server.js";
import { call } from "./kernel.js";

const pkg = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const combined = path.join(pkg, "vendor/communicate/src/com8.mjs");

async function selfTest(): Promise<number | null> {
  const name = "com8-selftest-" + Math.random().toString(16).slice(2, 10);
  const token = "selftest-" + Math.random().toString(16).slice(2, 10), started = Date.now();
  try {
    if (!(await call({ op: "claim", name })).ok) return null;
    if (!(await call({ op: "send", to: name, text: token, from: "com8-setup" })).ok) return null;
    for (let i = 0; i < 20; i++) {
      const inbox = await call({ op: "inbox", name, tail: 5 });
      if ((inbox.messages || []).some((m: any) => m.text === token)) return Date.now() - started;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return null;
  } finally { await call({ op: "release", name }); }
}

async function main() {
  const [command = "serve", ...args] = process.argv.slice(2);
  if (command === "serve") {
    await buildServer().connect(new StdioServerTransport());
    return;
  }
  let handle: string | undefined, claim: string | undefined;
  const forwarded: string[] = [];
  for (let i = 0; i < args.length; i++) {
    if (command === "setup" && ["--handle", "--claim", "--python"].includes(args[i])) {
      const value = args[++i];
      if (!value || value.startsWith("--")) throw new Error("setup option requires a value");
      if (args[i - 1] === "--handle") handle = value.replace(/^@/, "");
      else if (args[i - 1] === "--claim") claim = value;
      else process.env.COM8_PYTHON = value;
    } else if (args[i] === "--no-mcp") forwarded.push("--no-clients");
    else if (args[i] === "--no-persist") forwarded.push("--no-service");
    else forwarded.push(args[i]);
  }
  if (command === "setup" && !args.includes("--no-persist") && process.env.COM8_NO_PERSIST !== "1"
      && !args.includes("--no-service")) forwarded.push("--service");
  const result = spawnSync(process.execPath, [combined, command, ...forwarded], { stdio: "inherit" });
  if (result.error) throw result.error;
  if (result.status !== 0) { process.exitCode = result.status ?? 1; return; }
  if (command === "setup" && !args.includes("--dry-run")) {
    const status = await call({ op: "status" });
    console.log(`daemon version: ${status.self?.version}; source: ${status.self?.source_file || "unreported"}`);
    if (handle) {
      const result = await call({ op: "user-set", handle, via: "npx-setup" });
      if (!result.ok) throw new Error(result.err || "handle claim failed");
      console.log(`user: claimed @${result.user.handle}`);
    } else {
      const user = (await call({ op: "user" })).user;
      console.log(user?.handle ? `user: @${user.handle}` : "user: unclaimed (claim with com8 init)");
    }
    if (claim) {
      const result = await call({ op: "claim", name: claim });
      if (!result.ok) throw new Error(result.err || "identity claim failed");
      console.log(`claimed ${claim}`);
    }
    const elapsed = await selfTest();
    if (elapsed === null) throw new Error("self-test failed: durable inbox did not receive the message");
    console.log(`self-test MEASURED: claim → send → inbox (${elapsed} ms) → released`);
    console.log("Next: com8 pair DEVICE or com8 connect INVITE; com8 doctor reports installed and running code.");
  }
}
main().catch((error) => { console.error(`com8: ${error.message}`); process.exitCode = 1; });
