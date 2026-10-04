import { existsSync, readFileSync, realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

export const pkgDir = fileURLToPath(new URL("..", import.meta.url));
export const packageVersion = JSON.parse(readFileSync(path.join(pkgDir, "package.json"), "utf8")).version;
// Checkout MCP must execute checkout code, even when a stale vendor/ or a
// frozen npm installation exists. Published packages only have vendor/.
const checkout = path.resolve(pkgDir, "..", "..");
const checkoutPackage = path.join(checkout, "packages", "communicate");
export const isSourceCheckout = path.resolve(pkgDir) === checkoutPackage &&
  existsSync(path.join(checkout, "lib", "common.sh")) &&
  realpathSync(pkgDir) === realpathSync(checkoutPackage);
export const communicateCli = isSourceCheckout
  ? path.join(checkout, "bin", "communicate")
  : path.join(pkgDir, "vendor", "bin", "communicate");
export const com8Cli = path.join(path.dirname(communicateCli), "com8");
