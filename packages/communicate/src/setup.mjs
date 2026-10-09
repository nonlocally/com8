// setup/doctor for @aadarwal/communicate — stabilize the payload, register
// both ecosystems, reversibly. Never touches CLI-owned plugin state files;
// never hand-edits ~/.codex/config.toml.
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync, rmSync, symlinkSync, renameSync, readlinkSync, realpathSync, chmodSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { isDeepStrictEqual } from "node:util";
import { copyRuntimeDependencies, hasRuntimeDependencies } from "./runtime-deps.mjs";
import { home, dataRoot, currentLink, ledgerPath, readJson, writeJson, hash, linkTarget, switchCurrent,
  withInstallLock, executable, stateRoot, daemonRequest, installService, uninstallService, unloadService, loadService, assertManagedPath, installLockStatus } from "./lifecycle.mjs";
import { communicateCli } from "./paths.mjs";
import { buildIntegration, removeIntegration } from "./integration.mjs";
import { readCodexSettings, writeCodexSettings } from "./codex-settings.mjs";
import { inspectBus, busSummary, busStateDir, stopBus } from "./bus-setup.mjs";

const pkgDir = path.resolve(fileURLToPath(new URL("..", import.meta.url)));
const pkg = JSON.parse(readFileSync(path.join(pkgDir, "package.json"), "utf8"));
const settingsPath = () => path.join(process.env.CLAUDE_CONFIG_DIR || path.join(home(), ".claude"), "settings.json");
const codexHome = () => process.env.CODEX_HOME || path.join(home(), ".codex");
const MARKET_ID = "communicate";
const PLUGIN_ID = "communicate@communicate";

const log = (s) => console.log(s);
const clientExec = (command, args) => spawnSync(command, args, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 30000 });
const hasCodex = () => clientExec("codex", ["--version"]).status === 0;

function parseFlags(argv) {
  const f = { claude: false, codex: false, dryRun: false, uninstall: false, purge: false, yes: false, service: false, noClients: false, serviceInherit: [] };
  for (const a of argv) {
    if (a === "--claude") f.claude = true;
    else if (a === "--codex") f.codex = true;
    else if (a === "--dry-run") f.dryRun = true;
    else if (a === "--uninstall") f.uninstall = true;
    else if (a === "--purge") f.purge = true;
    else if (a === "--service") f.service = true;
    else if (a === "--no-service") { f.service = false; f.noService = true; }
    else if (a === "--no-clients") f.noClients = true;
    else if (a.startsWith("--service-inherit=")) {
      const name = a.slice("--service-inherit=".length);
      if (!["PATH", "COM8_SOCK_DIR", "COM8_SESSIONS_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "COM8_TMUX_SOCKET"].includes(name))
        throw new Error(`unsupported service environment variable: ${name}`);
      f.serviceInherit.push(name);
    }
    else if (a === "-y" || a === "--yes") f.yes = true;
    else throw new Error(`unknown flag: ${a}`);
  }
  if (f.noClients && (f.claude || f.codex)) throw new Error("--no-clients cannot be combined with --claude or --codex");
  if (f.service && f.noService) throw new Error("--service and --no-service cannot be combined");
  f.selective = f.claude || f.codex;
  if (f.purge && f.selective) throw new Error("--purge requires full uninstall; do not combine it with --claude or --codex");
  if (!f.noClients && !f.claude && !f.codex) { f.claude = true; f.codex = true; }
  if (f.purge && !f.uninstall) throw new Error("--purge only applies to uninstall; identity state is always preserved");
  return f;
}

function stabilize(dry) {
  const manifestPath = path.join(pkgDir, "vendor/release.json");
  if (!existsSync(manifestPath)) throw new Error("Release manifest missing; build or reinstall the COM8 artifact");
  const manifest = readJson(manifestPath);
  if (manifest.version !== pkg.version) throw new Error("Package version and release manifest disagree");
  const manifestHash = hash(readFileSync(manifestPath)).slice(0, 12);
  const dest = path.join(dataRoot(), `${pkg.version}-${manifestHash}`);
  assertManagedPath(dest);
  const hasDeps = hasRuntimeDependencies(pkgDir);
  if (!hasDeps) throw new Error("MCP runtime dependencies are missing; install the release package before setup");
  if (dry) {
    log(`[dry-run] would copy payload -> ${dest} (vendor, src, package.json${hasDeps ? ", node_modules" : ""})`);
    log(`[dry-run] would point symlink ${currentLink()} -> ${dest}`);
    return dest;
  }
  // Verify the source artifact before staging. Never replace an immutable
  // release directory in place: old processes and rollback keep using it.
  const verify = (root) => {
    for (const [prefix, files] of [["vendor", manifest.files], ["", manifest.packageFiles || {}]])
      for (const [name, expected] of Object.entries(files)) {
        const base = path.join(root, prefix), file = path.resolve(base, name);
        if (!file.startsWith(base + path.sep) || hash(readFileSync(file)) !== expected)
          throw new Error(`Release payload verification failed: ${name} in ${root}`);
      }
  };
  verify(pkgDir);
  if (existsSync(dest)) {
    if (readFileSync(path.join(dest, "vendor/release.json"), "utf8") !== readFileSync(manifestPath, "utf8"))
      throw new Error(`Release path collision: ${dest}`);
    verify(dest);
    if (!hasRuntimeDependencies(dest, { localOnly: true }))
      throw new Error("Installed MCP dependencies are missing. Use the retained release archive to uninstall --purge, then setup again; runtime state is preserved.");
    return dest;
  }
  const tmp = dest + `.tmp-${process.pid}`;
  rmSync(tmp, { recursive: true, force: true });
  mkdirSync(tmp, { recursive: true });
  for (const item of ["vendor", "src", "package.json", "LICENSE"]) cpSync(path.join(pkgDir, item), path.join(tmp, item), { recursive: true });
  for (const file of ["cli.mjs", "com8.mjs"]) chmodSync(path.join(tmp, "src", file), 0o755);
  if (hasDeps) copyRuntimeDependencies(pkgDir, tmp);
  // The release's plugin launcher is relative to its bundled Node source.
  // Keep it immutable so repeat setup verifies the same release hashes.
  renameSync(tmp, dest);
  log(`payload staged -> ${dest}`);
  return dest;
}

function readSettings() {
  const p = settingsPath();
  assertManagedPath(p);
  if (!existsSync(p)) return {};
  try { return JSON.parse(readFileSync(p, "utf8")); }
  catch { throw new Error(`refusing to touch unparsable ${p} — fix it first`); }
}

function writeSettings(obj, dry, why) {
  const p = settingsPath();
  assertManagedPath(p);
  if (dry) { log(`[dry-run] would ${why} in ${p}`); return; }
  mkdirSync(path.dirname(p), { recursive: true, mode: 0o700 });
  const hadSettings = existsSync(p);
  if (hadSettings) writeFileSync(`${p}.communicate-backup-${process.hrtime.bigint()}-${process.pid}`, readFileSync(p), { mode: 0o600, flag: "wx" });
  const temp = `${p}.tmp-${process.pid}`;
  writeFileSync(temp, JSON.stringify(obj, null, 2) + "\n", { mode: 0o600, flag: "wx" });
  renameSync(temp, p);
  log(`${why} in ${p} (${hadSettings ? "private backup written" : "private file created"})`);
}

function claudePlugin() {
  const result = clientExec("claude", ["plugin", "list", "--json"]);
  let rows;
  try { rows = result.status === 0 ? JSON.parse(result.stdout) : null; } catch {}
  if (!Array.isArray(rows)) throw new Error("Could not inspect Claude user-scope installed plugin state; no client changes made");
  if (rows.some((row) => row.id === PLUGIN_ID && row.scope !== "user"))
    throw new Error("Another Claude scope uses the COM8 plugin marketplace; preserve/manage that scope before replacing its shared registration");
  const matches = rows.filter((row) => row.id === PLUGIN_ID && row.scope === "user");
  if (matches.length > 1 || (matches.length && typeof matches[0].enabled !== "boolean"))
    throw new Error("Claude user-scope installed/enabled state is ambiguous; no client changes made");
  return { installed: matches.length === 1, enabled: matches[0]?.enabled ?? false };
}

function snapshotClaude() {
  const settings = readSettings();
  if (!executable("claude")) return { settings, available: false };
  const state = claudePlugin();
  if (state.installed && !settings.extraKnownMarketplaces?.[MARKET_ID])
    throw new Error("Installed Claude COM8 plugin has no restorable user marketplace; preserve its registration before setup");
  const restoreMarket = structuredClone(settings.extraKnownMarketplaces?.[MARKET_ID]);
  const source = restoreMarket?.source?.path;
  const mutable = path.resolve(currentLink());
  const aliases = [mutable];
  if (existsSync(path.dirname(mutable))) aliases.push(path.join(realpathSync(path.dirname(mutable)), path.basename(mutable)));
  // Old packages registered through `current`. Pin that managed pointer before
  // switching it, or uninstall would reinstall the new package as the original.
  // Ordinary user symlinks elsewhere remain entirely user-controlled.
  if (source && aliases.some((base) => path.resolve(source).startsWith(base + path.sep)))
    restoreMarket.source.path = realpathSync(source);
  return { settings, restoreMarket, ...state, available: true };
}

function checkClaudeOwnership(record, snapshot) {
  if (!record) return;
  if (typeof record.previousInstalled !== "boolean" || typeof record.previousPluginEnabled !== "boolean")
    throw new Error("Earlier installer did not record original Claude installed/enabled state. Preserve and manage that registration manually before retrying; no client changes made.");
  if (!snapshot.available || !snapshot.installed || !snapshot.enabled ||
      snapshot.settings.enabledPlugins?.[PLUGIN_ID] !== true ||
      snapshot.settings.extraKnownMarketplaces?.[MARKET_ID]?.source?.path !== record.marketRoot)
    throw new Error("Claude plugin registration or enablement changed after setup; user changes are preserved. Reconcile the owned registration before setup, rollback or uninstall.");
}

// The plugins this release's marketplace provides. Another plugin enabled under
// the same marketplace name stops resolving once that registration points here.
const shippedPlugins = () => JSON.parse(readFileSync(path.join(pkgDir, "vendor/plugins/.claude-plugin/marketplace.json"), "utf8"))
  .plugins.map((plugin) => `${plugin.name}@${MARKET_ID}`);

function claudeInstall(dry, integration) {
  const marketRoot = integration ? path.join(integration.root, "plugins") : path.join(currentLink(), "vendor", "plugins");
  log(`Claude configuration: ${path.dirname(settingsPath())}${process.env.CLAUDE_CONFIG_DIR ? " (CLAUDE_CONFIG_DIR)" : ""}`);
  if (!executable("claude")) { log("Claude CLI unavailable; rerun com8 setup --claude after installing it."); return false; }
  const s = readSettings();
  const previous = s.extraKnownMarketplaces?.[MARKET_ID]?.source;
  const previousSource = previous?.path || previous?.repo || previous?.url;
  if (previousSource && previousSource !== marketRoot) {
    const shipped = new Set(shippedPlugins());
    const stranded = Object.entries(s.enabledPlugins || {})
      .filter(([id, enabled]) => enabled === true && id.endsWith(`@${MARKET_ID}`) && !shipped.has(id)).map(([id]) => id);
    if (stranded.length)
      log(`warning: ${dry ? "setup would replace" : "replacing"} the existing Claude marketplace "${MARKET_ID}" (${previousSource}). ` +
        `These enabled plugins come from it and stop resolving while COM8 is installed: ${stranded.join(", ")}. ` +
        `They stay enabled in ${settingsPath()}; com8 uninstall restores the previous marketplace.`);
  }
  s.extraKnownMarketplaces = { ...(s.extraKnownMarketplaces || {}), [MARKET_ID]: { source: { source: "directory", path: marketRoot } } };
  s.enabledPlugins = { ...(s.enabledPlugins || {}), [PLUGIN_ID]: true };
  writeSettings(s, dry, `add extraKnownMarketplaces.${MARKET_ID} + enabledPlugins["${PLUGIN_ID}"]`);
  const commands = [["plugin", "marketplace", "add", marketRoot], ["plugin", "update", PLUGIN_ID, "--scope", "user"]];
  if (dry) {
    for (const args of commands) log(`[dry-run] would run: claude ${args.join(" ")}`);
    return true;
  }
  const probe = spawnSync("claude", ["--version"], {stdio: "ignore", timeout: 30000});
  if (probe.error?.code === "ENOENT") {
    log("Claude CLI not found; after installing it, rerun this archive's communicate setup --claude.");
    return;
  }
  const run = (args) => spawnSync("claude", args, {encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 30000}).status === 0;
  if (!run(commands[0])) throw new Error("Claude marketplace registration failed; rerun this archive's communicate setup --claude.");
  if (!run(commands[1]) && !run(["plugin", "install", PLUGIN_ID, "--scope", "user"]))
    throw new Error(`Claude plugin refresh failed; run claude plugin update ${PLUGIN_ID} --scope user.`);
  const expected = JSON.parse(readFileSync(path.join(marketRoot, "communicate", ".claude-plugin", "plugin.json"), "utf8")).version;
  const matches = () => {
    const listed = clientExec("claude", ["plugin", "list", "--json"]);
    try {
      const installed = listed.status === 0 ? JSON.parse(listed.stdout) : [];
      return Array.isArray(installed) && installed.some((p) => p.id === PLUGIN_ID && p.scope === "user" && p.version === expected);
    } catch { return false; }
  };
  // Some client versions treat changed build metadata as the same version.
  // Refresh that cache through its CLI, never by editing the client's registry.
  if (!matches() && !(run(["plugin", "uninstall", PLUGIN_ID, "--scope", "user"])
      && run(["plugin", "install", PLUGIN_ID, "--scope", "user"]) && matches()))
    throw new Error(`Claude plugin version verification failed; run claude plugin update ${PLUGIN_ID} --scope user.`);
  log("Claude plugin installed/refreshed via CLI. Restart Claude Code to load COM8's skills, commands, CLI, and MCP tools.");
  return true;
}

function claudeUninstall(dry, record = {}) {
  const s = readSettings();
  if (s.extraKnownMarketplaces?.[MARKET_ID]?.source?.path !== (record.marketRoot || path.join(currentLink(), "vendor/plugins"))) {
    log("kept Claude registration and released installer ownership: it no longer points at this installation"); return true;
  }
  if (!dry && !executable("claude")) { log("kept Claude registration: its CLI is unavailable to remove the registry entry"); return false; }
  const before = snapshotClaude();
  checkClaudeOwnership(record, before);
  const original = structuredClone(s);
  if (record.previousMarket) s.extraKnownMarketplaces[MARKET_ID] = record.previousMarket;
  else delete s.extraKnownMarketplaces[MARKET_ID];
  if (s.enabledPlugins?.[PLUGIN_ID] === true) {
    if (record.previousEnabled !== undefined) s.enabledPlugins[PLUGIN_ID] = record.previousEnabled;
    else delete s.enabledPlugins[PLUGIN_ID];
  }
  if (!dry) {
    const failures = restoreClaude({ settings: { extraKnownMarketplaces: { [MARKET_ID]: record.previousMarket }, enabledPlugins: { [PLUGIN_ID]: record.previousEnabled } },
      installed: record.previousInstalled, enabled: record.previousPluginEnabled, available: true });
    if (failures.length) {
      const compensation = restoreClaude(before);
      writeSettings(original, false, "retain installer ownership after incomplete Claude registry removal");
      throw new Error(`Claude registry restoration failed: ${failures.join("; ")}${compensation.length ? "; current registration recovery needs attention: " + compensation.join("; ") : "; current registration restored"}`);
    }
  }
  writeSettings(s, dry, "remove owned COM8 plugin settings (restore previous values)");
  return true;
}

function codexMarketplace(strict = false) {
  if (!executable("codex")) return null;
  const r = spawnSync("codex", ["plugin", "marketplace", "list", "--json"], { encoding: "utf8", timeout: 30000 });
  if (r.status !== 0) { if (strict) throw new Error("Could not inspect Codex marketplace ownership"); return null; }
  try {
    const rows = JSON.parse(r.stdout);
    return (Array.isArray(rows) ? rows : rows.marketplaces || []).find((m) => m.name === MARKET_ID) || null;
  } catch { if (strict) throw new Error("Could not parse Codex marketplace ownership"); return null; }
}
function codexPlugin() {
  const result = clientExec("codex", ["plugin", "list", "--marketplace", MARKET_ID, "--json"]);
  if (result.status !== 0) throw new Error("Could not inspect Codex installed plugin state; no client changes made");
  let rows;
  try { rows = JSON.parse(result.stdout); } catch { throw new Error("Could not parse Codex installed plugin state"); }
  if (!Array.isArray(rows.installed)) throw new Error("Codex plugin list must expose installed/enabled state; update Codex before setup");
  const matches = rows.installed.filter((row) => row.pluginId === PLUGIN_ID);
  if (matches.length > 1 || (matches.length && (matches[0].installed !== true || typeof matches[0].enabled !== "boolean")))
    throw new Error("Codex installed plugin state is ambiguous; no client changes made");
  return { installed: matches.length === 1, enabled: matches[0]?.enabled ?? false };
}

const codexRoot = (market) => market?.marketplaceSource?.source || market?.root;
const sameLocalRoot = (a, b) => typeof a === "string" && typeof b === "string" &&
  (existsSync(a) ? realpathSync(a) : path.resolve(a)) === (existsSync(b) ? realpathSync(b) : path.resolve(b));
async function snapshotCodex({ preflight = true } = {}) {
  if (!executable("codex")) return null;
  assertManagedPath(path.join(codexHome(), "config.toml"));
  // Codex rejects an explicit CODEX_HOME that does not exist, even for list.
  // Do this only during selected client setup/restoration, after path checks;
  // dry-run and doctor must not create a client home. Existing modes stay intact.
  mkdirSync(codexHome(), { recursive: true, mode: 0o700 });
  const market = codexMarketplace(true), plugin = codexPlugin();
  if (market && market.marketplaceSource?.sourceType !== "local")
    throw new Error("Codex COM8 marketplace is not a verified local source; preserve/export it before switching installations");
  if (plugin.installed && !market) throw new Error("Installed Codex COM8 plugin has no restorable marketplace; no client changes made");
  return { market, ...plugin, settings: await readCodexSettings({ preflight }) };
}

function checkCodexOwnership(record, snapshot) {
  if (!record) return;
  if (!record.original || !("ownedSettings" in record))
    throw new Error("Earlier installer did not record original Codex plugin settings. Preserve the current configuration and restore/manage its registration manually before retrying; no client changes made.");
  if (!sameLocalRoot(codexRoot(snapshot?.market), record.marketRoot) || !snapshot?.installed ||
      !isDeepStrictEqual(snapshot.settings, record.ownedSettings))
    throw new Error("Codex plugin registration or settings changed after setup. User edits are preserved; reconcile the owned registration/settings before setup, rollback or uninstall.");
}

async function codexInstall(dry, integration, before) {
  assertManagedPath(path.join(codexHome(), "config.toml"));
  log(`Codex configuration: ${codexHome()}${process.env.CODEX_HOME ? " (CODEX_HOME)" : ""}`);
  const marketRoot = integration?.root || path.join(currentLink(), "vendor");
  const cmds = [["plugin", "marketplace", "add", marketRoot], ["plugin", "add", PLUGIN_ID]];
  if (!hasCodex()) {
    log("codex CLI not found — run these once it is installed:");
    for (const c of cmds) log(`  codex ${c.join(" ")}`);
    log("After installing Codex, rerun this archive's communicate setup --codex to register its local MCP payload.");
    return false;
  }
  for (const c of cmds) {
    if (dry) { log(`[dry-run] would run: codex ${c.join(" ")}`); continue; }
    const r = clientExec("codex", c);
    const out = (r.stdout || "") + (r.stderr || "");
    if (r.status === 0) log(`codex ${c.join(" ")} — ok`);
    else {
      // Switching a registered checkout to this release is explicit setup.
      if (c[1] === "marketplace" && c[2] === "add") {
        clientExec("codex", ["plugin", "marketplace", "remove", MARKET_ID]);
        if (clientExec("codex", c).status === 0) continue;
      }
      throw new Error(`codex ${c.join(" ")} failed (${out.trim().slice(0, 200)})`);
    }
  }
  if (!dry) {
    // `plugin add` enables a plugin. Keep every prior per-tool policy and
    // restore only the installer-owned enabled change on later uninstall.
    const wanted = { ...(before?.settings || {}), enabled: true };
    const current = await readCodexSettings();
    if (!isDeepStrictEqual(current, wanted) && !isDeepStrictEqual(current, { enabled: true }))
      throw new Error("Codex plugin settings changed during registration; refusing to replace custom settings");
    await writeCodexSettings(wanted, current);
    const installed = codexPlugin();
    if (!installed.installed || !installed.enabled) throw new Error("Codex plugin did not become installed and enabled");
  }
  log("Codex: start a NEW thread to see the skills and MCP tools.");
  return true;
}

async function codexUninstall(dry, record = {}) {
  assertManagedPath(path.join(codexHome(), "config.toml"));
  if (!executable("codex")) { log("kept Codex registration: its CLI is unavailable to verify ownership"); return false; }
  const market = codexMarketplace(true);
  const ownedRoot = record.marketRoot || path.join(currentLink(), "vendor");
  if (!market || !sameLocalRoot(codexRoot(market), ownedRoot)) {
    log("kept Codex registration and released installer ownership: it no longer points at this installation"); return true;
  }
  const before = await snapshotCodex();
  checkCodexOwnership(record, before);
  if (dry) { log("[dry-run] would restore original Codex marketplace, installation and plugin settings"); return true; }
  const failures = await restoreCodex(record.original);
  if (failures.length) {
    const compensation = await restoreCodex(before);
    throw new Error(`Codex original state restoration failed; installer ownership retained. ${failures.join("; ")}${compensation.length ? "; current registration recovery needs attention: " + compensation.join("; ") : "; current registration restored"}`);
  }
  log("Codex original marketplace, installation and plugin settings restored");
  return true;
}

async function restoreCodex(snapshot) {
  // CLI-owned registry files are never edited directly.
  if (!snapshot) return [];
  const failures = [];
  try {
    const currentSettings = await readCodexSettings();
    const expected = [snapshot.settings, { ...(snapshot.settings || {}), enabled: true }, { enabled: true }, null];
    if (!expected.some((settings) => isDeepStrictEqual(currentSettings, settings)))
      throw new Error("Codex plugin settings changed during activation; user edits preserved, reconcile the registration manually");
    if (codexPlugin().installed && clientExec("codex", ["plugin", "remove", PLUGIN_ID]).status !== 0)
      throw new Error("Codex plugin removal failed");
    if (codexMarketplace(true) && clientExec("codex", ["plugin", "marketplace", "remove", MARKET_ID]).status !== 0)
      throw new Error("Codex marketplace removal failed");
    const previous = codexRoot(snapshot.market);
    if (previous && clientExec("codex", ["plugin", "marketplace", "add", previous]).status !== 0)
      throw new Error("Codex marketplace restoration failed");
    if (snapshot.installed && clientExec("codex", ["plugin", "add", PLUGIN_ID]).status !== 0)
      throw new Error("Codex installed plugin restoration failed");
    const current = await readCodexSettings();
    // Removal must leave either no table or the freshly-added default table.
    if (current !== null && !isDeepStrictEqual(current, { enabled: true }))
      throw new Error("Codex plugin settings changed during restoration; user edits preserved");
    await writeCodexSettings(snapshot.settings, current);
    const after = codexPlugin();
    if (after.installed !== snapshot.installed || after.enabled !== snapshot.enabled)
      throw new Error("Codex installed/enabled state did not restore exactly");
  } catch (error) { failures.push(error.message); }
  return failures;
}

function restoreClaude(snapshot) {
  if (!snapshot?.available) return [];
  const settings = snapshot.settings;
  const failures = [];
  const run = (args) => {
    if (clientExec("claude", args).status !== 0) failures.push(`claude ${args.join(" ")}`);
  };
  // Removal of a missing plugin is harmless; the marketplace operation below
  // is the registration boundary whose success must be checked.
  clientExec("claude", ["plugin", "uninstall", PLUGIN_ID, "--scope", "user"]);
  run(["plugin", "marketplace", "remove", MARKET_ID]);
  const market = snapshot.restoreMarket || settings.extraKnownMarketplaces?.[MARKET_ID];
  if (market) {
    const source = market.source?.path || market.source?.repo || market.source?.url;
    if (!source) failures.push("previous Claude marketplace has an unsupported source; restore its saved settings manually");
    else {
      run(["plugin", "marketplace", "add", source]);
      if (snapshot.installed) {
        run(["plugin", "install", PLUGIN_ID, "--scope", "user"]);
        try {
          const state = claudePlugin();
          // Claude treats an already-enabled/disabled operation as an error.
          if (state.installed && state.enabled !== snapshot.enabled)
            run(["plugin", snapshot.enabled ? "enable" : "disable", PLUGIN_ID, "--scope", "user"]);
        } catch (error) { failures.push(error.message); }
      }
    }
  }
  try {
    const restored = claudePlugin();
    if (restored.installed !== snapshot.installed || restored.enabled !== snapshot.enabled)
      failures.push("Claude user-scope installed/enabled state did not restore exactly");
  } catch (error) { failures.push(error.message); }
  return failures;
}

function restoreService(record) {
  unloadService(record);
  if (record.backup) {
    cpSync(record.backup, record.path);
    if (record.previousMode !== undefined) chmodSync(record.path, record.previousMode);
    loadService(record, { active: record.previousActive ?? true, enabled: record.previousEnabled ?? true,
      unitFileState: record.previousUnitFileState });
  } else rmSync(record.path, { force: true });
}

// Uninstall keeps people's data; say exactly what stays and where.
function remainingAfterUninstall() {
  const lines = [];
  const busDir = busStateDir();
  if (existsSync(busDir)) {
    const bus = inspectBus({ offline: true });
    lines.push(bus.configured && !bus.local
      ? `bus: this device stays enrolled at ${bus.hub}; its credential and registrations remain in ${busDir}. Its agents stay listed there until they leave or the hub administrator revokes this device.`
      : `bus: local registrations and messages remain in ${busDir}`);
  }
  const models = process.env.COM8_MODEL_CONFIG || path.join(process.env.XDG_CONFIG_HOME || path.join(home(), ".config"), "com8/models");
  if (existsSync(models)) lines.push(`model connections and their keys remain in ${models}`);
  const claudeDir = path.dirname(settingsPath());
  const backups = existsSync(claudeDir) ? readdirSync(claudeDir).filter((name) => name.startsWith("settings.json.communicate-backup-")).length : 0;
  if (backups) lines.push(`${backups} private Claude settings backup${backups === 1 ? "" : "s"} remain in ${claudeDir} (settings.json.communicate-backup-*)`);
  return lines;
}

export async function runSetup(argv) {
  const f = parseFlags(argv);
  if (!f.uninstall && f.claude) assertManagedPath(settingsPath());
  if (!f.uninstall && f.codex) assertManagedPath(path.join(codexHome(), "config.toml"));
  if (f.dryRun) {
    if (f.uninstall) {
      log(`[dry-run] would remove only owned registrations and executable links; preserve ${stateRoot()}`);
      if (!f.selective && existsSync(busStateDir()))
        log("[dry-run] would stop the bus worker and any owned local broker; registrations and enrollment are kept");
      const saved = readJson(ledgerPath());
      if (!f.selective) await uninstallService(saved.service, true);
    } else {
      stabilize(true);
      const integration = (f.claude || f.codex) ? buildIntegration(pkgDir, { dry: true }) : null;
      if (f.claude) claudeInstall(true, integration);
      if (f.codex) await codexInstall(true, integration);
      const saved = readJson(ledgerPath());
      if (!f.noService && (f.service || saved.service)) await installService(true, undefined, saved.service, { inherit: f.serviceInherit });
    }
    return;
  }
  await withInstallLock(async () => {
    const saved = readJson(ledgerPath(), { schema: 1, releases: [], clients: {}, links: {} });
    saved.clients ||= {}; saved.links ||= {}; saved.releases ||= []; saved.integrations ||= [];
    if (f.uninstall) {
      const inspectClients = Object.keys(saved.clients).length > 0 || saved.integrations.length > 0;
      if (f.claude && saved.clients.claude && claudeUninstall(false, saved.clients.claude)) {
        delete saved.clients.claude; writeJson(ledgerPath(), saved);
      }
      if (f.codex && saved.clients.codex && await codexUninstall(false, saved.clients.codex)) {
        delete saved.clients.codex; writeJson(ledgerPath(), saved);
      }
      if (!f.selective && await uninstallService(saved.service)) { delete saved.service; writeJson(ledgerPath(), saved); }
      // An uninstalled machine must not keep polling a hub or delivering messages.
      if (!f.selective) {
        try {
          const stopped = stopBus();
          if (stopped) log(stopped === "stopped" ? "stopped the bus worker and any owned local broker" : "the bus worker and local broker are stopping");
        } catch { log("warning: could not stop the bus worker; run com8 bus stop"); }
      }
      if (!f.selective) for (const [file, target] of Object.entries(saved.links)) {
        try {
          if (readlinkSync(file) === target) { rmSync(file); delete saved.links[file]; }
          else { log(`kept changed executable link and released ownership: ${file}`); delete saved.links[file]; }
        } catch (e) { delete saved.links[file]; if (e.code !== "ENOENT") log(`kept user-owned executable: ${file}`); }
      }
      if (!f.selective && !Object.keys(saved.clients).length && saved.integrations.length) {
        const roots = [readSettings().extraKnownMarketplaces?.[MARKET_ID]?.source?.path, codexMarketplace()?.root].filter(Boolean);
        saved.integrations = saved.integrations.filter((item) => {
          if (roots.some((root) => root === item.root || root.startsWith(item.root + path.sep))) {
            log(`kept client integration still referenced by a registration: ${item.root}`); return true;
          }
          return !removeIntegration(item);
        });
        if (!saved.integrations.some((item) => item.root === saved.integration?.root)) delete saved.integration;
      }
      // Persist successful detachments even when a later purge precondition
      // refuses deletion. A retry must reflect what was actually removed.
      writeJson(ledgerPath(), saved);
      if (f.purge) {
        if (f.selective || Object.keys(saved.clients).length || saved.service || Object.keys(saved.links).length || saved.integrations.length)
          throw new Error("Cannot purge while integrations remain; inspect ownership changes with com8 doctor");
        const runtime = path.resolve(stateRoot());
        const registered = inspectClients ? [readSettings().extraKnownMarketplaces?.[MARKET_ID]?.source?.path,
          codexMarketplace()?.root].filter(Boolean).map((entry) => path.resolve(entry)) : [];
        const running = await daemonRequest();
        for (const release of saved.releases) {
          const resolved = path.resolve(release);
          assertManagedPath(resolved);
          if (path.dirname(resolved) !== path.resolve(dataRoot()) || !existsSync(path.join(resolved, "vendor/release.json")))
            throw new Error(`Refusing unrecognized release directory: ${release}`);
          if (runtime === resolved || runtime.startsWith(resolved + path.sep)) throw new Error("Runtime state is inside a release; move it before purging code");
          if (registered.some((entry) => entry === resolved || entry.startsWith(resolved + path.sep)))
            throw new Error(`A client still references retained release ${resolved}; detach it before purge`);
          const loadedSource = running?.self?.source_file;
          if (loadedSource && (path.resolve(loadedSource).startsWith(resolved + path.sep) ||
              (existsSync(resolved) && path.resolve(loadedSource).startsWith(realpathSync(resolved) + path.sep))))
            throw new Error("A running daemon still uses a retained release; stop it explicitly before purging code");
        }
        switchCurrent(null);
        for (const release of saved.releases) rmSync(release, { recursive: true, force: true });
        saved.current = null; saved.previous = null; saved.releases = [];
      }
      writeJson(ledgerPath(), saved);
      log(`uninstalled owned integrations; identities, mail, credentials and configuration preserved at ${stateRoot()}`);
      if (!f.purge) log(`release payloads retained at ${dataRoot()} for rollback/reinstall`);
      if (!f.selective) for (const line of remainingAfterUninstall()) log(line);
      return;
    }
    const previous = linkTarget();
    const claudeBefore = f.claude ? snapshotClaude() : null;
    const before = claudeBefore?.settings || {};
    if (claudeBefore) checkClaudeOwnership(saved.clients.claude, claudeBefore);
    const codexBefore = f.codex ? await snapshotCodex() : null;
    if (f.codex && codexBefore) checkCodexOwnership(saved.clients.codex, codexBefore);
    const dest = stabilize(false);
    // Completed staging is owned even if activation later fails. Keep it
    // inspectable and purgeable without claiming it is the active release.
    saved.releases = [...new Set([...saved.releases, dest])];
    writeJson(ledgerPath(), saved);
    const next = structuredClone(saved);
    const integration = (f.claude || f.codex) ? buildIntegration(dest) : saved.integration;
    if (integration && !saved.integrations.some((item) => item.root === integration.root)) {
      saved.integrations.push(integration);
      writeJson(ledgerPath(), saved);
    }
    const bins = path.join(dataRoot(), "bin");
    assertManagedPath(bins);
    const linksBefore = new Map();
    for (const [name, entry] of [["com8", "com8.mjs"], ["communicate", "cli.mjs"]]) {
      const file = path.join(bins, name), target = `../current/src/${entry}`;
      let existing = null;
      try { existing = readlinkSync(file); }
      catch (e) { if (e.code !== "ENOENT") throw new Error(`Refusing to replace user-owned executable: ${file}`); }
      if (existing !== null && existing !== target && existing !== saved.links[file])
        throw new Error(`Refusing to replace user-owned executable: ${file}`);
      linksBefore.set(file, existing);
      next.links[file] = target;
    }
    let serviceChanged = false, codexChanged = false, claudeChanged = false;
    try {
      switchCurrent(dest);
      claudeChanged = !!claudeBefore?.available;
      if (f.claude && claudeInstall(false, integration)) {
        next.clients.claude ||= { previousMarket: claudeBefore.restoreMarket,
          previousMarketLiteral: isDeepStrictEqual(claudeBefore.restoreMarket, before.extraKnownMarketplaces?.[MARKET_ID]) ? undefined : before.extraKnownMarketplaces?.[MARKET_ID],
          previousEnabled: before.enabledPlugins?.[PLUGIN_ID], previousInstalled: claudeBefore.installed,
          previousPluginEnabled: claudeBefore.enabled };
        next.clients.claude.marketRoot = path.join(integration.root, "plugins");
      }
      if (f.codex) {
        codexChanged = !!codexBefore;
        if (await codexInstall(false, integration, codexBefore)) {
          next.clients.codex ||= { original: codexBefore };
          next.clients.codex.marketRoot = integration.root;
          next.clients.codex.ownedSettings = await readCodexSettings();
        }
      }
      mkdirSync(bins, { recursive: true });
      for (const file of linksBefore.keys()) {
        rmSync(file, { force: true });
        symlinkSync(next.links[file], file);
      }
      if (!f.noService && (f.service || saved.service)) {
        next.service = await installService(false, () => switchCurrent(previous), saved.service, { inherit: f.serviceInherit });
        serviceChanged = next.service !== saved.service;
      }
      next.current = dest;
      if (previous && previous !== dest) next.previous = previous;
      next.releases = [...new Set([...next.releases, dest])];
      next.source = readJson(path.join(dest, "vendor/release.json")).source;
      if (integration) {
        next.integration = integration;
        if (!next.integrations.some((item) => item.root === integration.root)) next.integrations.push(integration);
      }
      writeJson(ledgerPath(), next);
      log(`current -> ${dest}`);
      log(`CLI: ${path.join(bins, "com8")} (add ${bins} to PATH if needed)`);
      log(serviceChanged ? "durable daemon service installed" : "daemon service unchanged; use com8 start or com8 setup --service when needed");
      log("done — run the installed com8 doctor; local operation needs no hosted invitation.");
    } catch (error) {
      switchCurrent(previous);
      for (const [file, target] of linksBefore) {
        rmSync(file, { force: true });
        if (target !== null) symlinkSync(target, file);
      }
      if (serviceChanged) restoreService(next.service);
      // Restore just the keys this installer owns, preserving unrelated edits.
      const recoveryFailures = claudeChanged ? restoreClaude(claudeBefore) : [];
      if (claudeChanged) {
        const now = readSettings();
        for (const [parent, key] of [["extraKnownMarketplaces", MARKET_ID], ["enabledPlugins", PLUGIN_ID]]) {
          now[parent] ||= {};
          if (before[parent]?.[key] === undefined) delete now[parent][key];
          else now[parent][key] = before[parent][key];
        }
        writeSettings(now, false, "restore plugin settings after failed activation");
      }
      if (codexChanged) recoveryFailures.push(...await restoreCodex(codexBefore));
      if (!recoveryFailures.length && integration?.created && !saved.integrations.some((item) => item.root === integration.root)) removeIntegration(integration);
      throw new Error(`Activation failed; previous payload restored. ${error.message}${recoveryFailures.length ? " Client registry recovery needs attention: " + recoveryFailures.join("; ") : ""}`);
    }
  });
}

export async function runRollback(argv = []) {
  if (argv.some((a) => a !== "--dry-run")) throw new Error("usage: com8 rollback [--dry-run]");
  const saved = readJson(ledgerPath());
  if (!saved.previous) throw new Error("No retained previous release to roll back to");
  const combined = existsSync(path.join(saved.previous, "vendor/release.json"));
  const priorPackage = readJson(path.join(saved.previous, "package.json"));
  const legacy = !combined && priorPackage.name === "@aadarwal/communicate" && /^0\.[12]\.\d+(?:[+-].*)?$/.test(priorPackage.version || "") &&
    ["src/cli.mjs", "vendor/bin/communicate", "vendor/plugins/communicate/.claude-plugin/plugin.json"].every((name) => existsSync(path.join(saved.previous, name)));
  if (!combined && !legacy) throw new Error("Previous installation is not a recognized retained COM8 or legacy Communicate payload; no changes made");
  // A historical communicate-only package never shipped the durable daemon.
  // Do not replace its service's code pointer with a directory lacking com8.py.
  if (legacy && saved.service) throw new Error("Legacy rollback requires restoring or removing the managed daemon service first; the old package has no durable kernel. No pointer or client changes made. Keep the retained new archive for lifecycle commands.");
  if (argv.includes("--dry-run")) { log(`[dry-run] would restore ${saved.previous}; identities and mail stay at ${stateRoot()}`); return; }
  await withInstallLock(async () => {
    const from = linkTarget();
    const claudeBefore = saved.clients?.claude ? snapshotClaude() : null;
    if (claudeBefore) checkClaudeOwnership(saved.clients.claude, claudeBefore);
    const before = claudeBefore?.settings;
    const codexBefore = saved.clients?.codex ? await snapshotCodex() : null;
    if (saved.clients?.codex) {
      if (!codexBefore) throw new Error("Codex CLI is required to restore its owned registration; no changes made");
      checkCodexOwnership(saved.clients.codex, codexBefore);
    }
    const integration = Object.keys(saved.clients || {}).length ? buildIntegration(saved.previous) : null;
    let codexChanged = false, claudeChanged = false;
    try {
      switchCurrent(saved.previous);
      if (saved.clients?.claude) {
        claudeChanged = true;
        claudeInstall(false, integration);
        saved.clients.claude.marketRoot = path.join(integration.root, "plugins");
      }
      if (saved.clients?.codex) {
        codexChanged = true;
        await codexInstall(false, integration, codexBefore);
        saved.clients.codex.marketRoot = integration.root;
        saved.clients.codex.ownedSettings = await readCodexSettings();
      }
      if (saved.service) saved.service = await installService(false, () => switchCurrent(from), saved.service);
      saved.current = saved.previous; saved.previous = from;
      if (integration) {
        saved.integration = integration; saved.integrations ||= [];
        if (!saved.integrations.some((item) => item.root === integration.root)) saved.integrations.push(integration);
      }
      writeJson(ledgerPath(), saved);
      log(`rolled back to ${saved.current}; runtime state preserved`);
      if (legacy) log(`legacy Communicate ${priorPackage.version} restored; it has no combined com8 entry point. Use the retained new archive's bin/com8 for doctor, update, rollback or uninstall.`);
    } catch (error) {
      switchCurrent(from);
      const failures = claudeChanged ? restoreClaude(claudeBefore) : [];
      if (claudeChanged) {
        const now = readSettings();
        for (const [parent, key] of [["extraKnownMarketplaces", MARKET_ID], ["enabledPlugins", PLUGIN_ID]]) {
          now[parent] ||= {};
          if (before[parent]?.[key] === undefined) delete now[parent][key]; else now[parent][key] = before[parent][key];
        }
        writeSettings(now, false, "restore client settings after failed rollback");
      }
      if (codexChanged) failures.push(...await restoreCodex(codexBefore));
      if (!failures.length && integration?.created) removeIntegration(integration);
      throw new Error(`Rollback activation failed; original payload restored: ${error.message}${failures.length ? "; client registry recovery needs attention: " + failures.join("; ") : ""}`);
    }
  });
}

export async function runDoctor() {
  const rows = [];
  const cur = currentLink();
  const payloadOk = existsSync(cur) && existsSync(path.join(cur, "vendor", "bin", "communicate"));
  const ver = payloadOk && existsSync(path.join(cur, "vendor", "VERSION")) ? readFileSync(path.join(cur, "vendor", "VERSION"), "utf8").trim() : "-";
  rows.push(["invoked package", pkgDir]);
  rows.push(["selected CLI", communicateCli]);
  rows.push(["package version", pkg.version]);
  rows.push(["payload", payloadOk ? `ok (${ver} at ${realpathSync(cur)})` : "MISSING — run setup"]);
  rows.push(["runtime state", stateRoot()]);
  const bus = inspectBus();
  rows.push(["bus connection", busSummary(bus)]);
  if (bus.user) rows.push(["bus account", bus.user]);
  if (bus.device) rows.push(["bus device", bus.device]);
  if (bus.deviceId) rows.push(["bus device ID", bus.deviceId]);
  if (bus.configured) rows.push(["bus worker", bus.workerRecent ? "recent heartbeat (not proof of model activity)" : "no recent heartbeat; registration starts its worker"]);
  const lock = installLockStatus();
  rows.push(["installation lock", lock ? `${lock.path}: pid=${lock.pid ?? "unknown"}, ${lock.status}, started=${lock.started || "unrecorded"}` : "none"]);
  if (lock) rows.push(["lock recovery", lock.recovery]);
  rows.push(["Claude configuration", path.dirname(settingsPath())]);
  rows.push(["Codex configuration", codexHome()]);
  const legacyPointer = path.join(dataRoot(), "repo-path");
  if (existsSync(legacyPointer)) rows.push(["legacy checkout pointer", readFileSync(legacyPointer, "utf8").trim()]);
  for (const command of ["com8", "communicate"]) {
    const resolved = executable(command);
    rows.push([`${command} on PATH`, resolved ? `${resolved} -> ${realpathSync(resolved)}` : "not on PATH"]);
  }
  if (payloadOk) {
    const manifest = readJson(path.join(cur, "vendor/release.json"));
    rows.push(["source commit", `${manifest.source?.commit || "unknown"}${manifest.source?.dirty ? " (modified checkout)" : ""}`]);
    rows.push(["installed MCP", path.join(cur, "src/cli.mjs")]);
    rows.push(["installed daemon", path.join(cur, "vendor/lib/com8.py")]);
  }
  const s = readSettings();
  rows.push(["claude marketplace", s.extraKnownMarketplaces?.[MARKET_ID]?.source?.path || "not registered"]);
  rows.push(["claude plugin enabled", s.enabledPlugins?.[PLUGIN_ID] ? "ok" : "not enabled"]);
  if (hasCodex()) {
    rows.push(["Codex CLI version", clientExec("codex", ["--version"]).stdout.trim()]);
    try {
      const state = codexPlugin();
      rows.push(["codex plugin", !state.installed ? "not installed" : state.enabled ? "installed and enabled" : "installed but disabled"]);
    } catch { rows.push(["codex plugin", "unknown (installed/enabled inspection unsupported or failed)"]); }
    rows.push(["codex marketplace", codexMarketplace()?.root || "not registered/unknown"]);
  } else rows.push(["codex plugin", "codex CLI not found"]);
  if (executable("claude")) {
    const r = spawnSync("claude", ["plugin", "list", "--json"], { encoding: "utf8", timeout: 10000 });
    try { rows.push(["Claude cached version", JSON.parse(r.stdout).find((p) => p.id === PLUGIN_ID)?.version || "not installed"]); } catch {}
  }
  const daemon = await daemonRequest();
  rows.push(["running daemon", daemon?.ok ? `pid=${daemon.self?.pid} kernel=${daemon.self?.version} release=${daemon.self?.release || "unreported"} state=${daemon.self?.state_root}` : "not running (not started by doctor)"]);
  if (daemon?.self?.source_file) rows.push(["daemon loaded source", daemon.self.source_file]);
  if (daemon?.self?.source_commit) rows.push(["daemon loaded commit", daemon.self.source_commit]);
  if (daemon?.self?.user?.handle) rows.push(["user claimed", `@${daemon.self.user.handle}`]);
  if (payloadOk && daemon?.ok) {
    const kernel = path.join(cur, "vendor/lib/com8.py");
    if (existsSync(kernel)) {
      const expected = realpathSync(kernel);
      rows.push(["daemon release parity", daemon.self.source_file === expected ? "current" : "different/unreported source; restart explicitly with com8 setup --service"]);
    } else rows.push(["daemon release parity", "legacy communication payload has no durable kernel"]);
  }
  const orphan = path.join(process.env.XDG_STATE_HOME || path.join(home(), ".local/state"), "com8");
  if (path.resolve(orphan) !== path.join(path.resolve(stateRoot()), "com8") && existsSync(path.join(orphan, "mail"))) {
    rows.push(["orphaned legacy state", `${orphan} contains mail; inspect before migration or removal`]);
    process.exitCode = 1;
  }
  if (daemon?.self?.pid) {
    const r = spawnSync("ps", ["-p", String(daemon.self.pid), "-o", "command="], { encoding: "utf8", timeout: 5000 });
    rows.push(["daemon executable", r.status === 0 ? r.stdout.trim() : "process unavailable"]);
  }
  const ledger = readJson(ledgerPath());
  if (ledger.integration) {
    rows.push(["client projection", `${ledger.integration.pluginVersion} at ${ledger.integration.root}`]);
    rows.push(["MCP runtime paths", JSON.stringify(ledger.integration.environment)]);
  }
  rows.push(["managed service", ledger.service?.path || "none installed by this setup"]);
  for (const bin of ["bash", "python3", "node", "tmux", "ssh", "gh", "tailscale"]) rows.push([bin, executable(bin) || "unavailable (capability requires it)"]);
  rows.push(["client activation", "start fresh client sessions to load refreshed integrations; cached files do not prove a running client loaded them"]);
  for (const [k, v] of rows) console.log(`  ${k.padEnd(24)} ${v}`);
}
