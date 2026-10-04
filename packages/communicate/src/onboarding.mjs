// Setup-only dependency planning. Package managers own packages; COM8 owns only
// the integrations/configuration recorded by its existing setup/profile ledgers.
import { spawnSync } from "node:child_process";
import { accessSync, constants, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import { Writable } from "node:stream";
import { pkgDir } from "./paths.mjs";
import { applyBusChoice, busOrigin, busSummary, inspectBus, invitationOrigin, readPrivateInvitation } from "./bus-setup.mjs";
import { addModelConnection, prepareModelChoice } from "./model-setup.mjs";

export const INSTALLERS = Object.freeze({
  brew: "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh",
  claude: "https://claude.ai/install.sh",
  codex: "https://chatgpt.com/codex/install.sh",
});
const NEW_FLAGS = ["--guided", "--install-missing", "--terminal", "--mesh", "--ghostty", "--login-claude", "--login-codex", "--model"];
const SERVICE_ENV = ["PATH", "COM8_SOCK_DIR", "COM8_SESSIONS_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "COM8_TMUX_SOCKET"];
export function shouldGuide(argv, { stdinTTY = process.stdin.isTTY, stdoutTTY = process.stdout.isTTY } = {}) {
  return (argv.length === 0 && !!stdinTTY && !!stdoutTTY) || argv.some((a) => NEW_FLAGS.includes(a) || a.startsWith("--bus=") || a.startsWith("--bus-invite-file="));
}

export function parseOnboardingArgs(argv) {
  const o = { guided: false, installMissing: false, claude: false, codex: false, noClients: false,
    terminal: false, mesh: false, ghostty: false, service: false, noService: false,
    yes: false, dryRun: false, loginClaude: false, loginCodex: false, model: false, bus: null, busInviteFile: null, setupArgs: [] };
  const flags = { "--guided": "guided", "--install-missing": "installMissing", "--claude": "claude",
    "--codex": "codex", "--no-clients": "noClients", "--terminal": "terminal", "--mesh": "mesh",
    "--ghostty": "ghostty", "--service": "service", "--no-service": "noService", "--yes": "yes",
    "-y": "yes", "--dry-run": "dryRun", "--login-claude": "loginClaude", "--login-codex": "loginCodex", "--model": "model" };
  for (const a of argv) {
    if (flags[a]) o[flags[a]] = true;
    else if (a.startsWith("--bus=")) {
      if (o.bus !== null) throw new Error("Choose --bus only once");
      o.bus = a.slice(6) === "local" ? "local" : busOrigin(a.slice(6));
    }
    else if (a.startsWith("--bus-invite-file=")) {
      if (o.busInviteFile !== null) throw new Error("Choose an invitation file only once");
      o.busInviteFile = a.slice(18);
      if (!path.isAbsolute(o.busInviteFile)) throw new Error("The invitation file must use an absolute path");
    }
    else if (a.startsWith("--service-inherit=") && SERVICE_ENV.includes(a.slice(18))) o.setupArgs.push(a);
    else throw new Error(`unknown guided setup flag: ${a}`);
  }
  if (o.noClients && (o.claude || o.codex)) throw new Error("--no-clients cannot be combined with --claude or --codex");
  if (o.service && o.noService) throw new Error("--service and --no-service cannot be combined");
  if (o.bus !== null && o.busInviteFile !== null) throw new Error("Choose --bus or --bus-invite-file, not both");
  if (o.ghostty) o.terminal = true;
  if ((o.loginClaude && !o.claude) || (o.loginCodex && !o.codex))
    throw new Error("Select --claude or --codex explicitly before requesting its login");
  return o;
}

const major = (version) => Number(String(version || "").match(/\d+/)?.[0] || 0);
const pythonSupported = (version) => {
  const [a, b] = String(version || "").match(/\d+(?:\.\d+)*/)?.[0].split(".").map(Number) || [];
  return a > 3 || (a === 3 && b >= 9);
};
export function requirementMet(requirement, snapshot) {
  const tool = snapshot.tools?.[requirement.id];
  if (!tool?.path) return false;
  if (requirement.id === "python3") return pythonSupported(tool.version);
  if (requirement.id === "claude") return major(tool.version) >= 2;
  if (requirement.id === "codex") {
    const [a, b] = String(tool.version || "").match(/\d+(?:\.\d+)*/)?.[0].split(".").map(Number) || [];
    return a > 0 || (a === 0 && b >= (requirement.modelConnection ? 156 : 151));
  }
  return !requirement.minimum || major(tool.version) >= requirement.minimum;
}

export function planOnboarding(options, snapshot) {
  const o = { ...options, terminal: options.terminal || options.ghostty, setupArgs: [...(options.setupArgs || [])] };
  const requirements = [{ id: "node", minimum: 20, label: "Node.js 20+" },
    { id: "python3", label: "Python 3.9+" }, { id: "bash", minimum: o.terminal || o.mesh ? 4 : 3, label: o.terminal || o.mesh ? "Bash 4+" : "Bash" }];
  for (const [id, selected] of [["claude", o.claude], ["codex", o.codex], ["tmux", o.terminal || o.claude || o.codex],
    ["fzf", o.terminal || o.mesh], ["jq", o.mesh], ["ssh", o.mesh], ["ghostty", o.ghostty],
    ["font", o.ghostty]]) if (selected) requirements.push({ id,
      ...(id === "codex" && o.model ? { modelConnection: true } : {}),
      label: id === "font" ? "JetBrainsMono Nerd Font" : id === "tmux" ? "tmux (agent seats)" : id === "codex" && o.model ? "Codex 0.156+ (model connections)" : id });
  const missing = requirements.filter((r) => !requirementMet(r, snapshot));
  const steps = [], blocked = [];
  const add = (step) => { if (!steps.some((s) => s.id === step.id)) steps.push(step); };
  const tools = snapshot.tools || {};
  const command = (id, cmd, args, requires, reason) => add({ id, kind: "command", command: cmd, args, requires, reason });
  const script = (id, shell, args, requires, reason) => add({ id, kind: "script", command: shell, args, url: INSTALLERS[id], requires, reason });
  if (!["darwin", "linux"].includes(snapshot.platform)) blocked.push("Guided dependency installation supports macOS and Linux only.");
  for (const r of missing) {
    if (["claude", "codex"].includes(r.id) && tools[r.id]?.path) {
      blocked.push(`${r.label} exists at ${tools[r.id].path} but its version could not be verified or is unsupported. Update/repair it with its own installer, then rerun; COM8 does not silently replace existing clients.`);
      continue;
    }
    if (!o.installMissing) { blocked.push(`${r.label} is missing or unsupported; install it or select --install-missing.`); continue; }
    if (r.id === "node") { blocked.push("Run this release with Node.js 20+ first; the Node-based setup cannot bootstrap its own runtime."); continue; }
    if (snapshot.platform === "darwin") {
      if (snapshot.uid === 0) { blocked.push("Run Homebrew and COM8 setup as your normal account, not root."); continue; }
      if (!tools.brew?.path) {
        if (!tools.curl?.path) { blocked.push("curl is required to download the official Homebrew installer."); continue; }
        script("brew", "/bin/bash", [], ["brew"], "Install Homebrew using its official installer; it may request administrator permission.");
      }
      const casks = { claude: "claude-code", codex: "codex", ghostty: "ghostty", font: "font-jetbrains-mono-nerd-font" };
      const formulae = { python3: "python@3.14", bash: "bash", tmux: "tmux", fzf: "fzf", jq: "jq", ssh: "openssh" };
      if (casks[r.id]) command(r.id, "brew", ["install", "--cask", casks[r.id]], [r.id], `Install missing ${r.label}.`);
      else if (formulae[r.id]) command(r.id, "brew", ["install", formulae[r.id]], [r.id], `Install missing ${r.label}.`);
      else blocked.push(`No macOS installation recipe for ${r.label}.`);
    } else if (snapshot.platform === "linux") {
      const apt = tools["apt-get"]?.path;
      const prefix = snapshot.uid === 0 ? [] : ["sudo"];
      const installApt = (id, pkg, requires) => {
        if (!apt || (prefix.length && !tools.sudo?.path)) { blocked.push(`Install ${pkg} with your distribution's package manager, then rerun setup; automatic system packages require apt-get and sudo (unless already root).`); return; }
        command(id, prefix[0] || apt, [...(prefix.length ? [apt] : []), "install", "-y", "--", pkg], requires, `Install missing ${pkg} using the existing apt package index; administrator permission may be requested.`);
      };
      if (r.id === "claude" || r.id === "codex") {
        if (snapshot.uid === 0) { blocked.push(`Install ${r.id} as your normal user; COM8 will not install provider credentials or clients into root's account.`); continue; }
        if (!tools.curl?.path) installApt("curl", "curl", ["curl"]);
        script(r.id, r.id === "claude" ? "bash" : "sh", r.id === "claude" ? ["stable"] : [], [r.id], `Install ${r.id} for this user using its official native installer.`);
      } else if (r.id === "ghostty" || r.id === "font") {
        blocked.push(`Install ${r.label} with a supported distribution-specific method first. COM8 does not add third-party Linux repositories or guess a Ghostty/font package.`);
      } else {
        const packages = { python3: "python3", bash: "bash", tmux: "tmux", fzf: "fzf", jq: "jq", ssh: "openssh-client" };
        if (packages[r.id]) installApt(r.id, packages[r.id], [r.id]);
      }
    }
  }
  const setupArgs = [...o.setupArgs, ...(o.claude ? ["--claude"] : []), ...(o.codex ? ["--codex"] : []),
    ...(!o.claude && !o.codex ? ["--no-clients"] : []), o.service ? "--service" : "--no-service"];
  const profileArgs = [...(o.terminal ? ["--terminal"] : []), ...(o.mesh ? ["--mesh"] : [])];
  return { options: o, snapshot, requirements, steps, blocked, setupArgs, profileArgs };
}

function environment() {
  const home = process.env.HOME || os.homedir();
  return { ...process.env, PATH: [...new Set([process.env.PATH || "", path.join(home, ".local/bin"),
    "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin",
    "/Applications/Ghostty.app/Contents/MacOS", path.join(home, "Applications/Ghostty.app/Contents/MacOS")])].join(path.delimiter),
    HOMEBREW_NO_AUTO_UPDATE: "1", HOMEBREW_NO_INSTALL_CLEANUP: "1", HOMEBREW_NO_AUTOREMOVE: "1" };
}
function find(command, env = environment()) {
  for (const dir of env.PATH.split(path.delimiter)) {
    if (!dir) continue;
    const file = path.join(dir, command);
    try { accessSync(file, constants.X_OK); return file; } catch {}
  }
  return null;
}
function version(command) {
  const r = spawnSync(command, ["--version"], { env: environment(), encoding: "utf8", timeout: 10000 });
  return r.status === 0 ? (r.stdout || r.stderr || "").match(/\d+(?:\.\d+)+/)?.[0] || "" : "";
}
export function probeOnboarding() {
  const env = environment(), tools = {};
  for (const id of ["node", "python3", "bash", "sh", "claude", "codex", "tmux", "fzf", "jq", "ssh", "ghostty", "brew", "curl", "apt-get", "sudo"])
    if (find(id, env)) tools[id] = { path: find(id, env) };
  tools.node = { path: process.execPath, version: process.versions.node };
  for (const id of ["python3", "bash"]) {
    const candidates = [...new Set([tools[id]?.path, `/opt/homebrew/bin/${id}`, `/usr/local/bin/${id}`].filter(Boolean))];
    for (const file of candidates) {
      if (!existsSync(file)) continue;
      const v = version(file);
      if (!tools[id]?.version || (id === "bash" ? major(v) > major(tools[id].version) : pythonSupported(v) && !pythonSupported(tools[id].version)))
        tools[id] = { path: file, version: v };
    }
  }
  for (const id of ["claude", "codex"]) if (tools[id]) tools[id].version = version(tools[id].path);
  const home = process.env.HOME || os.homedir();
  for (const dir of process.platform === "darwin" ? [path.join(home, "Library/Fonts"), "/Library/Fonts"] : [path.join(home, ".local/share/fonts"), "/usr/share/fonts/truetype/jetbrains-mono"])
    try { if (readdirSync(dir).some((name) => /JetBrainsMono.*Nerd.*\.(ttf|otf)$/i.test(name))) tools.font = { path: dir }; } catch {}
  if (!tools.font && process.platform === "linux" && find("fc-match")) {
    const r = spawnSync(find("fc-match"), ["-f", "%{family}", "JetBrainsMono Nerd Font"], { env, encoding: "utf8", timeout: 10000 });
    if (r.status === 0 && /JetBrainsMono Nerd Font/.test(r.stdout)) tools.font = { path: "fontconfig:JetBrainsMono Nerd Font" };
  }
  return { platform: process.platform, tools, home, uid: process.getuid?.() };
}

export function runOnboardingCommand(command, args, { env = environment(), capture = false } = {}) {
  const executable = command.includes("/") ? command : find(command, env) || command;
  const r = spawnSync(executable, args, { env, stdio: capture ? ["ignore", "pipe", "pipe"] : "inherit", encoding: capture ? "utf8" : undefined });
  if (r.error || r.status !== 0) throw new Error(`${command} failed (${r.error?.message || r.signal || r.status}).${capture && r.stderr ? " " + r.stderr.trim().slice(-2000) : ""} Previously installed packages remain installed; fix the reported problem and rerun setup.`);
  return capture ? { stdout: r.stdout, stderr: r.stderr } : undefined;
}
async function installScript(step, options, runner = runOnboardingCommand) {
  if (INSTALLERS[step.id] !== step.url || !step.url.startsWith("https://")) throw new Error("Unrecognized installer URL");
  const temp = mkdtempSync(path.join(os.tmpdir(), "com8-install-"));
  const file = path.join(temp, "installer");
  try {
    await runner("curl", ["-q", "--fail", "--show-error", "--location", "--proto", "=https", "--proto-redir", "=https",
      "--connect-timeout", "30", "--max-time", "300", "--output", file, step.url]);
    if (!readFileSync(file).length) throw new Error("Installer download was empty");
    await runner(step.command, [file, ...step.args], { env: { ...environment(), ...(step.id === "brew" && options.yes ? { NONINTERACTIVE: "1" } : {}) } });
  } finally { rmSync(temp, { recursive: true, force: true }); }
}

function promptSession() {
  let closed = false, active = null;
  const ask = async (question, secret = false) => {
    if (closed || process.stdin.readableEnded) { closed = true; return undefined; }
    // readline owns raw mode; its output is muted for secrets, including pasted
    // characters and cursor redraws. The prompt itself contains no invitation.
    const output = secret ? new Writable({ write(_chunk, _encoding, done) { done(); } }) : process.stdout;
    const rl = createInterface({ input: process.stdin, output, terminal: secret || !!process.stdin.isTTY });
    if (secret) process.stdout.write(question + " ");
    active = rl;
    return new Promise((resolve) => {
      let settled = false;
      const done = (value) => {
        if (settled) return;
        settled = true; rl.off("close", cancel); rl.off("SIGINT", interrupt);
        rl.close(); active = null;
        if (secret) { process.stdout.write("\n"); output.end(); }
        resolve(value);
      };
      const cancel = () => { closed = true; done(undefined); };
      const interrupt = () => { closed = true; done(undefined); };
      rl.once("close", cancel); rl.once("SIGINT", interrupt);
      rl.question(secret ? "" : question + " ", (answer) => done(answer.trim()));
    });
  };
  return {
    confirm: async (question) => /^(y|yes)$/i.test(await ask(question + " [y/N]") || ""),
    ask: (question) => ask(question),
    secret: (question) => ask(question, true),
    get closed() { return closed; },
    close() { active?.close(); },
  };
}

export async function executeOnboarding(plan, io) {
  const { log = console.log, stdinTTY = false } = io;
  log("COM8 setup plan:");
  for (const r of plan.requirements) log(`  ${requirementMet(r, plan.snapshot) ? "reuse" : "need"}: ${r.label}`);
  for (const step of plan.steps) log(`  ${step.command} ${step.args.map((a) => JSON.stringify(a)).join(" ")}${step.url ? ` (download ${step.url} to a private temporary file first)` : ""}`);
  log(`  com8 setup ${plan.setupArgs.join(" ")}`);
  if (plan.profileArgs.length) log(`  com8 profile preview/install ${plan.profileArgs.join(" ")} (managed shell/tmux/Ghostty configuration; no live reload)`);
  if (plan.options.loginClaude) log("  Claude login after setup, only if not already signed in (credentials handled by Claude)");
  if (plan.options.loginCodex) log("  Codex login after setup, only if not already signed in (credentials handled by Codex)");
  if (plan.options.model) log("  Add a private model connection for explicitly selected Claude Code/Codex launches; existing defaults and logins stay available.");
  if (plan.options.bus) log(plan.options.bus === "local" ? "  Select local bus use; registration starts its broker later." : `  Select the existing enrollment at ${plan.options.bus}.`);
  else if (plan.options.busInviteFile || plan.options.busInvitePrompt) log("  Join the hub in your private invitation (invitation contents are never printed).");
  else log("  Keep the current bus selection; no device enrollment or agent registration.");
  log("Packages installed by your package manager or provider installer remain yours; COM8 uninstall does not remove them.");
  for (const reason of plan.blocked) log(`  unavailable: ${reason}`);
  if (plan.options.dryRun) {
    log("Dry run: no downloads, package installs, configuration writes, services or login.");
    return { status: "dry-run", plan };
  }
  if (plan.blocked.length) throw new Error(plan.blocked.join("\n"));
  if (!plan.options.claude && !plan.options.codex && !plan.options.noClients)
    throw new Error("Choose --claude, --codex or --no-clients explicitly for guided/automated setup.");
  if (!stdinTTY && !plan.options.yes) throw new Error("Noninteractive guided setup requires --yes after reviewing --dry-run.");
  if ((plan.options.loginClaude || plan.options.loginCodex) && !stdinTTY) throw new Error("Provider login requires an interactive terminal; run the provider's own login separately.");
  if (plan.options.model && !stdinTTY) throw new Error("Model setup needs a terminal for its hidden key prompt. For automation use com8 model add with --key-file or --key-stdin.");
  let modelChoice;
  if (plan.options.model) {
    modelChoice = await io.prepareModel();
    if (!modelChoice) return { status: "cancelled", plan };
    log(`  Model connection: ${modelChoice.description}`);
  }
  let busChoice;
  if (plan.options.bus || plan.options.busInviteFile || plan.options.busInvitePrompt) {
    busChoice = await io.prepareBus(plan.options);
    if (!busChoice) return { status: "cancelled", plan };
    log(`  Bus choice: ${busChoice.description}`);
  }
  // Preview the exact managed profile changes before consent, when Python exists.
  const python = plan.requirements.find((r) => r.id === "python3");
  const preview = async () => {
    const result = await io.profile(["preview", ...plan.profileArgs]);
    const conflicts = result?.actions?.filter((action) => action.action === "conflict") || [];
    if (result?.ok === false || conflicts.length)
      throw new Error(`Profile preview found conflicts; no COM8 setup was activated. ${conflicts.map((c) => `${c.path}: ${c.reason}`).join("; ")}`);
  };
  let profilePreviewed = false;
  if (plan.profileArgs.length && requirementMet(python, plan.snapshot)) {
    await preview(); profilePreviewed = true;
  }
  if (!plan.options.yes && !await io.confirm("Apply this setup plan?")) return { status: "cancelled", plan };
  for (const step of plan.steps) {
    const now = await io.probe();
    if (step.requires.every((id) => requirementMet(plan.requirements.find((r) => r.id === id) || { id }, now))) continue;
    if (step.kind === "script") await io.installScript(step, plan.options);
    else await io.run(step.command, step.args, { step });
  }
  const verified = await io.probe();
  const missing = plan.requirements.filter((r) => !requirementMet(r, verified));
  if (missing.length) throw new Error(`Installed dependencies could not be verified: ${missing.map((r) => r.label).join(", ")}. No COM8 setup was activated; fix PATH/install prerequisites and rerun.`);
  if (plan.profileArgs.length && !profilePreviewed) {
    await preview();
    if (!plan.options.yes && !await io.confirm("Apply the displayed profile changes and COM8 setup?")) return { status: "cancelled", plan };
  }
  await io.setup(plan.setupArgs);
  if (plan.profileArgs.length) {
    try { await io.profile(["install", ...plan.profileArgs]); }
    catch (error) { throw new Error(`COM8 core setup completed, but profile installation failed: ${error.message}. Core installation and packages are retained; reconcile the profile conflict and rerun.`); }
  }
  if (busChoice) {
    try {
      await busChoice.apply();
      log(plan.options.bus === "local" ? "Local bus selected. Ask your agent to register when needed; no bus service was started." : "Bus selection completed. Open your agent session and ask it to register on the intended bus.");
    } catch {
      await io.doctor();
      throw new Error("COM8 core setup completed, but optional bus setup did not complete. The installation is retained. Inspect com8 bus status --no-start --json, then retry setup with the intended existing hub or a fresh private invitation. No agent registration is claimed.");
    }
  }
  if (modelChoice) {
    try {
      await modelChoice.apply();
      log("Model connection saved. Ask your agent to select it when launching a worker. Catalog access and real inference can be checked separately.");
    } catch {
      throw new Error("COM8 core setup completed, but optional model setup did not complete. The installation is retained. Inspect com8 model list and retry com8 model add; no model connection success is claimed.");
    }
  }
  await io.doctor();
  for (const provider of ["claude", "codex"]) {
    const key = provider === "claude" ? "loginClaude" : "loginCodex";
    if (!plan.options[key]) continue;
    if (await io.inspectLogin(provider)) { log(`${provider}: already signed in.`); continue; }
    await io.login(provider);
    if (!await io.inspectLogin(provider)) throw new Error(`${provider}: login did not complete. COM8 remains installed; rerun the provider's login yourself.`);
  }
  log("Setup complete. Open fresh client sessions to load COM8. Provider login and package removal remain managed by their own tools.");
  return { status: "complete", plan };
}

export async function runOnboarding(argv, injected = {}) {
  const stdinTTY = injected.stdinTTY ?? !!process.stdin.isTTY;
  const stdoutTTY = injected.stdoutTTY ?? !!process.stdout.isTTY;
  const options = parseOnboardingArgs(argv);
  if (!argv.length && stdinTTY && stdoutTTY) options.guided = true;
  if (options.model && stdinTTY && !options.claude && !options.codex && !options.noClients) options.guided = true;
  if (options.guided) options.installMissing = true;
  const prompt = !options.dryRun && stdinTTY && !injected.confirm ? promptSession() : null;
  const confirm = injected.confirm || prompt?.confirm || (async () => false);
  const ask = injected.ask || prompt?.ask || (async () => undefined);
  const secret = injected.secret || prompt?.secret || (async () => undefined);
  const busStatus = injected.busStatus || (() => inspectBus({ env: environment() }));
  const applyBus = injected.applyBus || applyBusChoice;
  const probe = injected.probe || probeOnboarding;
  const profile = async (args) => {
    const python = (await probe()).tools.python3?.path;
    if (!python) throw new Error("Python 3.9+ is required to preview/install profiles");
    let result;
    await withSelectedPath(async () => { result = await (injected.run || runOnboardingCommand)(python, ["-B", path.join(pkgDir, "vendor/profiles/manage.py"), ...args], { capture: true }); });
    if (!result?.stdout) throw new Error("Profile manager returned no report; no profile success is claimed.");
    let report;
    try { report = JSON.parse(result.stdout); } catch { throw new Error("Profile manager returned an invalid report"); }
    (injected.log || console.log)(result.stdout.trim());
    return report;
  };
  const withSelectedPath = async (callback) => {
    const selected = await probe();
    const previous = process.env.PATH;
    const directories = ["bash", "python3", "claude", "codex", "ghostty"].map((id) => selected.tools[id]?.path).filter((p) => p?.startsWith("/")).map((p) => path.dirname(p));
    process.env.PATH = [...new Set([...directories, ...environment().PATH.split(path.delimiter)])].join(path.delimiter);
    try { await callback(); }
    finally { if (previous === undefined) delete process.env.PATH; else process.env.PATH = previous; }
  };
  const applySelectedBus = async (choice) => {
    let result;
    await withSelectedPath(async () => { result = await applyBus(choice, { env: environment() }); });
    return result;
  };
  try {
    if (options.guided && !options.dryRun && stdinTTY) {
      if (!options.claude && !options.codex && !options.noClients) {
        options.claude = await confirm("Set up Claude Code (install its CLI if missing)?");
        options.codex = await confirm("Set up Codex (install its CLI if missing)?");
        options.noClients = !options.claude && !options.codex;
      }
      if (!options.terminal) options.terminal = await confirm("Add the terminal profile and missing Bash/tmux/fzf tools?");
      if (!options.mesh) options.mesh = await confirm("Add the mesh profile and missing SSH/jq/fzf tools?");
      if (!options.ghostty && (await probe()).platform === "darwin")
        options.ghostty = await confirm("Install Ghostty and its configured Nerd Font with the terminal profile?");
      if (options.ghostty) options.terminal = true;
      if (!options.service && !options.noService) options.service = await confirm("Enable the per-user COM8 daemon service?");
      if (!options.model && (options.claude || options.codex)) options.model = await confirm("Configure a model API connection, such as GLM, to power your coding agents?");
      if (prompt?.closed) return { status: "cancelled" };
      // Login is a separate choice, never implied by installing a provider CLI.
      if (!options.model && options.claude && !options.loginClaude) options.loginClaude = await confirm("After setup, offer Claude's own login if not already signed in?");
      if (!options.model && options.codex && !options.loginCodex) options.loginCodex = await confirm("After setup, offer Codex's own login if not already signed in?");
      if (prompt?.closed) return { status: "cancelled" };
      if (!options.bus && !options.busInviteFile) {
        (injected.log || console.log)(`Bus: ${busSummary(await busStatus())}`);
        if (await confirm("Choose a local or shared bus now? Keeping the current selection is fine.")) {
          const choice = await ask("Enter local, invitation, or an already-connected HTTPS address (empty keeps the current selection):");
          if (choice === "invitation") options.busInvitePrompt = true;
          else if (choice) options.bus = choice === "local" ? "local" : busOrigin(choice);
        }
        if (prompt?.closed) return { status: "cancelled" };
      }
    }
    const io = { log: console.log, stdinTTY, confirm, probe, run: runOnboardingCommand,
      setup: async (args) => {
        // Make newly installed per-user/Homebrew commands visible to the
        // existing installer without changing the user's shell configuration.
        await withSelectedPath(async () => (await import("./setup.mjs")).runSetup(args));
      },
      profile,
      prepareModel: () => prepareModelChoice({ ask, secret, add: async (args, key) => {
        let result;
        await withSelectedPath(async () => { result = await (injected.addModel || addModelConnection)(args, key, { env: environment() }); });
        return result;
      } }),
      prepareBus: async (selected) => {
        if (selected.bus) return { description: selected.bus === "local" ? "local, without starting a service" : `existing enrollment at ${selected.bus}`,
          apply: () => applySelectedBus(selected.bus === "local" ? { mode: "local" } : { mode: "existing", hub: selected.bus }) };
        const code = selected.busInviteFile ? readPrivateInvitation(selected.busInviteFile) : await secret("Paste the invitation (hidden; empty cancels):");
        if (!code) return null;
        const hub = invitationOrigin(code);
        if (stdinTTY && !await confirm(`Enroll this installation at ${hub} using this invitation?`)) return null;
        return { description: `invited enrollment at ${hub}`, apply: () => applySelectedBus({ mode: "invite", code }) };
      },
      doctor: async () => { await withSelectedPath(async () => (await import("./setup.mjs")).runDoctor()); },
      installScript: (step, options) => installScript(step, options, injected.run || runOnboardingCommand),
      inspectLogin: async (provider) => {
        const r = spawnSync(find(provider) || provider, provider === "claude" ? ["auth", "status"] : ["login", "status"], { env: environment(), stdio: "ignore", timeout: 10000 });
        if (r.error || ![0, 1].includes(r.status)) throw new Error(`${provider}: could not inspect login state; use the provider's own login command.`);
        return r.status === 0;
      },
      login: async (provider) => (injected.run || runOnboardingCommand)(provider, provider === "claude" ? ["auth", "login"] : ["login"]),
      ...injected,
    };
    return await executeOnboarding(planOnboarding(options, await probe()), io);
  } finally { prompt?.close(); }
}
