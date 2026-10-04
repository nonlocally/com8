// Use Codex's config API for its own TOML syntax and narrow, versioned writes.
// Never parse/rewrite the complete file or expose its contents in diagnostics.
import { spawn } from "node:child_process";
import { existsSync, realpathSync, mkdirSync } from "node:fs";
import { createInterface } from "node:readline";
import { isDeepStrictEqual } from "node:util";
import path from "node:path";
import { home, assertManagedPath } from "./lifecycle.mjs";

const ID = "communicate@communicate";
const settingsPath = () => path.resolve(process.env.CODEX_HOME || path.join(home(), ".codex"), "config.toml");
const canonical = (file) => existsSync(file) ? realpathSync(file) : path.join(realpathSync(path.dirname(file)), path.basename(file));
const samePath = (a, b) => canonical(a) === canonical(b);

async function withConfig(operation) {
  assertManagedPath(settingsPath());
  mkdirSync(path.dirname(settingsPath()), { recursive: true, mode: 0o700 });
  const child = spawn("codex", ["app-server"], { cwd: home(), stdio: ["pipe", "pipe", "ignore"] });
  const pending = new Map();
  let sequence = 0, failure;
  const fail = (error) => { failure = error; for (const p of pending.values()) p.reject(error); pending.clear(); };
  child.on("error", () => fail(new Error("Codex config API is unavailable; no safe plugin settings snapshot is possible")));
  child.on("exit", () => fail(new Error("Codex config API exited before completing the settings operation")));
  child.stdin.on("error", () => fail(new Error("Codex config API input closed")));
  const lines = createInterface({ input: child.stdout });
  lines.on("line", (line) => {
    let message;
    try { message = JSON.parse(line); } catch {
      fail(new Error("Codex config API returned malformed JSON; client state must be preserved")); return;
    }
    const p = pending.get(message.id);
    if (!p) return;
    pending.delete(message.id);
    // Error payloads may contain user configuration. Keep them private.
    if (message.error) p.reject(new Error(`Codex ${p.method} refused the settings operation; COM8 requires layered config/read and versioned config/value/write (verified with Codex 0.156.1). Previous configuration must be preserved.`));
    else p.resolve(message.result);
  });
  const timeout = setTimeout(() => { fail(new Error("Codex config API timed out; client state needs inspection before retry")); child.kill("SIGKILL"); }, 30000);
  const request = (method, params) => new Promise((resolve, reject) => {
    if (failure) { reject(failure); return; }
    const id = ++sequence; pending.set(id, { resolve, reject, method });
    child.stdin.write(JSON.stringify({ id, method, params }) + "\n");
  });
  try {
    await request("initialize", { clientInfo: { name: "com8-installer", version: "0.4.0" } });
    return await operation(request);
  } finally {
    clearTimeout(timeout); lines.close(); child.stdin.end(); child.kill("SIGTERM");
    if (child.exitCode === null && child.signalCode === null) await new Promise((resolve) => {
      const kill = setTimeout(() => child.kill("SIGKILL"), 2000);
      child.once("close", () => { clearTimeout(kill); resolve(); });
    });
  }
}

function userSettings(result) {
  if (!Array.isArray(result?.layers)) throw new Error("Codex config/read must support includeLayers; no client changes made");
  const layers = result.layers.filter((layer) => layer.name?.type === "user" && !layer.name.profile && samePath(layer.name.file, settingsPath()));
  if (layers.length !== 1 || !layers[0].version) throw new Error("Codex user config layer/version could not be verified; no client changes made");
  if (result.layers.some((layer) => layer !== layers[0] && layer.config?.plugins?.[ID] !== undefined))
    throw new Error("Codex COM8 plugin has settings in another profile or managed layer; reconcile those settings before setup/uninstall");
  return { settings: layers[0].config?.plugins?.[ID] ?? null, version: layers[0].version };
}

export async function readCodexSettings({ preflight = false } = {}) {
  return withConfig(async (request) => {
    const state = userSettings(await request("config/read", { includeLayers: true }));
    // A semantic no-op verifies that this client can restore the exact table
    // before any marketplace/plugin removal. No model or tool is launched.
    if (preflight) await write(request, state.settings, state);
    return state.settings;
  });
}

async function write(request, settings, state) {
  const response = await request("config/value/write", { keyPath: `plugins."${ID}"`, value: settings,
    mergeStrategy: "replace", filePath: canonical(settingsPath()), expectedVersion: state.version });
  if (response?.status !== "ok") throw new Error("Codex plugin settings are overridden; exact restoration is unavailable");
  const after = userSettings(await request("config/read", { includeLayers: true }));
  if (!isDeepStrictEqual(after.settings, settings)) throw new Error("Codex plugin settings did not round-trip exactly; inspect configuration before retry");
}

export async function writeCodexSettings(settings, expected) {
  return withConfig(async (request) => {
    const state = userSettings(await request("config/read", { includeLayers: true }));
    if (!isDeepStrictEqual(state.settings, expected))
      throw new Error("Codex plugin settings changed during installation; refusing to overwrite the user's edits");
    await write(request, settings, state);
  });
}
