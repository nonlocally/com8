// kernel.ts — the daemon client. The MCP server holds ZERO fabric logic; every
// tool call becomes one JSON line to the com8 daemon's control socket. This
// file resolves the socket path (identical to com8.py's state_root, so a
// repo-managed and npm-managed com8 are two faces of ONE daemon), speaks the
// one-object-per-connection protocol, and autostarts the vendored daemon.
import net from "node:net";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const pkgRoot = path.dirname(here); // dist/.. == package root at runtime

export function stateRoot(): string {
  const base =
    process.env.COMM_STATE ||
    path.join(
      process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state"),
      "communicate",
    );
  return path.join(base, "com8");
}

export function controlSocket(): string {
  return process.env.COM8_SOCK || path.join(stateRoot(), "com8.sock");
}

export function resolvePython(): string {
  const cands = [process.env.COM8_PYTHON, "python3", "/usr/bin/python3"].filter(
    Boolean,
  ) as string[];
  for (const c of cands) {
    const r = spawnSync(c, ["-c", "import sys; assert sys.version_info>=(3,9)"], {
      stdio: "ignore",
    });
    if (r.status === 0) return c;
  }
  throw new Error("python3 (>=3.9) not found; set COM8_PYTHON");
}

// Prefer a stabilized install (survives npx cache pruning), then the vendored
// copy in this package (works for a bare `npx @aadarwal/com8` before setup).
export function daemonFile(): string {
  if (process.env.COM8_DAEMON_DIR)
    return path.join(process.env.COM8_DAEMON_DIR, "com8.py");
  const combined = path.join(process.env.COMMUNICATE_DATA ||
    path.join(process.env.HOME || os.homedir(), ".local/share/communicate"), "current/vendor/lib/com8.py");
  if (fs.existsSync(combined)) return combined;
  const installed = path.join(
    process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share"),
    "com8",
    "daemon",
    "current",
    "com8.py",
  );
  if (fs.existsSync(installed)) return installed;
  return path.join(pkgRoot, "vendor", "com8.py");
}

function connect(sock: string, timeoutMs: number): Promise<net.Socket> {
  return new Promise((resolve, reject) => {
    const s = net.createConnection({ path: sock });
    const to = setTimeout(() => {
      s.destroy();
      reject(new Error("connect timeout"));
    }, timeoutMs);
    s.once("connect", () => {
      clearTimeout(to);
      resolve(s);
    });
    s.once("error", (e) => {
      clearTimeout(to);
      reject(e);
    });
  });
}

async function waitForSocket(tries: number): Promise<boolean> {
  for (let i = 0; i < tries; i++) {
    try {
      const s = await connect(controlSocket(), 500);
      s.destroy();
      return true;
    } catch {
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  return false;
}

async function autostart(): Promise<void> {
  if (process.env.COM8_AUTOSTART === "0") throw new Error("daemon not running");
  const py = resolvePython();
  const daemon = daemonFile();
  fs.mkdirSync(stateRoot(), { recursive: true });
  // Cross-process spawn lock: two MCP clients (a claude and a codex both
  // configured with com8) can autostart at the same instant. O_EXCL means
  // exactly one wins the right to spawn; the loser waits for the socket the
  // winner's daemon will bind. The daemon's own singleton is the backstop.
  const lock = path.join(stateRoot(), "autostart.lock");
  let holder = false;
  try {
    const fd = fs.openSync(lock, "wx");
    fs.writeSync(fd, String(process.pid));
    fs.closeSync(fd);
    holder = true;
  } catch {
    // someone else is spawning — just wait for the socket
    if (await waitForSocket(60)) return;
    // stale lock (spawner died)? clear it and fall through to spawn ourselves
    try { fs.unlinkSync(lock); } catch {}
  }
  try {
    const log = fs.openSync(path.join(stateRoot(), "daemon.log"), "a");
    const child = spawn(py, [daemon, "daemon"], {
      detached: true,
      stdio: ["ignore", log, log],
    });
    child.unref();
    if (await waitForSocket(50)) return;
    throw new Error("daemon failed to start (see " + stateRoot() + "/daemon.log)");
  } finally {
    if (holder) { try { fs.unlinkSync(lock); } catch {} }
  }
}

export interface CallOpts {
  timeoutMs?: number;
  noAutostart?: boolean;
}

function controlAuth(): string | null {
  // The control credential (required once a fleet link exists). A local file
  // read — the 0700 state dir means only this uid can see it.
  try {
    const tok = fs.readFileSync(path.join(stateRoot(), "control.token"), "utf8").trim();
    return tok || null;
  } catch {
    return null;
  }
}

// One request, one reply. The reply is a single JSON object terminated by \n.
export async function call(req: Record<string, unknown>, opts: CallOpts = {}): Promise<any> {
  const timeoutMs = opts.timeoutMs ?? 15000;
  if (!("auth" in req)) {
    const tok = controlAuth();
    if (tok) req = { ...req, auth: tok };
  }
  let s: net.Socket;
  try {
    s = await connect(controlSocket(), 3000);
  } catch (e) {
    if (opts.noAutostart) throw e;
    await autostart();
    s = await connect(controlSocket(), 3000);
  }
  return new Promise((resolve, reject) => {
    let buf = "";
    const to = setTimeout(() => {
      s.destroy();
      reject(new Error("call timeout"));
    }, timeoutMs);
    s.on("data", (d) => {
      buf += d.toString("utf8");
      const nl = buf.indexOf("\n");
      if (nl >= 0) {
        clearTimeout(to);
        s.end();
        try {
          resolve(JSON.parse(buf.slice(0, nl)));
        } catch (e) {
          reject(e);
        }
      }
    });
    s.on("error", (e) => {
      clearTimeout(to);
      reject(e);
    });
    s.on("end", () => {
      if (!buf.includes("\n")) {
        clearTimeout(to);
        reject(new Error("daemon closed without a reply"));
      }
    });
    s.write(JSON.stringify(req) + "\n");
    s.end();
  });
}
