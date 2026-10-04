// Optional setup for a model that powers the coding client itself. Credentials
// travel only over the child process's stdin, never argv or the setup report.
import { spawnSync } from "node:child_process";
import { com8Cli } from "./paths.mjs";

export function modelOrigin(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error("Use the model API's HTTPS address ending in /v1."); }
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash ||
      !/^\/v1\/?$/.test(url.pathname))
    throw new Error("Use the model API's HTTPS address ending in /v1, without credentials, query or fragment.");
  return { base: `${url.origin}/v1`, origin: url.origin };
}

export function addModelConnection(args, key, { run = spawnSync, env = process.env } = {}) {
  if (typeof key !== "string" || !/^[\x21-\x7e]+$/.test(key) || Buffer.byteLength(key) > 8191)
    throw new Error("The model key must be a nonempty ASCII token without whitespace, at most 8191 bytes.");
  const result = run(com8Cli, ["model", "add", ...args, "--key-stdin", "--json"], {
    env, input: key + "\n", encoding: "utf8", stdio: ["pipe", "pipe", "pipe"],
    timeout: 30000, maxBuffer: 1024 * 1024,
  });
  // Never include child output/exception text in an error: a third-party
  // wrapper or damaged installation could echo its input.
  if (result.error || result.status !== 0) throw new Error("Model connection was not saved. Check its name and private configuration with com8 model list; existing connections are never overwritten.");
  let report;
  try { report = JSON.parse(result.stdout); } catch { throw new Error("Model setup returned an invalid report; no success is claimed."); }
  if (report.ok !== true) throw new Error("Model connection was not saved; no success is claimed.");
  return { ok: true };
}

export async function prepareModelChoice({ ask, secret, add = addModelConnection }) {
  const name = await ask("Connection name (for example glm; empty cancels):");
  if (!name) return null;
  if (!/^[a-z0-9][a-z0-9_-]{0,47}$/.test(name)) throw new Error("Use at most 48 lowercase letters, digits, dashes or underscores for the connection name.");
  const endpoint = await ask("Canonical model API address (HTTPS, ending in /v1; empty cancels):");
  if (!endpoint) return null;
  const { base, origin } = modelOrigin(endpoint);
  const model = await ask("Model ID from that API's catalog (for example glm; empty cancels):");
  if (!model) return null;
  if (!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(model)) throw new Error("Use the model ID exactly as shown in the API catalog.");
  const keyPrompt = base === "https://mit.nonlocally.org/v1"
    ? "Create a model API key in API & clients at https://mit.nonlocally.org/workspaces/developer.\n" +
      "Use the model key beginning with nlm_; the website account API key beginning with sk- does not grant model access.\n" +
      "Paste the model API key (hidden; empty cancels):"
    : "Paste a scoped model API key (hidden; empty cancels):";
  let key = await secret(keyPrompt);
  if (!key) return null;
  if (!/^[\x21-\x7e]+$/.test(key) || Buffer.byteLength(key) > 8191) throw new Error("The model API key must be an ASCII token without whitespace, at most 8191 bytes.");
  return {
    description: `${name}: ${model} at ${base}; explicitly selected launches only`,
    apply: async () => {
      try { return await add([name, "--base-url", base, "--model", model, "--anthropic-base-url", origin], key); }
      finally { key = undefined; }
    },
  };
}
