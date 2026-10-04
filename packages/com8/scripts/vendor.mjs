// Compatibility package: use the same reviewed artifact and installer.
import { cpSync, mkdirSync, rmSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const pkg = dirname(dirname(fileURLToPath(import.meta.url)));
const repo = dirname(dirname(pkg));
const combined = join(repo, "packages/communicate");
execFileSync(process.execPath, [join(combined, "scripts/vendor.mjs")], { stdio: "inherit" });
const vendor = join(pkg, "vendor");
rmSync(vendor, { recursive: true, force: true });
mkdirSync(vendor, { recursive: true });
for (const file of ["src", "vendor", "package.json", "LICENSE", "README.md"])
  cpSync(join(combined, file), join(vendor, "communicate", file), { recursive: true });
const manifest = JSON.parse(readFileSync(join(combined, "vendor/release.json"), "utf8"));
for (const file of Object.keys(manifest.files))
  if (file.startsWith("lib/") && file.endsWith(".py") && !file.startsWith("lib/bus"))
    cpSync(join(combined, "vendor", file), join(vendor, file.slice(4)));
cpSync(join(combined, "vendor/VERSION"), join(vendor, "VERSION"));
cpSync(join(repo, "LICENSE"), join(pkg, "LICENSE"));
