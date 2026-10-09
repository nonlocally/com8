#!/usr/bin/env node
// The combined release allowlist. Never vendor live configuration or state.
import { cpSync, mkdirSync, rmSync, writeFileSync, readdirSync, chmodSync, readFileSync, existsSync } from "node:fs";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const pkgDir = fileURLToPath(new URL("..", import.meta.url));
const repo = path.resolve(pkgDir, "..", "..");
const vendor = path.join(pkgDir, "vendor");
const pkg = JSON.parse(readFileSync(path.join(pkgDir, "package.json"), "utf8"));
const kernel = JSON.parse(execFileSync(process.env.COM8_PYTHON || "python3", ["-B", "-c",
  "import json,sys;sys.path.insert(0,sys.argv[1]);from com8_payload import KERNEL_FILES;print(json.dumps(KERNEL_FILES))",
  path.join(repo, "lib")], { encoding: "utf8" }));
const copy = (name, dest = name) => {
  const source = path.join(repo, name), target = path.join(vendor, dest);
  if (!existsSync(source)) throw new Error(`Required release file missing: ${name}`);
  mkdirSync(path.dirname(target), { recursive: true });
  cpSync(source, target, { recursive: true,
    filter: (entry) => !entry.split(path.sep).includes("__pycache__") && !entry.endsWith(".pyc") });
};

rmSync(vendor, { recursive: true, force: true });
mkdirSync(vendor, { recursive: true });
for (const file of ["bin/communicate", "bin/com8", "bin/com8-boxed-init", "LICENSE"]) copy(file);
for (const file of readdirSync(path.join(repo, "lib")))
  if (file.endsWith(".sh")) copy("lib/" + file);
for (const file of new Set([...kernel, "bus.py", "bus_broker.py", "bus_service.py", "bus_ui.html"])) copy("lib/" + file);
for (const file of ["bus-graph.js", "bus-graph.css", "bus-graph.LICENSES.txt"]) copy("lib/assets/" + file);
copy("plugins/.claude-plugin");
copy("plugins/communicate");
copy(".agents");
if (existsSync(path.join(repo, "profiles"))) copy("profiles");
cpSync(path.join(repo, "LICENSE"), path.join(pkgDir, "LICENSE"));
// An installed artifact never falls back to an unrelated checkout or registry.
// Resolve from this plugin when the host supplies its root; otherwise use the
// installer-owned stable path. The source-checkout launcher remains unchanged.
writeFileSync(path.join(vendor, "plugins/communicate/bin/communicate-mcp"), `#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$here/../../../../src/cli.mjs" ] && [ -f "$here/../../../release.json" ]; then
  exec node "$here/../../../../src/cli.mjs" serve
fi
data="\${COMMUNICATE_DATA:-$HOME/.local/share/communicate}"
exec node "$data/current/src/cli.mjs" serve
`);
writeFileSync(path.join(vendor, "plugins/communicate/bin/communicate"), `#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
if [ -x "$here/../../../bin/communicate" ] && [ -f "$here/../../../release.json" ]; then
  exec "$here/../../../bin/communicate" "$@"
fi
data="\${COMMUNICATE_DATA:-$HOME/.local/share/communicate}"
exec "$data/current/vendor/bin/communicate" "$@"
`);
const mcpPath = path.join(vendor, "plugins/communicate/.mcp.json");
const mcp = JSON.parse(readFileSync(mcpPath, "utf8"));
mcp.mcpServers.communicate = { type: "stdio", command: "bash", args: ["-c", `for root in "$CLAUDE_PLUGIN_ROOT" "$PLUGIN_ROOT"; do
  if [ -n "$root" ] && [ -f "$root/bin/communicate-mcp" ]; then exec bash "$root/bin/communicate-mcp"; fi
done
data=\${COMMUNICATE_DATA:-$HOME/.local/share/communicate}
exec bash "$data/current/vendor/plugins/communicate/bin/communicate-mcp"`] };
writeFileSync(mcpPath, JSON.stringify(mcp, null, 2) + "\n");
for (const file of ["bin/communicate", "bin/com8", "bin/com8-boxed-init",
  "plugins/communicate/bin/communicate", "plugins/communicate/bin/communicate-mcp"])
  chmodSync(path.join(vendor, file), 0o755);

const git = (...args) => execFileSync("git", args, { cwd: repo, encoding: "utf8" }).trim();
const commit = git("rev-parse", "HEAD");
const dirty = Boolean(git("status", "--porcelain", "--untracked-files=no"));
const files = {};
const walk = (dir) => readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
  entry.isDirectory() ? walk(path.join(dir, entry.name)) : [path.join(dir, entry.name)]);
// Client caches key plugins by version. Include a deterministic content stamp
// so two candidates of the same product version cannot reuse stale instructions.
const pluginContent = createHash("sha256").update(commit);
for (const file of [...walk(vendor), ...walk(path.join(pkgDir, "src"))].sort())
  pluginContent.update(path.relative(pkgDir, file)).update("\0").update(readFileSync(file)).update("\0");
const pluginVersion = `${pkg.version}+${pluginContent.digest("hex").slice(0, 12)}`;
for (const kind of [".claude-plugin", ".codex-plugin"]) {
  const file = path.join(vendor, "plugins/communicate", kind, "plugin.json");
  const plugin = JSON.parse(readFileSync(file, "utf8"));
  plugin.version = pluginVersion;
  writeFileSync(file, JSON.stringify(plugin, null, 2) + "\n");
}
for (const file of walk(vendor).sort()) files[path.relative(vendor, file)] =
  createHash("sha256").update(readFileSync(file)).digest("hex");
const daemonVersion = readFileSync(path.join(vendor, "lib/com8.py"), "utf8").match(/^COM8_VERSION = "([^"]+)"/m)?.[1];
const packageFiles = {};
for (const file of [...walk(path.join(pkgDir, "src")), path.join(pkgDir, "package.json"), path.join(pkgDir, "LICENSE")].sort())
  packageFiles[path.relative(pkgDir, file)] = createHash("sha256").update(readFileSync(file)).digest("hex");
writeFileSync(path.join(vendor, "release.json"), JSON.stringify({ schema: 1, product: "COM8", version: pkg.version,
  source: { repository: "https://github.com/nonlocally/com8", commit, dirty },
  components: { package: pkg.name, plugin: "communicate@communicate", pluginVersion, daemon: daemonVersion }, files, packageFiles }, null, 2) + "\n");
writeFileSync(path.join(vendor, "VERSION"), `${pkg.version}+${commit.slice(0, 12)}${dirty ? ".dirty" : ""}\n`);
console.log(`vendored COM8 ${pkg.version} (${Object.keys(files).length} files, ${commit.slice(0, 12)})`);
