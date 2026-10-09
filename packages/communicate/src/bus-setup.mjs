// Setup/doctor use the existing bus CLI. Invitation bytes travel only on stdin;
// public reports contain an allowlisted view, never raw CLI output or tokens.
import { spawnSync } from "node:child_process";
import { closeSync, constants, existsSync, fstatSync, openSync, readFileSync, readSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { communicateCli } from "./paths.mjs";

const MAX_INVITE = 8192;
const label = (value) => typeof value === "string" ? value.replace(/[\x00-\x1f\x7f]/g, " ").slice(0, 160) : undefined;
export function busOrigin(value, { local = false } = {}) {
  try {
    if (typeof value !== "string" || /\s/.test(value)) throw new Error();
    const u = new URL(value);
    if (u.port === "0") throw new Error();
    if (u.username || u.password || u.search || u.hash || !["", "/"].includes(u.pathname)) throw new Error();
    if (u.protocol !== "https:" && !(local && u.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(u.hostname))) throw new Error();
    return u.origin;
  } catch { throw new Error("A bus address must be an HTTPS origin without credentials, a path, query or fragment."); }
}

export function invitationOrigin(code) {
  try {
    if (typeof code !== "string" || !/^commbus1\.[A-Za-z0-9_-]+$/.test(code) || Buffer.byteLength(code) > MAX_INVITE) throw new Error();
    const card = JSON.parse(Buffer.from(code.slice(9), "base64url").toString("utf8"));
    if (typeof card.invite !== "string" || !card.invite) throw new Error();
    return busOrigin(card.url);
  } catch { throw new Error("The invitation is invalid. Request a fresh invitation from the intended hub administrator."); }
}

export function readPrivateInvitation(file) {
  if (!path.isAbsolute(file)) throw new Error("The invitation file must use an absolute path.");
  let fd;
  try {
    fd = openSync(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.uid !== process.getuid?.() || (stat.mode & 0o077) || stat.size > MAX_INVITE + 1) throw new Error();
    const bytes = Buffer.alloc(MAX_INVITE + 2);
    let size = 0, count;
    while (size < bytes.length && (count = readSync(fd, bytes, size, bytes.length - size, null))) size += count;
    if (size > MAX_INVITE + 1) throw new Error();
    const code = bytes.subarray(0, size).toString("utf8").trim();
    invitationOrigin(code);
    return code;
  } catch { throw new Error("Cannot read the invitation file. It must be a regular, non-symlink file owned by you, readable only by you, and contain one valid invitation of at most 8192 bytes."); }
  finally { if (fd !== undefined) closeSync(fd); }
}

function callBus(args, { env = process.env, cli = communicateCli, run = spawnSync, input } = {}) {
  let result;
  try { result = run(cli, ["bus", ...args], { env, input, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"], timeout: 20000, maxBuffer: 256 * 1024 }); }
  catch { throw new Error("The bus command could not run; inspect your COM8 installation and retry."); }
  // Even an error from the broker is untrusted and could echo credentials.
  if (result.error || result.status !== 0) throw new Error("The bus command did not complete. Check the selected hub and its invitation, then retry.");
  try {
    const data = JSON.parse(result.stdout);
    if (!data || data.ok !== true) throw new Error();
    return data;
  } catch { throw new Error("The bus returned an invalid result; no connection success is claimed."); }
}

export function busStateDir(options = {}) {
  const env = options.env || process.env;
  const state = env.COMM_STATE || path.join(env.XDG_STATE_HOME || path.join(env.HOME || os.homedir(), ".local/state"), "communicate");
  return path.join(state, "bus");
}

// Stop this device's outbound worker and any owned local broker. Registrations
// and enrollment stay. A machine that never used the bus gets no new state.
export function stopBus(options = {}) {
  if (!existsSync(busStateDir(options))) return null;
  return callBus(["stop"], options).status;
}

export function inspectBus(options = {}) {
  const file = path.join(busStateDir(options), "client.json");
  if (!existsSync(file)) return { configured: false, selection: "not selected", enrollment: "not enrolled" };
  let saved, selected;
  try {
    saved = JSON.parse(readFileSync(file, "utf8"));
    selected = saved.connections?.[saved.default];
    if (!selected) return { configured: false, selection: "local on first registration", enrollment: "no remote hub selected" };
    const hub = busOrigin(selected.url, { local: !!selected.local });
    const summary = { configured: true, hub, local: !!selected.local, selection: selected.local ? "local" : "remote",
      enrollment: selected.local ? "local account" : selected.principal && selected.token ? "saved; not verified" : "not enrolled" };
    if (options.offline) return summary;
    try {
      const result = callBus(["status", "--no-start", "--json"], options);
      if (!result.configured || busOrigin(result.hub, { local: !!selected.local }) !== hub) return { ...summary, reachable: false, enrollment: "selection changed; inspect again" };
      return { ...summary, reachable: result.reachable === true,
        enrollment: result.reachable === true ? (selected.local ? "local account" : "verified") : summary.enrollment,
        ...(result.reachable === true ? { user: label(result.user), device: label(result.device), deviceId: label(result.device_id || result.principal) } : {}),
        workerRecent: result.worker_recent === true };
    } catch { return { ...summary, reachable: false }; }
  } catch { return { configured: false, selection: "configuration could not be read", enrollment: "unknown" }; }
}

export function busSummary(status) {
  if (!status.configured) return `${status.selection}; ${status.enrollment}. No bus service was started.`;
  return `${status.hub} (${status.local ? "local" : "remote"}; ${status.enrollment}${status.reachable === undefined ? "" : status.reachable ? "; reachable" : "; offline or access unavailable"})`;
}

export function applyBusChoice(choice, options = {}) {
  if (choice.mode === "invite") {
    const hub = invitationOrigin(choice.code);
    const result = callBus(["connect", "--invite-stdin"], { ...options, input: choice.code + "\n" });
    if (busOrigin(result.hub) !== hub) throw new Error("The reported hub does not match the selected invitation; inspect bus status before continuing.");
    return { hub, connected: true, user: label(result.user), device: label(result.device), deviceId: label(result.device_id || result.principal) };
  }
  if (choice.mode === "local") {
    callBus(["use", "local", "--no-start"], options);
    return { local: true, connected: false };
  }
  const hub = busOrigin(choice.hub);
  const result = callBus(["use", hub], options);
  if (busOrigin(result.hub) !== hub) throw new Error("The selected hub changed unexpectedly; inspect bus status before continuing.");
  return { hub, selected: true };
}
