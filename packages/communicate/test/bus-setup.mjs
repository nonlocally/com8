#!/usr/bin/env node
// Private temporary configuration plus injected CLI results; no broker/provider.
import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { applyBusChoice, busOrigin, busSummary, inspectBus, invitationOrigin, readPrivateInvitation } from "../src/bus-setup.mjs";

const temp = mkdtempSync(path.join(os.tmpdir(), "com8-bus-setup-"));
const state = path.join(temp, "state"), config = path.join(state, "bus/client.json");
const env = { HOME: temp, COMM_STATE: state };
const secret = "fixture-secret-never-rendered";
const code = "commbus1." + Buffer.from(JSON.stringify({ url: "https://hub.example", invite: secret })).toString("base64url");
let count = 0;
function check(name, fn) { fn(); count += 1; console.log(`PASS: ${name}`); }
const writeConfig = (value) => { mkdirSync(path.dirname(config), { recursive: true }); writeFileSync(config, JSON.stringify(value), { mode: 0o600 }); };
const saved = { default: "https://hub.example", connections: { "https://hub.example": { url: "https://hub.example", principal: "device-1", token: secret } } };
try {
  check("fresh doctor inspection creates no state and invokes no command", () => {
    assert.equal(inspectBus({ env, run: () => { throw new Error("unexpected command"); } }).configured, false);
    assert.equal(existsSync(state), false);
  });
  check("unsafe origin input is rejected without reproducing its content", () => {
    for (const origin of ["http://remote.example", "https://user:SECRET@hub.example", "https://hub.example/?SECRET", "https://hub.example/#SECRET", "https://hub.example/path", "https://hub.example:0", " https://hub.example"]) {
      assert.throws(() => busOrigin(origin), (error) => !error.message.includes("SECRET"));
    }
  });
  check("invitation validates destination without retaining its credential in summary", () => {
    assert.equal(invitationOrigin(code), "https://hub.example");
    for (const value of ["", code + "!", "commbus1." + "a".repeat(9000), "commbus1." + Buffer.from(JSON.stringify({ url: "http://hub.example", invite: secret })).toString("base64url")]) {
      assert.throws(() => invitationOrigin(value), (error) => !error.message.includes(secret) && (!value || !error.message.includes(value)));
    }
  });
  const inviteFile = path.join(temp, "private invite");
  writeFileSync(inviteFile, code + "\n", { mode: 0o600 });
  check("private invitation file preserves bytes and remains user-owned", () => {
    assert.equal(readPrivateInvitation(inviteFile), code);
    assert.equal(readFileSync(inviteFile, "utf8"), code + "\n");
  });
  check("invitation file refuses public permissions, symlink, directory and oversized bytes", () => {
    chmodSync(inviteFile, 0o644);
    assert.throws(() => readPrivateInvitation(inviteFile));
    chmodSync(inviteFile, 0o600);
    const link = path.join(temp, "symlink"); symlinkSync(inviteFile, link);
    assert.throws(() => readPrivateInvitation(link));
    assert.throws(() => readPrivateInvitation(temp));
    assert.throws(() => readPrivateInvitation("relative"));
    const large = path.join(temp, "large"); writeFileSync(large, code + " ".repeat(9000), { mode: 0o600 });
    assert.throws(() => readPrivateInvitation(large));
  });
  check("invitation is only on child stdin, never argv/env/public result", () => {
    const calls = [];
    const result = applyBusChoice({ mode: "invite", code }, { env, cli: "/fixture/communicate", run: (command, args, options) => {
      calls.push({ command, args, options });
      return { status: 0, stdout: JSON.stringify({ ok: true, hub: "https://hub.example", user: "person", device: "laptop", principal: "device-1", token: secret, unknown: code }) };
    } });
    assert.deepEqual(calls[0].args, ["bus", "connect", "--invite-stdin"]);
    assert.equal(calls[0].options.input, code + "\n");
    assert.equal(JSON.stringify(calls[0].args).includes(code), false);
    assert.equal(JSON.stringify(calls[0].options.env).includes(code), false);
    assert.equal(JSON.stringify(result).includes(secret), false);
    assert.equal(JSON.stringify(result).includes(code), false);
  });
  check("CLI failure and malformed output do not echo invitation or broker errors", () => {
    for (const response of [{ status: 1, stdout: code, stderr: secret }, { status: 0, stdout: secret }, { error: new Error(code), status: null }]) {
      assert.throws(() => applyBusChoice({ mode: "invite", code }, { env, run: () => response }),
        (error) => !error.message.includes(secret) && !error.message.includes(code));
    }
  });
  check("local selection uses explicit nonstarting CLI mode", () => {
    let args;
    const result = applyBusChoice({ mode: "local" }, { env, run: (_command, argv) => { args = argv; return { status: 0, stdout: '{"ok":true,"hub":"local","started":false}' }; } });
    assert.deepEqual(args, ["bus", "use", "local", "--no-start"]);
    assert.deepEqual(result, { local: true, connected: false });
  });
  check("existing remote selection never invents an invitation or enrollment", () => {
    let args;
    applyBusChoice({ mode: "existing", hub: "https://hub.example" }, { env, run: (_command, argv) => { args = argv; return { status: 0, stdout: '{"ok":true,"hub":"https://hub.example"}' }; } });
    assert.deepEqual(args, ["bus", "use", "https://hub.example"]);
  });
  check("doctor reports a reachable enrollment through nonstarting status only", () => {
    writeConfig(saved);
    const before = readFileSync(config), calls = [];
    const status = inspectBus({ env, run: (_command, args) => {
      calls.push(args);
      return { status: 0, stdout: JSON.stringify({ ok: true, configured: true, hub: "https://hub.example", reachable: true,
        user: "person", device: "laptop", device_id: "device-1", worker_recent: true, token: secret, worker: { error: secret } }) };
    } });
    assert.deepEqual(calls, [["bus", "status", "--no-start", "--json"]]);
    assert.equal(status.enrollment, "verified");
    assert.equal(status.deviceId, "device-1");
    assert.equal(JSON.stringify(status).includes(secret), false);
    assert.equal(busSummary(status).includes(secret), false);
    assert.deepEqual(readFileSync(config), before);
  });
  check("offline differs from unenrolled and raw network errors are discarded", () => {
    const status = inspectBus({ env, run: () => ({ status: 0, stdout: JSON.stringify({ ok: true, configured: true, hub: "https://hub.example", reachable: false, error: secret }) }) });
    assert.equal(status.configured, true);
    assert.equal(status.enrollment, "saved; not verified");
    assert.match(busSummary(status), /offline or access unavailable/);
    assert.equal(JSON.stringify(status).includes(secret), false);
  });
  check("offline metadata inspection makes no network request", () => {
    const status = inspectBus({ env, offline: true, run: () => { throw new Error("network"); } });
    assert.equal(status.hub, "https://hub.example");
    assert.equal(status.reachable, undefined);
  });
  check("broken saved config is unknown without leaking malformed contents", () => {
    writeFileSync(config, secret);
    const status = inspectBus({ env, run: () => { throw new Error("must not call"); } });
    assert.equal(status.enrollment, "unknown");
    assert.equal(JSON.stringify(status).includes(secret), false);
  });
  check("no selected remote preserves saved connection bytes without connecting", () => {
    writeConfig({ ...saved, default: null });
    const before = readFileSync(config);
    const status = inspectBus({ env, run: () => { throw new Error("must not call"); } });
    assert.equal(status.configured, false);
    assert.deepEqual(readFileSync(config), before);
  });
} finally { rmSync(temp, { recursive: true, force: true }); }
console.log(`${count} bus setup privacy and nonstarting inspection checks passed`);
