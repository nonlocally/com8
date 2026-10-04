#!/usr/bin/env node
// Dependency planning/consent tests. Installers, providers and host setup are
// injected fakes; actual package installation is a separate artifact gate.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { parseOnboardingArgs, planOnboarding, executeOnboarding, shouldGuide,
  runOnboarding, runOnboardingCommand } from "../src/onboarding.mjs";

const tests = [];
const test = (name, run) => tests.push({ name, run });
const clone = (value) => structuredClone(value);
const versions = {
  node: "v22.12.0", npm: "10.9.0", python3: "Python 3.12.8",
  bash: "GNU bash, version 5.2.37", git: "git version 2.47.1",
  tmux: "tmux 3.5a", jq: "jq-1.7.1", brew: "Homebrew 5.0.0",
  claude: "2.1.281 (Claude Code)", codex: "codex-cli 0.156.1",
  ghostty: "Ghostty 1.2.0", tailscale: "1.90.0", curl: "curl 8.12.0",
  sudo: "Sudo version 1.9.16", "apt-get": "apt 2.8.0", fzf: "0.60.0",
  ssh: "OpenSSH_9.9", font: "installed", sh: "shell",
};
function present(id) {
  return { path: `/fixture/bin/${id}`, version: versions[id] || "99.0.0" };
}
function snapshot({ missing = [], platform = "darwin", paths = {} } = {}) {
  const tools = Object.fromEntries(Object.keys(versions).map((id) => [id, present(id)]));
  for (const id of missing) delete tools[id];
  for (const [id, value] of Object.entries(paths)) tools[id] = { ...tools[id], path: value };
  return { platform, home: "/fixture/home with spaces", uid: 501, tools };
}
const selected = (...extra) => parseOnboardingArgs(["--guided", "--install-missing", "--claude", "--no-service", ...extra]);
const makePlan = (state, args = []) => planOnboarding(selected(...args), clone(state));
const mutations = (events) => events.filter((event) => ["run", "script", "setup", "login"].includes(event.kind) ||
  (event.kind === "profile" && event.args[0] !== "preview"));
function fixture(plan, initial, changes = {}) {
  const state = clone(initial), events = [], logs = [];
  const authenticated = new Set();
  let probes = 0;
  const markInstalled = (step) => {
    for (const id of step.requires || []) state.tools[id] = present(id);
  };
  const findStep = (command, args) => {
    const step = plan.steps.find((item) => item.command === command && JSON.stringify(item.args) === JSON.stringify(args));
    assert.ok(step, `unexpected executable request: ${command}`);
    return step;
  };
  const defaults = {
    stdinTTY: true,
    log: (line) => logs.push(String(line)),
    confirm: async (text) => { events.push({ kind: "confirm", text }); return true; },
    probe: async () => { probes += 1; return clone(state); },
    run: async (command, args, meta) => {
      assert.equal(typeof command, "string");
      assert.ok(Array.isArray(args) && args.every((arg) => typeof arg === "string"));
      assert.notEqual(meta?.shell, true, "installer must preserve argv instead of enabling a shell");
      const step = findStep(command, args);
      events.push({ kind: "run", id: step.id, command, args: [...args], meta });
      markInstalled(step);
      return { status: 0, stdout: "", stderr: "" };
    },
    installScript: async (step) => {
      events.push({ kind: "script", id: step.id, step: clone(step) });
      markInstalled(step);
      return { status: 0, stdout: "", stderr: "" };
    },
    setup: async (args) => { events.push({ kind: "setup", args: [...args] }); },
    profile: async (args) => { events.push({ kind: "profile", args: [...args] }); return { ok: true, actions: [] }; },
    doctor: async () => { events.push({ kind: "doctor" }); },
    busStatus: async () => ({ configured: false, selection: "not selected", enrollment: "not enrolled" }),
    prepareBus: async (options) => ({ description: options.bus || "invited hub", apply: async () => { events.push({ kind: "bus", selection: options.bus || "invite" }); } }),
    inspectLogin: async (provider) => { events.push({ kind: "inspect-login", provider }); return authenticated.has(provider); },
    login: async (provider) => { events.push({ kind: "login", provider }); authenticated.add(provider); },
  };
  return { state, events, logs, markInstalled, get probes() { return probes; },
    io: { ...defaults, ...changes }, run: () => executeOnboarding(plan, { ...defaults, ...changes }) };
}

test("guided selection is explicit or a bare interactive setup", () => {
  assert.equal(shouldGuide([], { stdinTTY: true, stdoutTTY: true }), true);
  assert.equal(shouldGuide([], { stdinTTY: false, stdoutTTY: false }), false);
  assert.equal(shouldGuide([], { stdinTTY: true, stdoutTTY: false }), false);
  assert.equal(shouldGuide(["--claude", "--dry-run"], { stdinTTY: true, stdoutTTY: true }), false);
  assert.equal(shouldGuide(["--guided"], { stdinTTY: false, stdoutTTY: false }), true);
  assert.equal(shouldGuide(["--install-missing"], { stdinTTY: false, stdoutTTY: false }), true);
});

test("selected existing providers remain selected without reinstalling them", async () => {
  const state = snapshot(), plan = makePlan(state, ["--codex", "--yes"]);
  assert.deepEqual(plan.blocked, []);
  assert.equal(plan.steps.length, 0);
  const f = fixture(plan, state), result = await f.run();
  assert.equal(result.status, "complete");
  assert.equal(f.events.filter((event) => ["run", "script"].includes(event.kind)).length, 0);
  const setup = f.events.find((event) => event.kind === "setup");
  assert.ok(setup.args.includes("--claude") && setup.args.includes("--codex"));
  assert.ok(setup.args.includes("--no-service"));
});

test("agent-only setup installs missing tmux without activating the terminal profile", async () => {
  for (const provider of ["--claude", "--codex"]) {
    const state = snapshot({ missing: ["tmux"] });
    const options = parseOnboardingArgs(["--install-missing", provider, "--no-service", "--yes"]);
    const plan = planOnboarding(options, state);
    assert.deepEqual(plan.blocked, []);
    assert.deepEqual(plan.profileArgs, []);
    assert.deepEqual(new Set(plan.steps.flatMap((step) => step.requires)), new Set(["tmux"]));
    const f = fixture(plan, state);
    assert.equal((await f.run()).status, "complete");
    assert.ok(f.state.tools.tmux);
    assert.equal(f.events.some((event) => event.kind === "profile"), false);
  }
});

test("explicit client-free setup does not install tmux or a terminal profile", async () => {
  const state = snapshot({ missing: ["tmux"] });
  const options = parseOnboardingArgs(["--install-missing", "--no-clients", "--no-service", "--yes"]);
  const plan = planOnboarding(options, state);
  assert.equal(plan.requirements.some((requirement) => requirement.id === "tmux"), false);
  assert.deepEqual(plan.profileArgs, []);
  const f = fixture(plan, state);
  assert.equal((await f.run()).status, "complete");
  assert.equal(f.events.some((event) => ["profile", "run", "script"].includes(event.kind)), false);
  assert.equal(f.state.tools.tmux, undefined);
});

test("fresh selected software is installed before COM8 setup", async () => {
  const state = snapshot({ missing: ["brew", "python3", "bash", "tmux", "jq", "fzf", "font", "claude", "codex", "ghostty"] });
  const plan = makePlan(state, ["--codex", "--terminal", "--mesh", "--ghostty", "--yes"]);
  assert.deepEqual(plan.blocked, []);
  assert.ok(plan.steps.length > 0);
  const f = fixture(plan, state), result = await f.run();
  assert.equal(result.status, "complete");
  const setupIndex = f.events.findIndex((event) => event.kind === "setup");
  const installs = f.events.map((event, index) => ({ event, index })).filter(({ event }) => ["run", "script"].includes(event.kind));
  assert.ok(installs.length && installs.every(({ index }) => index < setupIndex));
  assert.ok(f.probes >= installs.length + 1, "installer success must be followed by dependency detection");
  assert.ok(f.events.some((event) => event.kind === "doctor"));
});

test("missing selected provider does not reinstall existing provider or unrelated software", async () => {
  const state = snapshot({ missing: ["codex"] }), plan = makePlan(state, ["--codex", "--yes"]);
  assert.deepEqual(plan.blocked, []);
  assert.ok(plan.steps.length);
  assert.ok(plan.steps.every((step) => step.requires.includes("codex")));
  const f = fixture(plan, state);
  await f.run();
  assert.deepEqual(f.state.tools.claude, state.tools.claude);
});

test("declining installer consent performs no setup or installer writes", async () => {
  const state = snapshot({ missing: ["claude"] }), plan = makePlan(state);
  const f = fixture(plan, state, { confirm: async () => false });
  assert.equal((await f.run()).status, "cancelled");
  assert.deepEqual(mutations(f.events), []);
});

test("EOF is cancellation rather than implicit consent", async () => {
  const state = snapshot({ missing: ["claude"] }), plan = makePlan(state);
  const f = fixture(plan, state, { confirm: async () => undefined });
  assert.equal((await f.run()).status, "cancelled");
  assert.deepEqual(mutations(f.events), []);
});

test("noninteractive installation requires explicit yes", async () => {
  const state = snapshot({ missing: ["claude"] }), plan = makePlan(state);
  const f = fixture(plan, state, { stdinTTY: false, confirm: async () => { throw new Error("must not prompt without a terminal"); } });
  await assert.rejects(f.run());
  assert.deepEqual(mutations(f.events), []);
});

test("explicit yes authorizes dependency setup but never provider authentication", async () => {
  const state = snapshot({ missing: ["claude"] }), plan = makePlan(state, ["--yes"]);
  const f = fixture(plan, state, { stdinTTY: false, confirm: async () => { throw new Error("yes must not prompt"); } });
  assert.equal((await f.run()).status, "complete");
  assert.equal(f.events.some((event) => ["inspect-login", "login"].includes(event.kind)), false);
});

test("dry-run with missing tools neither installs nor configures", async () => {
  const state = snapshot({ missing: ["claude", "python3"] }), plan = makePlan(state, ["--dry-run"]);
  const f = fixture(plan, state, { stdinTTY: false, confirm: async () => { throw new Error("dry-run must not prompt"); } });
  assert.equal((await f.run()).status, "dry-run");
  assert.deepEqual(f.events, []);
});

test("dry-run with dependencies prints a plan without activating setup or profiles", async () => {
  const state = snapshot(), plan = makePlan(state, ["--terminal", "--dry-run", "--yes"]);
  const f = fixture(plan, state);
  assert.equal((await f.run()).status, "dry-run");
  assert.deepEqual(f.events, []);
});

test("successful installer exit cannot hide a still-missing dependency", async () => {
  const state = snapshot({ missing: ["claude"] }), plan = makePlan(state, ["--yes"]);
  const f = fixture(plan, state, { run: async () => ({ status: 0 }), installScript: async () => ({ status: 0 }) });
  await assert.rejects(f.run());
  assert.equal(f.events.some((event) => ["setup", "profile", "doctor"].includes(event.kind)), false);
});

test("installer failure stops setup and preserves earlier successful installations for resume", async () => {
  const state = snapshot({ missing: ["claude", "codex"] }), plan = makePlan(state, ["--codex", "--yes"]);
  assert.ok(plan.steps.length >= 2, "fixture needs separate provider installation steps");
  const f = fixture(plan, state);
  let calls = 0;
  const install = async (step) => {
    calls += 1;
    if (calls === 2) throw new Error("fixture installer failure");
    f.markInstalled(step);
    return { status: 0 };
  };
  await assert.rejects(executeOnboarding(plan, { ...f.io,
    run: async (command, args) => install(plan.steps.find((step) => step.command === command && JSON.stringify(step.args) === JSON.stringify(args))),
    installScript: install,
  }), /fixture installer failure/);
  assert.equal(f.events.some((event) => ["setup", "profile", "doctor"].includes(event.kind)), false);
  const completed = plan.steps[0].requires.filter((id) => f.state.tools[id]);
  assert.ok(completed.length);
  const next = makePlan(f.state, ["--codex", "--yes"]);
  assert.ok(next.steps.every((step) => !step.requires.every((id) => completed.includes(id))));
  assert.equal((await fixture(next, f.state).run()).status, "complete");
});

test("a dependency installed since planning is detected and skipped", async () => {
  const state = snapshot({ missing: ["claude"] }), plan = makePlan(state, ["--yes"]);
  assert.ok(plan.steps.length);
  const now = snapshot(), f = fixture(plan, now);
  assert.equal((await f.run()).status, "complete");
  assert.equal(f.events.some((event) => ["run", "script"].includes(event.kind)), false);
});

test("setup failure prevents profile activation and success doctor output", async () => {
  const state = snapshot(), plan = makePlan(state, ["--terminal", "--yes"]);
  const f = fixture(plan, state, { setup: async () => { throw new Error("fixture setup failed"); } });
  await assert.rejects(f.run(), /fixture setup failed/);
  assert.equal(f.events.some((event) => event.kind === "doctor" || (event.kind === "profile" && event.args[0] === "install")), false);
});

test("profile failure is not reported as successful onboarding", async () => {
  const state = snapshot(), plan = makePlan(state, ["--terminal", "--yes"]);
  const f = fixture(plan, state, { profile: async (args) => { if (args[0] === "install") throw new Error("fixture profile failed"); } });
  await assert.rejects(f.run(), /fixture profile failed/);
  assert.equal(f.events.some((event) => event.kind === "setup"), true);
  assert.equal(f.events.some((event) => event.kind === "doctor"), false);
});

for (const missingPython of [false, true]) {
  test(`profile conflicts block core activation with Python ${missingPython ? "newly installed" : "already present"}`, async () => {
    const state = snapshot({ missing: missingPython ? ["python3"] : [] });
    const plan = makePlan(state, ["--terminal", "--yes"]);
    const f = fixture(plan, state, { profile: async (args) => {
      assert.equal(args[0], "preview");
      return { ok: true, actions: [{ action: "conflict", path: "/fixture/.zshrc", reason: "unowned content" }] };
    } });
    await assert.rejects(f.run(), /Profile preview found conflicts.*unowned content/);
    assert.equal(f.events.some((event) => ["setup", "doctor"].includes(event.kind)), false);
    if (missingPython) assert.ok(f.events.some((event) => event.kind === "run" && event.id === "python3"));
    else assert.deepEqual(mutations(f.events), []);
  });
}

test("present but unsupported or unverified provider versions are never replaced automatically", async () => {
  for (const [id, version] of [["claude", "1.0.0"], ["claude", ""], ["codex", "0.150.0"], ["codex", ""]]) {
    const state = snapshot();
    state.tools[id].version = version;
    const plan = makePlan(state, ["--codex", "--yes"]);
    assert.ok(plan.blocked.some((reason) => reason.includes(state.tools[id].path)));
    assert.equal(plan.steps.some((step) => step.requires.includes(id)), false);
    const f = fixture(plan, state);
    await assert.rejects(f.run(), /does not silently replace/);
    assert.deepEqual(mutations(f.events), []);
  }
});

test("Ghostty selection includes terminal prerequisites and profile activation", () => {
  const state = snapshot({ missing: ["tmux", "fzf"] });
  const plan = makePlan(state, ["--ghostty", "--yes"]);
  assert.equal(plan.options.terminal, true);
  assert.ok(plan.profileArgs.includes("--terminal"));
  assert.deepEqual(new Set(plan.steps.flatMap((step) => step.requires)), new Set(["tmux", "fzf"]));
});

test("planner reuses a Ghostty executable discovered inside its app bundle", () => {
  const state = snapshot({ paths: { ghostty: "/Applications/Ghostty.app/Contents/MacOS/ghostty" } });
  delete state.tools.ghostty.version;
  const plan = makePlan(state, ["--terminal", "--ghostty", "--yes"]);
  assert.deepEqual(plan.blocked, []);
  assert.equal(plan.steps.some((step) => step.requires.includes("ghostty")), false);
});

test("unsupported Linux Ghostty installation is reported before any installation", async () => {
  const state = snapshot({ platform: "linux", missing: ["ghostty"] });
  delete state.tools.brew;
  const plan = makePlan(state, ["--terminal", "--ghostty", "--yes"]);
  assert.ok(plan.blocked.length);
  const f = fixture(plan, state);
  await assert.rejects(f.run());
  assert.deepEqual(mutations(f.events), []);
});

test("explicit selected-provider login remains separate from package installation", async () => {
  const state = snapshot(), plan = makePlan(state, ["--yes", "--login-claude"]);
  const f = fixture(plan, state);
  assert.equal((await f.run()).status, "complete");
  assert.deepEqual(f.events.filter((event) => event.kind === "login").map((event) => event.provider), ["claude"]);
  assert.equal(f.events.some((event) => ["run", "script"].includes(event.kind)), false);
});

test("already authenticated selected provider is not logged in again", async () => {
  const state = snapshot(), plan = makePlan(state, ["--yes", "--login-claude"]);
  const f = fixture(plan, state, { inspectLogin: async () => true });
  assert.equal((await f.run()).status, "complete");
  assert.equal(f.events.some((event) => event.kind === "login"), false);
});

test("unsuccessful explicit provider login does not claim completed onboarding", async () => {
  const state = snapshot(), plan = makePlan(state, ["--yes", "--login-claude"]);
  const f = fixture(plan, state, { inspectLogin: async () => false });
  await assert.rejects(f.run(), /login did not complete/);
  assert.equal(f.events.some((event) => event.kind === "setup"), true);
  assert.equal(f.events.filter((event) => event.kind === "login").length, 1);
});

test("login flags cannot authorize authentication in a noninteractive invocation", async () => {
  const state = snapshot(), plan = makePlan(state, ["--yes", "--login-claude"]);
  const f = fixture(plan, state, { stdinTTY: false });
  await assert.rejects(f.run());
  assert.deepEqual(mutations(f.events), []);
});

test("an unselected provider cannot be logged in implicitly", async () => {
  const state = snapshot();
  await assert.rejects(async () => {
    const plan = makePlan(state, ["--yes", "--login-codex"]);
    const f = fixture(plan, state);
    try { await f.run(); } finally { assert.equal(f.events.some((event) => event.kind === "login"), false); }
  });
});

test("incompatible Node and unsupported platforms block all mutation", async () => {
  for (const state of [snapshot(), snapshot({ platform: "win32" })]) {
    if (state.platform === "darwin") state.tools.node.version = "v18.20.0";
    const plan = makePlan(state, ["--yes"]);
    assert.ok(plan.blocked.length);
    const f = fixture(plan, state);
    await assert.rejects(f.run());
    assert.deepEqual(mutations(f.events), []);
  }
});

test("terminal selection replaces unsupported Bash and Python requirements only", () => {
  const state = snapshot();
  state.tools.bash.version = "GNU bash, version 3.2.57";
  state.tools.python3.version = "Python 3.8.20";
  const plan = makePlan(state, ["--terminal", "--yes"]);
  assert.deepEqual(plan.blocked, []);
  assert.deepEqual(new Set(plan.steps.flatMap((step) => step.requires)), new Set(["bash", "python3"]));
});

test("unsupported flags and contradictory selections are rejected before planning", () => {
  for (const args of [["--no-clients", "--claude"], ["--service", "--no-service"],
    ["--service-inherit=ANTHROPIC_API_KEY"], ["--install-missing;touch", "/tmp/forbidden"]]) {
    assert.throws(() => parseOnboardingArgs(args));
  }
});

test("guided answers choose only the requested client and keep authentication separate", async () => {
  const state = snapshot(), plan = makePlan(state);
  const answers = [true, false, false, false, false, false, false, false, false, true];
  const f = fixture(plan, state, { confirm: async () => {
    assert.ok(answers.length, "unexpected additional prompt");
    return answers.shift();
  } });
  assert.equal((await runOnboarding(["--guided"], { ...f.io, stdoutTTY: true })).status, "complete");
  assert.equal(answers.length, 0);
  const setup = f.events.find((event) => event.kind === "setup");
  assert.ok(setup.args.includes("--claude"));
  assert.equal(setup.args.includes("--codex"), false);
  assert.equal(f.events.some((event) => ["run", "script", "login", "inspect-login"].includes(event.kind)), false);
});

test("injected EOF during the wizard never installs or activates defaults", async () => {
  const state = snapshot(), plan = makePlan(state);
  let prompts = 0;
  const f = fixture(plan, state, { confirm: async () => { prompts += 1; assert.ok(prompts < 12); return undefined; } });
  assert.equal((await runOnboarding(["--guided"], { ...f.io, stdoutTTY: true })).status, "cancelled");
  assert.deepEqual(mutations(f.events), []);
});

test("Linux wizard does not offer an unsupported automatic Ghostty install", async () => {
  const state = snapshot({ platform: "linux", missing: ["ghostty", "font"] }), plan = makePlan(state);
  const f = fixture(plan, state, { confirm: async (text) => {
    assert.doesNotMatch(text, /Ghostty|Nerd Font/);
    return /Set up Claude|Apply this setup plan/.test(text);
  } });
  const result = await runOnboarding(["--guided"], { ...f.io, stdoutTTY: true });
  assert.equal(result.status, "complete");
  assert.equal(result.plan.options.ghostty, false);
  assert.equal(f.events.some((event) => ["run", "script", "login"].includes(event.kind)), false);
});

for (const report of ["conflict", "invalid", "missing"]) {
  test(`default profile report ${report} cannot activate core setup`, async () => {
    const state = snapshot(), plan = makePlan(state, ["--terminal", "--yes"]), f = fixture(plan, state);
    const { profile: unusedProfileFake, ...io } = f.io;
    let captured = false;
    const beforePath = process.env.PATH;
    await assert.rejects(runOnboarding(["--install-missing", "--claude", "--terminal", "--no-service", "--yes"], {
      ...io, stdinTTY: false, stdoutTTY: false,
      run: async (command, args, meta) => {
        assert.equal(command, state.tools.python3.path);
        assert.equal(meta.capture, true);
        assert.ok(args.includes("preview"));
        captured = true;
        return { stdout: report === "missing" ? "" : report === "invalid" ? "not JSON" :
          JSON.stringify({ ok: true, actions: [{ action: "conflict", path: "/fixture/.zshrc", reason: "unowned" }] }) };
      },
    }), /Profile preview found conflicts|invalid report|no report/);
    assert.equal(captured, true);
    assert.equal(process.env.PATH, beforePath, "temporary selected-tool PATH must be restored after failure");
    assert.equal(f.events.some((event) => ["setup", "doctor"].includes(event.kind)), false);
  });
}

test("bus setup flags preserve explicit destinations and reject mixed enrollment", () => {
  assert.equal(shouldGuide(["--bus=local"]), true);
  assert.equal(shouldGuide(["--bus-invite-file=/private/invite"]), true);
  assert.equal(parseOnboardingArgs(["--bus=https://hub.example/"]).bus, "https://hub.example");
  for (const args of [["--bus=http://remote.example"], ["--bus=local", "--bus-invite-file=/private/invite"],
    ["--bus-invite-file=relative"], ["--bus=local", "--bus=local"]]) assert.throws(() => parseOnboardingArgs(args));
});

test("ordinary and repeated setup leave the selected bus untouched", async () => {
  const state = snapshot(), plan = makePlan(state, ["--yes"]);
  const f = fixture(plan, state, { prepareBus: async () => { throw new Error("must preserve the existing selection"); } });
  await f.run(); await f.run();
  assert.equal(f.events.some((event) => event.kind === "bus"), false);
});

test("dry-run neither reads an invitation nor inspects or connects a hub", async () => {
  const state = snapshot(), plan = makePlan(state, ["--bus-invite-file=/private/missing", "--dry-run"]);
  const f = fixture(plan, state, { prepareBus: async () => { throw new Error("dry run read invitation"); }, busStatus: async () => { throw new Error("dry run inspected network"); } });
  assert.equal((await f.run()).status, "dry-run");
  assert.deepEqual(f.events, []);
});

test("explicit bus selection runs after installation and before doctor", async () => {
  const state = snapshot(), plan = makePlan(state, ["--bus=local", "--yes"]), f = fixture(plan, state);
  assert.equal((await f.run()).status, "complete");
  assert.deepEqual(f.events.filter((event) => ["setup", "bus", "doctor"].includes(event.kind)).map((event) => event.kind), ["setup", "bus", "doctor"]);
});

test("declining invitation origin confirmation leaves installation untouched", async () => {
  const state = snapshot(), plan = makePlan(state, ["--bus-invite-file=/private/invite"]);
  const f = fixture(plan, state, { prepareBus: async () => null });
  assert.equal((await f.run()).status, "cancelled");
  assert.deepEqual(mutations(f.events), []);
});

test("optional bus failure reports retained core installation and no registration", async () => {
  const state = snapshot(), plan = makePlan(state, ["--bus=https://offline.example", "--yes"]);
  const f = fixture(plan, state, { prepareBus: async () => ({ description: "offline hub", apply: async () => { throw new Error("sensitive-broker-output"); } }) });
  await assert.rejects(f.run(), (error) => /core setup completed.*optional bus setup did not complete.*No agent registration/.test(error.message) && !error.message.includes("sensitive-broker-output"));
  assert.deepEqual(f.events.filter((event) => ["setup", "doctor"].includes(event.kind)).map((event) => event.kind), ["setup", "doctor"]);
});

for (const consent of [true, false]) {
  test(`hidden invitation confirms its origin and ${consent ? "joins without returning the secret" : "cancels before any installation"}`, async () => {
    const state = snapshot(), plan = makePlan(state), f = fixture(plan, state);
    const code = "commbus1." + Buffer.from(JSON.stringify({ url: "https://shared.example", invite: "private-hidden-value" })).toString("base64url");
    const { prepareBus: _fixturePrepare, ...io } = f.io;
    const questions = [], joins = [];
    const result = await runOnboarding(["--guided", "--claude", "--no-service"], { ...io, stdoutTTY: true,
      confirm: async (text) => {
        questions.push(text);
        if (text.startsWith("Enroll this installation")) return consent;
        return text.startsWith("Choose a local or shared bus") || text.startsWith("Apply this setup plan");
      },
      ask: async () => "invitation", secret: async () => code,
      applyBus: async (choice) => { joins.push(choice); f.events.push({ kind: "bus" }); },
    });
    assert.ok(questions.some((text) => text.includes("https://shared.example")));
    assert.equal(JSON.stringify(result).includes(code), false);
    assert.equal(JSON.stringify(result).includes("private-hidden-value"), false);
    assert.equal(JSON.stringify(f.logs).includes(code), false);
    if (consent) {
      assert.equal(result.status, "complete");
      assert.deepEqual(joins, [{ mode: "invite", code }]);
      assert.deepEqual(f.events.filter((event) => ["setup", "bus", "doctor"].includes(event.kind)).map((event) => event.kind), ["setup", "bus", "doctor"]);
    } else {
      assert.equal(result.status, "cancelled");
      assert.deepEqual(joins, []);
      assert.deepEqual(mutations(f.events), []);
    }
  });
}

test("automated private invitation never appears in logs or the returned setup plan", async () => {
  const state = snapshot(), plan = makePlan(state), f = fixture(plan, state);
  const temp = mkdtempSync(path.join(os.tmpdir(), "com8-invite-consent-"));
  try {
    const code = "commbus1." + Buffer.from(JSON.stringify({ url: "https://shared.example", invite: "private-file-value" })).toString("base64url");
    const file = path.join(temp, "invite"); writeFileSync(file, code, { mode: 0o600 });
    const { prepareBus: _fixturePrepare, ...io } = f.io;
    let joined = false;
    const result = await runOnboarding(["--install-missing", "--claude", "--no-service", "--yes", `--bus-invite-file=${file}`], {
      ...io, stdinTTY: false, stdoutTTY: false,
      confirm: async () => { throw new Error("noninteractive invitation must not prompt"); },
      applyBus: async (choice) => { assert.equal(choice.code, code); joined = true; },
    });
    assert.equal(result.status, "complete"); assert.equal(joined, true);
    for (const text of [JSON.stringify(result), JSON.stringify(f.logs)]) {
      assert.equal(text.includes(code), false); assert.equal(text.includes("private-file-value"), false);
    }
    assert.equal(readFileSync(file, "utf8"), code);
  } finally { rmSync(temp, { recursive: true, force: true }); }
});

for (const mode of ["local", "existing", "invite"]) {
  for (const fails of [false, true]) {
    test(`${mode} bus setup uses the selected Python and restores PATH after ${fails ? "failure" : "success"}`, async () => {
      const temp = mkdtempSync(path.join(os.tmpdir(), "com8-bus-python-"));
      const originalPath = process.env.PATH;
      try {
        const earlier = path.join(temp, "earlier"), selected = path.join(temp, "selected");
        for (const [directory, label] of [[earlier, "earlier-python"], [selected, "selected-python"]]) {
          mkdirSync(directory);
          writeFileSync(path.join(directory, "python3"), `#!/bin/sh\nprintf '%s\\n' '${label}'\n`, { mode: 0o755 });
        }
        process.env.PATH = earlier + path.delimiter + (originalPath || "");
        const priorPath = process.env.PATH;
        const state = snapshot({ paths: { python3: path.join(selected, "python3") } });
        const f = fixture(makePlan(state), state);
        const { prepareBus: _fixturePrepare, ...io } = f.io;
        const invite = path.join(temp, "invitation");
        const code = "commbus1." + Buffer.from(JSON.stringify({ url: "https://shared.example", invite: "fixture-only" })).toString("base64url");
        writeFileSync(invite, code, { mode: 0o600 });
        const flag = mode === "invite" ? `--bus-invite-file=${invite}` : `--bus=${mode === "local" ? "local" : "https://shared.example"}`;
        let resolved, appliedMode;
        const launch = () => runOnboarding(["--install-missing", "--claude", "--no-service", "--yes", flag], {
          ...io, stdinTTY: false, stdoutTTY: false,
          applyBus: async (choice, { env }) => {
            appliedMode = choice.mode;
            // Exercise child executable lookup using harmless fixture programs,
            // as the bus launcher resolves its bare python3 through this PATH.
            resolved = (await runOnboardingCommand("python3", [], { env, capture: true })).stdout.trim();
            if (fails) throw new Error("fixture bus failure");
          },
        });
        if (fails) await assert.rejects(launch(), /optional bus setup did not complete/);
        else assert.equal((await launch()).status, "complete");
        assert.equal(appliedMode, mode);
        assert.equal(resolved, "selected-python", "bus setup used an earlier, unselected interpreter");
        assert.equal(process.env.PATH, priorPath, "bus setup leaked its selected PATH into the caller");
      } finally {
        if (originalPath === undefined) delete process.env.PATH;
        else process.env.PATH = originalPath;
        rmSync(temp, { recursive: true, force: true });
      }
    });
  }
}

test("default command runner preserves literal executable paths and arguments", async () => {
  const temp = mkdtempSync(path.join(os.tmpdir(), "com8-onboard-argv-"));
  try {
    const executable = path.join(temp, "installer ; literal $HOME `echo ignored`.mjs"), log = path.join(temp, "argv.json");
    const sentinel = path.join(temp, "must-not-exist");
    writeFileSync(executable, `#!${process.execPath}\nimport {writeFileSync} from 'node:fs';\nwriteFileSync(${JSON.stringify(log)},JSON.stringify(process.argv.slice(2)));\n`, { mode: 0o755 });
    const args = ["space containing value", "$HOME", "`touch ignored`", `$(touch ${sentinel})`, "--", "a'b\"c", "line one\nline two"];
    await runOnboardingCommand(executable, args, { env: { PATH: path.dirname(process.execPath), HOME: temp } });
    assert.deepEqual(JSON.parse(readFileSync(log, "utf8")), args);
    assert.equal(existsSync(sentinel), false);
  } finally { rmSync(temp, { recursive: true, force: true }); }
});

test("default command runner reports failed executables without a success result", async () => {
  await assert.rejects(async () => runOnboardingCommand(process.execPath, ["-e", "process.exit(19)"], { env: {} }), /19/);
});

test("default command runner captures profile output without mixing stderr", async () => {
  const result = await runOnboardingCommand(process.execPath,
    ["-e", "process.stdout.write('{\"ok\":true}'); process.stderr.write('fixture diagnostic');"], { env: {}, capture: true });
  assert.deepEqual(result, { stdout: '{"ok":true}', stderr: "fixture diagnostic" });
});

for (const outcome of ["success", "empty", "download-error"]) {
  test(`official installer download ${outcome} uses owned files and always cleans them`, async () => {
    const state = snapshot({ platform: "linux", missing: ["claude"] });
    const plan = makePlan(state, ["--yes"]), f = fixture(plan, state);
    const { installScript: unusedScriptFake, ...io } = f.io;
    let downloadedFile, scriptInvocations = 0;
    const run = async (command, args) => {
      if (command === "curl") {
        assert.ok(args.includes("--fail"));
        assert.equal(args[args.indexOf("--proto") + 1], "=https");
        assert.equal(args[args.indexOf("--proto-redir") + 1], "=https");
        assert.ok(args.at(-1).startsWith("https://"));
        downloadedFile = args[args.indexOf("--output") + 1];
        assert.ok(path.isAbsolute(downloadedFile));
        if (outcome === "download-error") throw new Error("fixture download failed");
        writeFileSync(downloadedFile, outcome === "empty" ? "" : "# fixture installer, never executed\n");
      } else {
        scriptInvocations += 1;
        assert.equal(command, "bash");
        assert.equal(args[0], downloadedFile);
        assert.ok(existsSync(downloadedFile));
        f.state.tools.claude = present("claude");
      }
    };
    const launch = () => runOnboarding(["--install-missing", "--claude", "--no-service", "--yes"],
      { ...io, stdinTTY: false, stdoutTTY: false, run });
    if (outcome === "success") {
      assert.equal((await launch()).status, "complete");
      assert.equal(scriptInvocations, 1);
    } else {
      await assert.rejects(launch());
      assert.equal(scriptInvocations, 0);
      assert.equal(f.events.some((event) => event.kind === "setup"), false);
    }
    assert.ok(downloadedFile, "fixture did not exercise the installer download path");
    assert.equal(existsSync(path.dirname(downloadedFile)), false);
  });
}

let failed = 0;
for (const { name, run } of tests) {
  try { await run(); console.log(`PASS: ${name}`); }
  catch (error) { failed += 1; console.error(`FAIL: ${name}\n${error.stack || error}`); }
}
console.log(`${tests.length - failed}/${tests.length} onboarding dependency/consent fixture tests passed`);
if (failed) process.exitCode = 1;
