// Package managers deliver an available release; explicit setup activates it.
// Runtime commands follow that activation, including after rollback. Source
// checkouts deliberately keep executing their own code for development.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { pkgDir, isSourceCheckout } from "./paths.mjs";

export function selectedRuntime() {
  if (isSourceCheckout) return path.resolve(pkgDir);
  const data = process.env.COMMUNICATE_DATA || path.join(process.env.HOME || os.homedir(), ".local/share/communicate");
  const ledger = path.join(data, "install.json");
  if (!fs.existsSync(ledger)) return path.resolve(pkgDir);
  const installed = JSON.parse(fs.readFileSync(ledger, "utf8"));
  if (!installed.current) return path.resolve(pkgDir);
  const active = fs.realpathSync(path.join(data, "current"));
  if (active !== fs.realpathSync(installed.current) || !fs.existsSync(path.join(active, "package.json")))
    throw new Error("Installed runtime pointer disagrees with its ownership record; inspect com8 doctor before activation");
  return active;
}

export function dispatchActive(entry, args) {
  const active = selectedRuntime();
  if (active === fs.realpathSync(pkgDir)) return false;
  const target = path.join(active, "src", entry);
  if (!fs.existsSync(target))
    throw new Error("The active legacy release has no COM8 CLI; use communicate for its native tools or run this archive's com8 setup to upgrade");
  const result = spawnSync(process.execPath, [target, ...args], { stdio: "inherit" });
  if (result.error) throw result.error;
  process.exitCode = result.status ?? 1;
  return true;
}
