#!/usr/bin/env node
// COM8's package entry point. Runtime behavior stays in the shared CLI kernel.
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { pkgDir, com8Cli, packageVersion } from "./paths.mjs";
import { dispatchActive } from "./runtime-selection.mjs";

const [command = "help", ...args] = process.argv.slice(2);
try {
  if (!["setup", "update", "uninstall", "rollback", "doctor"].includes(command) &&
      dispatchActive("com8.mjs", [command, ...args])) {
    // The selected release supplied the result and exit status.
  } else {
  switch (command) {
    case "setup": {
      const onboarding = await import("./onboarding.mjs");
      if (onboarding.shouldGuide(args)) {
        const result = await onboarding.runOnboarding(args);
        if (result.status === "cancelled") { console.log("Setup cancelled."); process.exitCode = 130; }
      }
      else await (await import("./setup.mjs")).runSetup(args);
      break;
    }
    case "update":
      await (await import("./setup.mjs")).runSetup(args);
      break;
    case "uninstall":
      await (await import("./setup.mjs")).runSetup(["--uninstall", ...args]);
      break;
    case "rollback":
      await (await import("./setup.mjs")).runRollback(args);
      break;
    case "doctor":
      await (await import("./setup.mjs")).runDoctor();
      break;
    case "serve":
      await (await import("./serve.mjs")).runServe();
      break;
    case "version": case "--version": {
      const manifest = path.join(pkgDir, "vendor", "release.json");
      const source = existsSync(manifest) ? JSON.parse(readFileSync(manifest, "utf8")).source : null;
      console.log(`COM8 ${packageVersion}${source ? ` (${source.commit}${source.dirty ? ", modified checkout" : ""})` : " (source checkout)"}`);
      break;
    }
    default: {
      if (!existsSync(com8Cli)) throw new Error("COM8 payload missing; rebuild or reinstall the release artifact");
      const result = spawnSync(com8Cli, [command, ...args], { stdio: "inherit" });
      if (result.error) throw result.error;
      process.exitCode = result.status ?? 1;
    }
  }
  }
} catch (error) {
  console.error(`com8: ${error.message}`);
  process.exitCode = 1;
}
