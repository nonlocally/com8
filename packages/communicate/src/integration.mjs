// Client-specific registration files live outside immutable release payloads.
// Agent hosts may filter their MCP child's environment, so the descriptor must
// carry the selected installation and nonsecret runtime paths explicitly.
import fs from 'node:fs';
import path from 'node:path';
import { home, dataRoot, stateRoot, currentLink, executable, hash, assertManagedPath } from './lifecycle.mjs';

const walk = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
  entry.isDirectory() ? walk(path.join(dir, entry.name)) : [path.join(dir, entry.name)]);
const shell = (value) => "'" + String(value).replaceAll("'", "'\\''") + "'";
const absolute = (value) => path.resolve(value);
function runtimeEnvironment() {
  const env = { HOME: absolute(home()), COMMUNICATE_DATA: absolute(dataRoot()), COMM_STATE: absolute(stateRoot()),
    CLAUDE_CONFIG_DIR: absolute(process.env.CLAUDE_CONFIG_DIR || path.join(home(), '.claude')),
    CODEX_HOME: absolute(process.env.CODEX_HOME || path.join(home(), '.codex')) };
  for (const key of ['COM8_SOCK_DIR', 'COM8_SESSIONS_DIR', 'XDG_RUNTIME_DIR']) if (process.env[key]) env[key] = absolute(process.env[key]);
  if (process.env.COMM_BUS_PORT !== undefined) {
    if (!/^\d+$/.test(process.env.COMM_BUS_PORT) || Number(process.env.COMM_BUS_PORT) > 65535)
      throw new Error('COMM_BUS_PORT must be an integer from 0 through 65535');
    env.COMM_BUS_PORT = String(Number(process.env.COMM_BUS_PORT));
  }
  for (const key of ['COM8_TMUX_SOCKET', 'COM8_SELF']) if (process.env[key]) env[key] = process.env[key];
  return env;
}
function verify(root, record) {
  const metaFile = path.join(root, 'integration.json');
  const raw = fs.readFileSync(metaFile);
  if (hash(raw) !== record.manifestHash) throw new Error(`Integration ownership changed: ${root}`);
  const manifest = JSON.parse(raw);
  const expectedFiles = new Set([...Object.keys(manifest.files), 'integration.json']);
  for (const file of walk(root)) {
    if (fs.lstatSync(file).isSymbolicLink() || !expectedFiles.delete(path.relative(root, file)))
      throw new Error(`Integration contains an unowned file: ${file}`);
  }
  if (expectedFiles.size) throw new Error(`Integration files missing: ${root}`);
  for (const [name, expected] of Object.entries(manifest.files)) {
    const file = path.resolve(root, name);
    if (!file.startsWith(root + path.sep) || hash(fs.readFileSync(file)) !== expected)
      throw new Error(`Integration file changed: ${file}`);
  }
}
export function buildIntegration(sourceRoot, { dry = false } = {}) {
  const source = path.join(sourceRoot, 'vendor');
  const environment = runtimeEnvironment();
  const node = executable('node');
  if (!node) throw new Error('Node must be available on PATH when registering an agent client');
  const inputFiles = {};
  for (const folder of ['plugins', '.agents']) {
    const dir = path.join(source, folder);
    if (!fs.existsSync(dir)) throw new Error(`Client descriptors missing: ${dir}`);
    for (const file of walk(dir).sort()) inputFiles[path.relative(source, file)] = hash(fs.readFileSync(file));
  }
  const signature = hash(JSON.stringify({ inputFiles, environment, node, entry: path.join(currentLink(), 'src/cli.mjs') }));
  const root = path.join(dataRoot(), 'integrations', signature.slice(0, 20));
  assertManagedPath(root);
  const base = JSON.parse(fs.readFileSync(path.join(source, 'plugins/communicate/.claude-plugin/plugin.json'))).version;
  const pluginVersion = base + (base.includes('+') ? '.' : '+') + 'install.' + signature.slice(0, 12);
  const partial = { root, signature, pluginVersion, environment, node };
  if (dry) {
    console.log(`[dry-run] would register client projection ${root} (${pluginVersion})`);
    console.log(`[dry-run] MCP runtime paths: ${JSON.stringify(environment)}`);
    return partial;
  }
  if (fs.existsSync(root)) {
    const meta = fs.readFileSync(path.join(root, 'integration.json'));
    const saved = JSON.parse(meta);
    if (saved.signature !== signature) throw new Error(`Integration path collision: ${root}`);
    const record = { ...partial, manifestHash: hash(meta), created: false };
    verify(root, record);
    return record;
  }
  fs.mkdirSync(path.dirname(root), { recursive: true, mode: 0o700 });
  const temp = fs.mkdtempSync(path.join(path.dirname(root), '.com8-integration-'));
  try {
    for (const folder of ['plugins', '.agents']) fs.cpSync(path.join(source, folder), path.join(temp, folder), { recursive: true });
    const plugin = path.join(temp, 'plugins/communicate');
    for (const kind of ['.claude-plugin', '.codex-plugin']) {
      const file = path.join(plugin, kind, 'plugin.json');
      const contents = JSON.parse(fs.readFileSync(file)); contents.version = pluginVersion;
      fs.writeFileSync(file, JSON.stringify(contents, null, 2) + '\n');
    }
    const mcp = JSON.parse(fs.readFileSync(path.join(plugin, '.mcp.json')));
    mcp.mcpServers.communicate = { type: 'stdio', command: node,
      args: [absolute(path.join(currentLink(), 'src/cli.mjs')), 'serve'], env: environment };
    fs.writeFileSync(path.join(plugin, '.mcp.json'), JSON.stringify(mcp, null, 2) + '\n');
    const exported = Object.entries(environment).map(([key, value]) => `export ${key}=${shell(value)}`).join('\n');
    fs.writeFileSync(path.join(plugin, 'bin/communicate'), `#!/usr/bin/env bash\nset -euo pipefail\n${exported}\nexec ${shell(absolute(path.join(currentLink(), 'vendor/bin/communicate')))} "$@"\n`, { mode: 0o755 });
    fs.writeFileSync(path.join(plugin, 'bin/communicate-mcp'), `#!/usr/bin/env bash\nset -euo pipefail\n${exported}\nexec ${shell(node)} ${shell(absolute(path.join(currentLink(), 'src/cli.mjs')))} serve\n`, { mode: 0o755 });
    const files = {};
    for (const file of walk(temp).sort()) files[path.relative(temp, file)] = hash(fs.readFileSync(file));
    const metadata = JSON.stringify({ ...partial, files }, null, 2) + '\n';
    fs.writeFileSync(path.join(temp, 'integration.json'), metadata, { mode: 0o600 });
    fs.renameSync(temp, root);
    console.log(`client projection -> ${root} (${pluginVersion})`);
    console.log(`MCP runtime paths: ${JSON.stringify(environment)}`);
    return { ...partial, manifestHash: hash(metadata), created: true };
  } finally { fs.rmSync(temp, { recursive: true, force: true }); }
}
export function removeIntegration(record) {
  if (!record?.root || !fs.existsSync(record.root)) return true;
  assertManagedPath(record.root);
  const root = path.resolve(record.root);
  if (path.dirname(root) !== path.resolve(dataRoot(), 'integrations')) throw new Error(`Refusing unrecognized integration directory: ${root}`);
  try { verify(root, record); }
  catch (error) { console.log(`kept modified client integration: ${error.message}`); return false; }
  fs.rmSync(root, { recursive: true });
  return true;
}
