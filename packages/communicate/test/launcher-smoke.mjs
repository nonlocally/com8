#!/usr/bin/env node
// A checkout (including paths with spaces) must win over an older npm install.
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, symlinkSync, cpSync, realpathSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import os from "node:os";
const pkg = fileURLToPath(new URL("..", import.meta.url));
const root = path.resolve(pkg, "../..");
const temp = mkdtempSync(path.join(os.tmpdir(), "comm-launcher-"));
const repo = path.join(temp, "checkout with spaces");
symlinkSync(root, repo);
const data = path.join(temp, "data");
mkdirSync(path.join(data, "current", "vendor", "bin"), { recursive: true });
mkdirSync(path.join(temp, "bin"));
writeFileSync(path.join(data, "repo-path"), repo + "\n", { mode: 0o600 });
writeFileSync(path.join(data, "current", "vendor", "bin", "communicate"), "#!/bin/sh\necho stale-install\n", { mode: 0o755 });
writeFileSync(path.join(temp, "bin", "node"), "#!/bin/sh\nprintf '%s\\n' \"$@\"\n", { mode: 0o755 });
writeFileSync(path.join(temp, "bin", "npx"), "#!/bin/sh\necho registry-fallback >&2\nexit 99\n", { mode: 0o755 });
const env = { ...process.env, HOME: temp, COMMUNICATE_DATA: data, COMM_STATE: path.join(temp, "state"),
  PATH: path.join(temp, "bin") + path.delimiter + process.env.PATH };
for (const key of ["CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT", "COMMUNICATE_HOME"]) delete env[key];
try {
  const mcp = JSON.parse(readFileSync(path.join(root, "plugins/communicate/.mcp.json"), "utf8")).mcpServers.communicate;
  const r = spawnSync(mcp.command, mcp.args, { env, encoding: "utf8" });
  if (r.status !== 0 || !r.stdout.includes("checkout with spaces/packages/communicate/src/cli.mjs\nserve"))
    throw new Error("checkout MCP not selected: " + r.stdout + r.stderr);
  const cli = spawnSync(path.join(root, "plugins/communicate/bin/communicate"), ["--help"], { env, encoding: "utf8" });
  if (cli.status !== 0 || cli.stdout.includes("stale-install") || !cli.stdout.includes("communicate bus"))
    throw new Error("neighboring checkout CLI not selected: " + cli.stdout + cli.stderr);
  // A normal tar extraction in checkout/dist must never inherit that checkout.
  const shaped = path.join(temp, "checkout-shaped parent");
  const archive = path.join(shaped, "dist/com8-0.3.0");
  mkdirSync(path.join(shaped, "lib"), { recursive: true });
  mkdirSync(path.join(shaped, "packages/communicate"), { recursive: true });
  writeFileSync(path.join(shaped, "lib/common.sh"), "source-only marker");
  writeFileSync(path.join(shaped, "packages/communicate/package.json"), "{}");
  mkdirSync(path.join(archive, "vendor/bin"), { recursive: true });
  cpSync(path.join(pkg, "src"), path.join(archive, "src"), { recursive: true });
  cpSync(path.join(pkg, "package.json"), path.join(archive, "package.json"));
  for (const name of ["com8", "communicate"])
    writeFileSync(path.join(archive, "vendor/bin", name), '#!/bin/sh\nprintf "artifact:%s\\n" "$@"\n', { mode: 0o755 });
  const run = (entry, args) => spawnSync(process.execPath, [path.join(archive, "src", entry), ...args], { env, encoding: "utf8" });
  const artifactRun = run("com8.mjs", ["status", "--json"]);
  if (artifactRun.status || artifactRun.stdout !== "artifact:status\nartifact:--json\n")
    throw new Error("archive selected checkout code: " + artifactRun.stdout + artifactRun.stderr);
  const resolved = spawnSync(process.execPath, ["--input-type=module", "-e",
    `import {communicateCli,isSourceCheckout} from ${JSON.stringify(path.join(archive, "src/paths.mjs"))}; console.log(JSON.stringify({communicateCli,isSourceCheckout}));`], { env, encoding: "utf8" });
  const selection = JSON.parse(resolved.stdout);
  if (selection.isSourceCheckout || selection.communicateCli !== path.join(realpathSync(archive), "vendor/bin/communicate"))
    throw new Error("archive MCP CLI resolution escaped its package");

  // An upgraded package-manager executable follows the activated release.
  // Lifecycle commands still belong to the newly downloaded package.
  const active = path.join(data, "retained release");
  mkdirSync(path.join(active, "src"), { recursive: true });
  writeFileSync(path.join(active, "package.json"), '{"type":"module"}');
  for (const entry of ["cli.mjs", "com8.mjs"])
    writeFileSync(path.join(active, "src", entry), 'console.log(JSON.stringify({active:true,args:process.argv.slice(2)}));');
  rmSync(path.join(data, "current"), { recursive: true });
  symlinkSync(active, path.join(data, "current"));
  writeFileSync(path.join(data, "install.json"), JSON.stringify({ current: active }));
  for (const [entry, args] of [["com8.mjs", ["send", "name", "--", "literal $HOME `id`"]], ["cli.mjs", ["serve"]]]) {
    const result = run(entry, args);
    const actual = result.status === 0 && JSON.parse(result.stdout);
    if (!actual?.active || JSON.stringify(actual.args) !== JSON.stringify(args))
      throw new Error("package-manager invocation ignored active release: " + result.stdout + result.stderr);
  }
  const setup = run("com8.mjs", ["setup", "--no-clients", "--dry-run"]);
  if (setup.status === 0 || !setup.stderr.includes("Release manifest missing"))
    throw new Error("setup was redirected to the active old release");
  const checkoutVersion = spawnSync(process.execPath, [path.join(pkg, "src/com8.mjs"), "version"], { env, encoding: "utf8" });
  if (checkoutVersion.status || !checkoutVersion.stdout.startsWith("COM8 "))
    throw new Error("source checkout incorrectly followed installed activation");
  writeFileSync(path.join(data, "install.json"), JSON.stringify({ current: archive }));
  const mismatch = run("com8.mjs", ["status"]);
  if (!mismatch.status || !mismatch.stderr.includes("ownership record"))
    throw new Error("inconsistent active runtime silently fell back");
  console.log("PASS: launcher-smoke — exact checkout detection, extracted archive isolation, active release dispatch, literal arguments and lifecycle routing");
} finally { rmSync(temp, { recursive: true, force: true }); }
