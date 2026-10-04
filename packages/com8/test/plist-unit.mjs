// Unit test for the launchd plist rendering. Pins the two installer bugs that
// shipped a daemon nobody talks to: (P1) COMM_STATE must strip exactly ONE
// path level off stateRoot() — stripping two orphaned the daemon under
// ~/.local/state/com8 while every client looked in ~/.local/state/communicate/
// com8; (P2) PATH must be baked into the unit, or launchd's default PATH hides
// homebrew tmux/claude/codex from seat/spawn/fan/consult.
import path from "node:path";
import os from "node:os";

let pass = 0, fail = 0;
const ok = (m) => { pass++; console.log("ok   " + m); };
const bad = (m) => { fail++; console.log("FAIL " + m); };

// Deterministic state root for the assertions.
delete process.env.COMM_STATE;
process.env.XDG_STATE_HOME = "/tmp/plist-unit-xdg";

const { renderPlist, LAUNCHD_PATH } = await import("../dist/plist.js");

// P1: default COMM_STATE = dirname(stateRoot()) = $XDG_STATE_HOME/communicate.
{
  const p = renderPlist("/usr/bin/python3", "/tmp/daemon/com8.py", "testhost");
  const m = p.match(/<key>COMM_STATE<\/key><string>([^<]*)<\/string>/);
  if (m && m[1] === "/tmp/plist-unit-xdg/communicate")
    ok("COMM_STATE strips exactly one level (got " + m[1] + ")");
  else bad("COMM_STATE value wrong: " + (m ? m[1] : "missing") +
           " (want /tmp/plist-unit-xdg/communicate)");
  if (m && path.join(m[1], "com8") === "/tmp/plist-unit-xdg/communicate/com8")
    ok("daemon started from this plist binds the same state root clients use");
  else bad("plist state root diverges from client state root");

  // P2: PATH baked, mirroring the repo installer.
  const pm = p.match(/<key>PATH<\/key><string>([^<]*)<\/string>/);
  if (pm && pm[1] === LAUNCHD_PATH && /\/opt\/homebrew\/bin/.test(pm[1]))
    ok("PATH baked into the unit (" + pm[1] + ")");
  else bad("PATH missing or wrong: " + (pm ? pm[1] : "absent"));

  if (/<key>COM8_SELF<\/key><string>testhost<\/string>/.test(p))
    ok("COM8_SELF carried");
  else bad("COM8_SELF missing");
}

// An explicit COMM_STATE env wins verbatim.
{
  process.env.COMM_STATE = "/tmp/custom-root";
  const p = renderPlist("/usr/bin/python3", "/tmp/daemon/com8.py", "testhost");
  const m = p.match(/<key>COMM_STATE<\/key><string>([^<]*)<\/string>/);
  if (m && m[1] === "/tmp/custom-root") ok("explicit COMM_STATE env wins verbatim");
  else bad("explicit COMM_STATE not honored: " + (m ? m[1] : "missing"));
  delete process.env.COMM_STATE;
}

console.log(`\npass=${pass} fail=${fail}`);
process.exit(fail ? 1 : 0);
