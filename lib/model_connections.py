#!/usr/bin/env python3
"""Private model connections for existing coding clients, not a model proxy."""
import argparse
import contextlib
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.dont_write_bytecode = True
OWNER = "com8-model-connection-v1"
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}\Z")
MAX_KEY = 8192
CLAUDE_ALTERNATE_AUTH = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
                        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR", "CCR_OAUTH_TOKEN_FILE")


class ConnectionError(Exception):
    pass


def require(value, message):
    if not value:
        raise ConnectionError(message)


def absolute(value):
    path = Path(value).expanduser()
    require(path.is_absolute(), "configuration paths must be absolute")
    return Path(os.path.abspath(path))


def config_root():
    return absolute(os.environ.get("COM8_MODEL_CONFIG") or
                    str(absolute(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "com8/models"))


def no_links(path):
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise ConnectionError("refusing a symlink in a configuration path")


def private_dir(path, create=False):
    no_links(path)
    if create and not path.exists():
        # Every directory we create is private; existing ancestors are unchanged.
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for current in reversed(missing):
            current.mkdir(mode=0o700)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and
            stat.S_IMODE(info.st_mode) & 0o077 == 0, "model configuration directory must be owned and private (0700)")


def private_read(path, limit=1024 * 1024):
    no_links(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
                stat.S_IMODE(info.st_mode) & 0o077 == 0, "credential/configuration file must be owned and private (0600)")
        value = stream.read(limit + 1)
    require(len(value) <= limit, "credential/configuration file exceeds its size limit")
    return value


def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        return os.fstat(stream.fileno())


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


@contextlib.contextmanager
def locked(root):
    private_dir(root, create=True)
    fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
                stat.S_IMODE(info.st_mode) & 0o077 == 0, "model configuration lock is not private")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def valid_name(name):
    require(isinstance(name, str) and NAME.fullmatch(name), "connection name must use lowercase letters, digits, hyphens or underscores (max 48)")
    return name


def endpoint(value, allow_loopback=False):
    require(isinstance(value, str) and len(value) <= 2048 and not any(c.isspace() for c in value), "invalid model endpoint")
    try:
        url = urllib.parse.urlsplit(value)
        host, port = url.hostname, url.port
    except ValueError:
        raise ConnectionError("invalid model endpoint") from None
    require(host and not url.username and not url.password and not url.query and not url.fragment,
            "model endpoint must not contain credentials, a query or fragment")
    loopback = False
    with contextlib.suppress(ValueError):
        loopback = ipaddress.ip_address(host).is_loopback
    require(url.scheme == "https" or (allow_loopback and url.scheme == "http" and loopback),
            "model endpoint requires HTTPS; HTTP is only allowed for an explicit loopback fixture")
    require(not any(c in url.path for c in "\r\n\\"), "invalid model endpoint path")
    return value.rstrip("/")


def credential(raw):
    require(len(raw) <= MAX_KEY, "credential exceeds 8192 bytes")
    try:
        value = raw.decode("ascii").strip()
    except UnicodeError:
        raise ConnectionError("credential must be a nonempty ASCII token") from None
    require(value and all(33 <= ord(c) <= 126 for c in value), "credential must be a nonempty ASCII token without whitespace")
    return value


def validate_metadata(meta, name):
    require(isinstance(meta, dict) and meta.get("managed_by") == OWNER and meta.get("name") == valid_name(name),
            "connection is not owned by COM8")
    require(set(meta) <= {"managed_by", "name", "model", "base_url", "anthropic_base_url", "context_window", "allow_loopback_http"},
            "connection metadata contains unrecognized fields")
    model = meta.get("model")
    require(isinstance(model, str) and 0 < len(model) <= 256 and not any(ord(c) < 33 for c in model), "invalid model identifier")
    endpoint(meta.get("base_url"), meta.get("allow_loopback_http") is True)
    if meta.get("anthropic_base_url"):
        endpoint(meta["anthropic_base_url"], meta.get("allow_loopback_http") is True)
        a, b = urllib.parse.urlsplit(meta["base_url"]), urllib.parse.urlsplit(meta["anthropic_base_url"])
        require((a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port), "Claude and Codex endpoints must share the same origin")
    if "context_window" in meta:
        require(type(meta["context_window"]) is int and 1024 <= meta["context_window"] <= 10_000_000, "context window must be between 1024 and 10000000 tokens")
    return meta


def load(name, root=None):
    root = config_root() if root is None else absolute(root)
    valid_name(name)
    private_dir(root)
    directory = root / name
    private_dir(directory)
    require({p.name for p in directory.iterdir()} == {"connection.json", "credential", "ownership.json"},
            "connection contains unowned files; inspect it before changing it")
    meta_raw = private_read(directory / "connection.json")
    key_raw = private_read(directory / "credential", MAX_KEY)
    owner = json.loads(private_read(directory / "ownership.json"))
    require(owner == {"managed_by": OWNER, "connection_sha256": digest(meta_raw), "credential_sha256": digest(key_raw)},
            "connection files changed outside COM8; refusing to use or remove them")
    return validate_metadata(json.loads(meta_raw), name), credential(key_raw)


def public(meta):
    return {k: v for k, v in meta.items() if k not in ("managed_by", "allow_loopback_http")} | {"configured": True}


def add(args):
    name = valid_name(args.name)
    meta = {"managed_by": OWNER, "name": name, "model": args.model,
            "base_url": endpoint(args.base_url, args.allow_loopback_http)}
    if args.anthropic_base_url:
        meta["anthropic_base_url"] = endpoint(args.anthropic_base_url, args.allow_loopback_http)
    if args.context_window is not None:
        meta["context_window"] = args.context_window
    if args.allow_loopback_http:
        meta["allow_loopback_http"] = True
    validate_metadata(meta, name)
    root = config_root()
    no_links(root)
    require(not (root / name).exists() and not (root / name).is_symlink(), "connection already exists; remove the owned connection before replacing it")
    if args.dry_run:
        return {"ok": True, "dry_run": True, "connection": public(meta), "credential_read": False}
    if args.key_file:
        raw = private_read(absolute(args.key_file), MAX_KEY)
    elif sys.stdin.isatty():
        raw = getpass.getpass("Model API key: ").encode()
    else:
        raw = sys.stdin.buffer.read(MAX_KEY + 1)
    secret = credential(raw).encode()
    with locked(root):
        require(not (root / name).exists() and not (root / name).is_symlink(), "connection already exists")
        staging = Path(tempfile.mkdtemp(prefix=".add-", dir=root))
        try:
            metadata = encoded(meta)
            write_new(staging / "connection.json", metadata)
            write_new(staging / "credential", secret)
            write_new(staging / "ownership.json", encoded({"managed_by": OWNER,
                "connection_sha256": digest(metadata), "credential_sha256": digest(secret)}))
            staging.rename(root / name)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {"ok": True, "connection": public(meta)}


def list_connections():
    root = config_root()
    no_links(root)
    if not root.exists():
        return {"ok": True, "connections": []}
    private_dir(root)
    connections = [public(load(path.name, root)[0]) for path in sorted(root.iterdir()) if NAME.fullmatch(path.name)]
    return {"ok": True, "connections": connections}


def remove(name):
    root = config_root()
    require(root.exists(), "model connection not found")
    with locked(root):
        meta, _ = load(name, root)
        directory = root / name
        for filename in ("credential", "connection.json", "ownership.json"):
            (directory / filename).unlink()
        directory.rmdir()
    return {"ok": True, "removed": name}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def doctor(name):
    meta, key = load(name)
    request = urllib.request.Request(meta["base_url"] + "/models", headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            require(response.status == 200, "model catalog request failed")
            body = response.read(1024 * 1024 + 1)
        require(len(body) <= 1024 * 1024, "model catalog response exceeds its size limit")
        data = json.loads(body)
        require(isinstance(data, dict) and isinstance(data.get("data"), list), "model catalog returned an invalid response")
        found = any(isinstance(row, dict) and row.get("id") == meta["model"] for row in data["data"])
        require(found, "configured model is not available in the authenticated catalog")
    except urllib.error.HTTPError as error:
        error.close()
        raise ConnectionError("model catalog request rejected (HTTP %d)" % error.code) from None
    except (urllib.error.URLError, TimeoutError):
        raise ConnectionError("model catalog is unreachable or TLS verification failed") from None
    return {"ok": True, "connection": public(meta), "checks": {"catalog_access": True, "model_present": True}, "inference_tested": False}


def client_home(cli, env=None):
    env = os.environ if env is None else env
    key = "CODEX_HOME" if cli == "codex" else "CLAUDE_CONFIG_DIR"
    path = absolute(env.get(key) or str(Path(env.get("HOME") or str(Path.home())) / (".codex" if cli == "codex" else ".claude")))
    no_links(path)
    require(path.is_dir() and path.stat().st_uid == os.getuid(), "client configuration home must already exist and be owned; run COM8 setup for this client first")
    return path


def validate_launch(name, cli, root=None):
    require(cli in ("codex", "claude"), "model connections support only codex or claude")
    meta, key = load(name, root)
    if cli == "claude":
        require(meta.get("anthropic_base_url"), "Claude requires an explicit Anthropic-compatible endpoint; add --anthropic-base-url")
    return meta, key


def spawn_command(name, cli):
    """Validate before claim; the command contains paths/names only, no key."""
    validate_launch(name, cli)
    home = client_home(cli)
    root = config_root()
    key = "CODEX_HOME" if cli == "codex" else "CLAUDE_CONFIG_DIR"
    python = os.environ.get("COM8_PYTHON") or sys.executable
    return shlex.join(["env", "COM8_MODEL_CONFIG=" + str(root), key + "=" + str(home),
                       python, str(Path(__file__).resolve()), "run", name, "--cli", cli] +
                      (["--accept-inbound"] if cli == "claude" else []))


def launch_env(cli, home, key):
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(("ANTHROPIC_", "OPENAI_", "CLAUDE_CODE_USE_")) or name in CLAUDE_ALTERNATE_AUTH or name in {
                "CLAUDE_CODE_SUBAGENT_MODEL",
                "CLAUDE_CODE_SIMPLE",
                "CODEX_THREAD_ID", "CODEX_API_KEY", "ANU_ACCOUNT", "ANU_PROVIDER", "ANU_LAUNCH_NONCE", "COM8_ACCOUNT"}:
            env.pop(name, None)
    env["CODEX_HOME" if cli == "codex" else "CLAUDE_CONFIG_DIR"] = str(home)
    env.pop("COM8_MODEL_API_KEY", None)
    if cli == "codex":
        env["COM8_MODEL_API_KEY"] = key
    return env


def validate_client_args(cli, args):
    forbidden = ({"-m", "--model", "-p", "--profile", "-c", "--config", "--oss", "--local-provider", "--remote"}
                 if cli == "codex" else {"--model", "--settings", "--setting-sources", "--fallback-model",
                    "--bare", "--bg", "--background", "--cloud", "--environment", "--teleport",
                    "--remote-control", "--remote-control-session-name-prefix"})
    # These are client management/remote modes, not a local coding session.
    # In particular, background execution would outlive the owned launch file.
    non_session = ({"app", "app-server", "cloud", "queue", "login", "logout", "remote-control", "exec-server"}
                   if cli == "codex" else {"attach", "agents", "respawn", "gateway", "ultrareview"})
    require(not args or args[0] not in non_session,
            "model connections launch local coding sessions; remote, shared-server and background modes are not supported")
    for arg in args:
        require(arg.split("=", 1)[0] not in forbidden and not
                (cli == "codex" and re.match(r"^-[cmp].+", arg)),
                "client arguments cannot override the selected model connection or detach its launch")


def codex_profile(meta):
    # JSON string literals are valid TOML basic strings for these values.
    q = json.dumps
    text = ["model = " + q(meta["model"]), "review_model = " + q(meta["model"]),
            'model_provider = "com8_connection"', 'web_search = "disabled"']
    if meta.get("context_window"):
        text += ["model_context_window = " + str(meta["context_window"]),
                 "model_auto_compact_token_limit = " + str(meta["context_window"] * 4 // 5)]
    text += ['[model_providers.com8_connection]', 'name = "COM8 model connection"',
             "base_url = " + q(meta["base_url"]), 'wire_api = "responses"',
             'env_key = "COM8_MODEL_API_KEY"', "requires_openai_auth = false", "supports_websockets = false",
             # Preserve both current keyed filters and legacy exclude/include
             # arrays inherited from the user's config. They cannot coexist.
             '[shell_environment_policy]', 'ignore_default_excludes = false',
             '[shell_environment_policy.set]', 'COM8_MODEL_API_KEY = ""']
    return ("\n".join(text) + "\n").encode()


def check_pane():
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return
    require(re.fullmatch(r"%[0-9]+", pane), "cannot verify this pane; launch in a fresh COM8 seat")
    tmux = shutil.which("tmux")
    require(tmux, "cannot inspect account ownership; launch in a fresh COM8 seat")
    result = subprocess.run([tmux, "show-options", "-pqv", "-t", pane, "@anu_account"], capture_output=True, timeout=5)
    require(result.returncode == 0, "cannot inspect account ownership; launch in a fresh COM8 seat")
    require(not result.stdout.strip(), "this pane belongs to subscription account rotation; launch the model connection in a fresh COM8 seat")


def run(name, cli, args, accept_inbound=False):
    # Endpoint and credential must come from one verified record. Loading twice
    # can mix two connections when another setup rotates the saved connection.
    meta, key = validate_launch(name, cli)
    home = client_home(cli)
    validate_client_args(cli, args)
    check_pane()
    executable = shutil.which(cli)
    require(executable, "selected coding client is not installed; run COM8 setup first")
    env = launch_env(cli, home, key)
    if cli == "codex":
        # Avoid sending a per-launch credential into an existing shared server.
        check = subprocess.run([executable, "--version"], env=env, capture_output=True, timeout=10)
        version = re.search(rb"codex-cli (\d+)\.(\d+)\.(\d+)", check.stdout)
        require(check.returncode == 0 and version and tuple(map(int, version.groups())) >= (0, 156, 0),
                "model connections require Codex 0.156.0 or newer")
        profile = "com8-connection-" + uuid.uuid4().hex
        path, contents = home / (profile + ".config.toml"), codex_profile(meta)
        command = [executable, "--no-daemon", "--profile", profile, "--model", meta["model"], *args]
    else:
        env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_AUTH_TOKEN"] = meta["anthropic_base_url"], key
        for alias in ("SONNET", "OPUS", "HAIKU", "FABLE"):
            env["ANTHROPIC_DEFAULT_" + alias + "_MODEL"] = meta["model"]
        env["ANTHROPIC_SMALL_FAST_MODEL"] = meta["model"]
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = meta["model"]
        env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
        path = home / (".com8-connection-" + uuid.uuid4().hex + ".json")
        settings_env = {k: v for k, v in env.items() if k.startswith("ANTHROPIC_") or
                        k in ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS")}
        # User/project settings can restore env values after process-env cleanup.
        # Override the alternate routes and credential channels at that layer too.
        settings_env.update({name: "" for name in CLAUDE_ALTERNATE_AUTH})
        settings_env.update({"ANTHROPIC_API_KEY": "", "ANTHROPIC_CUSTOM_HEADERS": "",
                             "ANTHROPIC_UNIX_SOCKET": "", "CLAUDE_CODE_USE_GATEWAY": "0",
                             "CLAUDE_CODE_USE_BEDROCK": "0", "CLAUDE_CODE_USE_VERTEX": "0",
                             "CLAUDE_CODE_USE_FOUNDRY": "0", "CLAUDE_CODE_USE_MANTLE": "0",
                             "CLAUDE_CODE_USE_AWS": "0"})
        settings = {"model": meta["model"], "apiKeyHelper": "", "env": settings_env}
        if accept_inbound:
            settings["crossSessionInbound"] = "accept"
        contents = encoded(settings)
        command = [executable, "--settings", str(path), "--model", meta["model"], *args]
    written = write_new(path, contents)
    process = None
    previous = {}
    try:
        process = subprocess.Popen(command, env=env)
        def forward(signum, _frame):
            if process.poll() is None:
                process.send_signal(signum)
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, forward)
        returncode = process.wait()
        return returncode if returncode >= 0 else 128 - returncode
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        # Remove only the exact file created by this launch. External edits stay.
        if process is None or process.poll() is not None:
            try:
                info = path.lstat()
                if (info.st_dev, info.st_ino) == (written.st_dev, written.st_ino) and private_read(path) == contents:
                    path.unlink()
                else:
                    print("com8 model: launch profile changed; retained for inspection", file=sys.stderr)
            except (OSError, ConnectionError):
                print("com8 model: launch profile could not be verified; retained for inspection", file=sys.stderr)


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        # argparse otherwise reflects unknown arguments, which may contain a key.
        raise ConnectionError("invalid model command arguments; use com8 model --help")


def parser():
    ap = Parser(description=__doc__)
    commands = ap.add_subparsers(dest="command", required=True, parser_class=Parser)
    p = commands.add_parser("add")
    p.add_argument("name")
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--anthropic-base-url")
    p.add_argument("--context-window", type=int)
    p.add_argument("--allow-loopback-http", action="store_true", help="explicit local fixture only")
    keys = p.add_mutually_exclusive_group(required=True)
    keys.add_argument("--key-file")
    keys.add_argument("--key-stdin", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    for verb in ("list", "doctor", "remove"):
        p = commands.add_parser(verb)
        if verb != "list":
            p.add_argument("name")
        p.add_argument("--json", action="store_true")
    p = commands.add_parser("run")
    p.add_argument("name")
    p.add_argument("--cli", required=True, choices=("codex", "claude"))
    p.add_argument("--accept-inbound", action="store_true", help=argparse.SUPPRESS)
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    want_json = "--json" in argv
    try:
        client_args = []
        if argv and argv[0] == "run" and "--" in argv:
            offset = argv.index("--")
            argv, client_args = argv[:offset], argv[offset + 1:]
        args = parser().parse_args(argv)
        if args.command == "run":
            return run(args.name, args.cli, client_args, args.accept_inbound)
        result = (add(args) if args.command == "add" else list_connections() if args.command == "list"
                  else doctor(args.name) if args.command == "doctor" else remove(args.name))
        print(json.dumps(result) if want_json else json.dumps(result, indent=2))
        return 0
    except (ConnectionError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, ConnectionError) else "model operation failed; check private file ownership, configuration and client availability"
        if want_json:
            print(json.dumps({"ok": False, "error": message}))
        else:
            print("com8 model: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
