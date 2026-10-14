#!/usr/bin/env node
// @aadarwal/communicate — node verbs (setup/doctor/serve/version) + bash passthrough.
import { spawnSync } from "node:child_process";
import { readFileSync, existsSync } from "node:fs";
import path from "node:path";
import { pkgDir, communicateCli } from "./paths.mjs";
import { dispatchActive } from "./runtime-selection.mjs";

const [cmd, ...rest] = process.argv.slice(2);

const version = () => {
  const v = JSON.parse(readFileSync(path.join(pkgDir, "package.json"), "utf8")).version;
  const vp = path.join(pkgDir, "vendor", "VERSION");
  const stamp = existsSync(vp) ? readFileSync(vp, "utf8").trim() : "unvendored";
  console.log(`@aadarwal/communicate ${v} (payload ${stamp})`);
};

try {
if (!["setup", "update", "uninstall", "rollback", "doctor"].includes(cmd) &&
    dispatchActive("cli.mjs", process.argv.slice(2))) {
  // The selected release supplied the result and exit status.
} else switch (cmd) {
  case undefined:
  case "help":
  case "--help":
    version();
    console.log("verbs: setup/update [--claude|--codex|--no-clients] [--service|--no-service] [--dry-run] | rollback | uninstall [--purge] | doctor | serve | version");
    console.log("       setup --guided | setup --install-missing [--claude|--codex|--no-clients] [--terminal] [--mesh] [--ghostty] [--yes] [--dry-run]");
    console.log("       setup [--claude|--codex|--no-clients] [--bus=local|HTTPS_ORIGIN | --bus-invite-file=ABS_PRIVATE_FILE] [--yes] [--dry-run]");
    console.log("       any communicate CLI verb (bus ..., agents, route, send, codex ..., claude ..., wake ...)");
    break;
  case "version":
  case "--version":
    version();
    break;
  case "setup": {
    const onboarding = await import("./onboarding.mjs");
    if (onboarding.shouldGuide(rest)) {
      const result = await onboarding.runOnboarding(rest);
      if (result.status === "cancelled") { console.log("Setup cancelled."); process.exitCode = 130; }
    }
    else await (await import("./setup.mjs")).runSetup(rest, { narratePlan: rest.length === 0 });
    break;
  }
  case "update":
    await (await import("./setup.mjs")).runSetup(rest);
    break;
  case "uninstall":
    await (await import("./setup.mjs")).runSetup(["--uninstall", ...rest]);
    break;
  case "rollback":
    await (await import("./setup.mjs")).runRollback(rest);
    break;
  case "doctor":
    await (await import("./setup.mjs")).runDoctor();
    break;
  case "serve":
    await (await import("./serve.mjs")).runServe();
    break;
  default: {
    if (!existsSync(communicateCli)) {
      console.error("communicate: payload missing — reinstall @aadarwal/communicate (or run: npm run vendor)");
      process.exit(127);
    }
    const r = spawnSync(communicateCli, [cmd, ...rest], { stdio: "inherit" });
    process.exit(r.status ?? 1);
  }
}
} catch (error) {
  console.error(`communicate: ${error.message}`);
  process.exitCode = 1;
}
