"""com8 adopt: one command onboards a device where you have an account.

Everything the fleet needed by hand — encoded. The ceremony:
  probe -> plan -> provision -> pair -> (spawn) -> human checklist

The PLANNER is pure (facts in, actions out) so every field lesson is a unit
test, not tribal knowledge:
  - reverse keys: the far device dials the hub with its OWN key, installed
    during adopt over the already-authenticated channel (never a laptop's
    forwarded agent — a field device lost its reverse leg that way).
  - dial address: when the hub's bare device name doesn't resolve from the
    far side (off-tailnet or without MagicDNS), the address
    that provably works is the one this very ssh connection came from —
    $SSH_CONNECTION — written as a Host alias.
  - runtime dir: claude puts its cross-session socket in
    $XDG_RUNTIME_DIR/cc-socks falling back to /tmp/cc-socks; on a shared
    mac another uid owns /tmp/cc-socks and live delivery silently dies.
    Darwin has no systemd runtime dirs, so provision ~/.local/run always;
    Linux only when $XDG_RUNTIME_DIR is unset (Termux).
  - kernel: hash-compared, never version-string-compared (pair kept stale
    bytes on multiple hosts); refresh restarts the far daemon.
  - CLI shim + spawn (settings via a FILE — inline JSON dies in nested
    shell quoting, proven twice).
Steps that genuinely need the human (claude /login, a Tailscale SSH check)
are detected and printed as a checklist, not discovered by timeout.
"""
import os
import shlex
import subprocess
import time

from com8_payload import KERNEL_FILES

_FAR_PATH = "/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"


# ---------------------------------------------------------------- probe

def probe_script(my_addr, hub_key_material=""):
    """One remote sh script emitting KEY=VALUE lines — a single round-trip.
    my_addr: how the far side would dial the hub (user@devname).
    hub_key_material: the hub's own fleet pubkey material, so the probe can
    report whether this device already trusts the hub for outbound dials."""
    kf = " ".join("~/.local/share/com8/daemon/current/%s" % f
                  for f in KERNEL_FILES)
    return (
        'echo "OS=$(uname -s)"; echo "HOMEDIR=$HOME"; echo "SHELL_=$SHELL"; '
        'echo "UNAME_O=$(uname -o 2>/dev/null)"; echo "PREFIX=$PREFIX"; '
        'echo "COM8_SELF_=$COM8_SELF"; '
        # The probe runs `ssh <host> sh -s`: non-login and non-interactive,
        # so it reads NO profile. Asking $COM8_SELF here answers "is it in
        # MY shell", which is always no — a checklist item keyed on that can
        # never clear, and a checklist that stays lit after the human did
        # the thing teaches them to ignore the next one. Ask instead which
        # profiles actually carry the export.
        # Read the VALUE from the device's own login shell in login mode —
        # by construction the environment its agent gets. Grepping the
        # assignment cannot resolve COM8_SELF="$(hostname)", quoting, or a
        # later reassignment, and would fail quietly toward "configured".
        # (This is not the bug that was reported: that was reading
        # $COM8_SELF in a NON-login sh, which can never see a profile.)
        # The default is emitted first so the key always exists; the login
        # shell's own line overwrites it, and any chatter a profile prints
        # lands on other lines that parse_facts ignores.
        'echo "COM8_SELF_VALUE="; '
        '"$SHELL" -lc \'printf "COM8_SELF_VALUE=%%s\\n" "$COM8_SELF"\' '
        '2>/dev/null; '
        'hsf=""; for f in .bash_profile .bashrc .profile .zshenv .zshrc; do '
        'grep -qs "^[[:space:]]*export COM8_SELF=" "$HOME/$f" && '
        'hsf="$hsf $f"; done; echo "COM8_SELF_FILES=$hsf"; '
        'echo "XDG=$XDG_RUNTIME_DIR"; '
        'echo "SSHIP=${SSH_CONNECTION%%%% *}"; '
        'o=$(stat -c %%u /tmp/cc-socks 2>/dev/null) || '
        'o=$(stat -f %%u /tmp/cc-socks 2>/dev/null); '
        'echo "CCOWNER=$o"; echo "UID_=$(id -u)"; '
        '[ -f ~/.ssh/id_ed25519.pub ] && echo "OWNKEY=1" || echo "OWNKEY=0"; '
        '[ -f ~/.ssh/id_com8 ] && echo "COM8KEY=1" || echo "COM8KEY=0"; '
        '%(hubk)s'
        'command -v python3 >/dev/null && echo "PY3=1" || echo "PY3=0"; '
        'echo "TMUX_BIN=$(PATH=%(p)s command -v tmux)"; '
        'echo "CLAUDE_BIN=$(PATH=%(p)s command -v claude)"; '
        'echo "SHIM=$([ -x ~/.local/bin/communicate ] && echo 1 || echo 0)"; '
        'if [ -f ~/.local/share/com8/daemon/current/com8.py ]; then '
        'echo "KHASH=$(cat %(kf)s 2>/dev/null | md5 -q 2>/dev/null || '
        'cat %(kf)s 2>/dev/null | md5sum 2>/dev/null | cut -d" " -f1)"; '
        'else echo "KHASH="; fi; '
        # REV counts ONLY when the fabric key exists: `-i <missing>` does not
        # force a failure, so ssh falls through to an agent or another
        # identity and reports a reverse leg the DAEMON will not have
        # (seen live: adopt skipped authorizing the key, and pair then
        # found the reverse leg dead).
        '[ -f ~/.ssh/id_com8 ] && ssh -o BatchMode=yes -o ConnectTimeout=6 '
        '-i ~/.ssh/id_com8 -o IdentitiesOnly=yes %(me)s true 2>/dev/null '
        '&& echo "REV=1" || echo "REV=0"'
        % {"p": _FAR_PATH, "kf": kf, "me": shlex.quote(my_addr),
           "hubk": ('grep -q %s ~/.ssh/authorized_keys 2>/dev/null '
                    '&& echo "HUBKEY=1" || echo "HUBKEY=0"; '
                    % shlex.quote(hub_key_material))
           if hub_key_material else 'echo "HUBKEY=1"; '}
    )


def parse_facts(out):
    """KEY=VALUE lines -> facts dict. Banners and junk are ignored; a
    missing key gets a safe default (absent/false)."""
    kv = {}
    for line in (out or "").splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k.isupper() and k.replace("_", "").isalnum():
            kv[k] = v.strip()
    owner, uid = kv.get("CCOWNER", ""), kv.get("UID_", "")
    import re as _re
    ip = kv.get("SSHIP", "")
    if not _re.match(r"[0-9A-Fa-f:.]+\Z", ip):
        ip = ""   # an address, or nothing — never shell metacharacters
    return {
        "os": kv.get("OS", ""),
        # Termux/Android is its own device class, not "a Linux": no systemd
        # runtime dir, an unwritable /tmp, arm64, no MagicDNS.
        "is_termux": (kv.get("UNAME_O", "") == "Android"
                      or "com.termux" in kv.get("PREFIX", "")),
        "com8_self": kv.get("COM8_SELF_", ""),
        "com8_self_files": [f for f in kv.get("COM8_SELF_FILES", "").split()
                            if f],
        "com8_self_value": kv.get("COM8_SELF_VALUE", ""),
        "home": kv.get("HOMEDIR", ""),
        "login_shell": kv.get("SHELL_", ""),
        "xdg": kv.get("XDG", ""),
        "ssh_ip": ip,
        "cc_collision": bool(owner) and bool(uid) and owner != uid,
        "own_key": kv.get("OWNKEY") == "1",
        "fabric_key": kv.get("COM8KEY") == "1",
        "hub_key_there": kv.get("HUBKEY") == "1",
        "py3": kv.get("PY3") == "1",
        "tmux_bin": kv.get("TMUX_BIN", ""),
        "claude_bin": kv.get("CLAUDE_BIN", ""),
        "shim": kv.get("SHIM") == "1",
        "kernel_hash": kv.get("KHASH", ""),
        "reverse_ok": kv.get("REV") == "1",
    }


# ----------------------------------------------------------------- plan

def parse_args(argv):
    """(addr, spawn_name, error) — strict: exactly one positional, --spawn
    requires a value, anything else is an error (a typo must never silently
    retarget another device)."""
    addr = spawn = None
    it = iter(argv)
    for a in it:
        if a == "--spawn":
            spawn = next(it, None)
            if not spawn or spawn.startswith("-"):
                return None, None, "--spawn requires an agent name"
        elif a.startswith("-"):
            return None, None, "unknown flag %r" % a
        elif addr is None:
            addr = a
        else:
            return None, None, "unexpected extra argument %r" % a
    if not addr:
        return None, None, "usage: com8 adopt <user@host> [--spawn <name>]"
    return addr, spawn, None


FABRIC_KEY = "~/.ssh/id_com8"

# The newest claude build that ships a runnable Android binary; the native
# installer's latest does not. Bump only after testing ON a phone.
ANDROID_CLAUDE_PIN = "2.1.72"


def fabric_keygen_cmd():
    """Generate the fabric's OWN key — dedicated and passphrase-free.

    Never reuse the operator's personal key: it is commonly passphrase-
    protected (studio-2, live), and a locked key makes the far daemon's
    non-interactive reverse dial fail with a misleading 'Permission denied'
    even though sshd accepted the key. A dedicated key also means the hub
    authorizes exactly one, tagged, revocable credential per device."""
    return ("[ -f %s ] || ssh-keygen -t ed25519 -N '' -q -f %s "
            "-C com8-$(hostname -s) </dev/null" % (FABRIC_KEY, FABRIC_KEY))


def _hub_ipv4s():
    """The hub's own non-loopback IPv4 addresses, for reverse-dial
    candidates. Best-effort; empty on any platform where ifconfig differs."""
    import subprocess as _sp
    try:
        out = _sp.run(["ifconfig"], capture_output=True, text=True,
                      timeout=6).stdout
    except Exception:
        return []
    ips = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("inet ") and not line.startswith("inet 127."):
            ip = line.split()[1]
            if ip.count(".") == 3:
                ips.append(ip)
    return ips


def hub_reverse_candidates(ssh_ip, hub_ips):
    """Ordered, de-duplicated reverse-dial addresses: the connection's own
    source IP first (usually same-network and correct), then the hub's other
    addresses. Bridge/virtual ranges (192.168., 10., 172.16-31.) go last —
    they rarely route from another machine."""
    def rank(ip):
        return 1 if (ip.startswith("192.168.") or ip.startswith("10.")
                     or ip.startswith("172.")) else 0
    seen, out = set(), []
    for ip in ([ssh_ip] if ssh_ip else []) + list(hub_ips):
        if ip and ip not in seen:
            seen.add(ip)
            out.append(ip)
    return sorted(out, key=rank)


def _profile_files(login_shell):
    sh = os.path.basename(login_shell or "")
    if sh == "zsh":
        return ["~/.zshenv"]
    if sh == "bash":
        return ["~/.bash_profile", "~/.bashrc"]
    return ["~/.profile"]


def plan(facts, local):
    """facts (probed) + local {kernel_hash, my_addr} -> (actions, checklist).
    Pure. Ordering: keys -> alias -> runtime dir -> shim -> kernel."""
    acts, checklist = [], []

    if not facts.get("fabric_key"):
        acts.append({"step": "gen_fabric_key"})
    if not facts.get("reverse_ok"):
        # The far side must dial the hub with its own key: authorize it here
        # (dedup-checked on apply), and give it an address that actually
        # routes back — chosen from candidates at execute time.
        acts.append({"step": "authorize_key_here"})
        cands = local.get("reverse_candidates") or []
        if cands:
            acts.append({"step": "reverse_alias", "candidates": cands})
        else:
            checklist.append(
                "reverse path: no hub address to offer — on the device, make "
                "`ssh %s` work (alias or DNS), then re-run adopt"
                % local.get("my_addr"))

    steer = bool(facts.get("cc_collision")) or (
        facts.get("os") != "Darwin" and not facts.get("xdg"))
    if not facts.get("hub_key_there", True):
        # The MIRROR of the device's fabric key: a hub daemon started by
        # launchd/systemd has NO ssh agent, so its outbound dial must rest on
        # the hub's own passphrase-free fleet key — which this device has to
        # trust. Installed now, over the channel the operator already holds.
        acts.append({"step": "authorize_hub_key_there"})

    provisioned_rt = False
    if steer:
        # Move BOTH the daemon (pair steers its plist/unit to the same
        # ~/.local/run/cc-socks) and claude off the unusable default.
        acts.append({"step": "runtime_dir",
                     "profiles": _profile_files(facts.get("login_shell")),
                     "reason": ("collision" if facts.get("cc_collision")
                                else "no-systemd-runtime")})
        provisioned_rt = True

    if not facts.get("shim"):
        acts.append({"step": "shim"})

    termux = bool(facts.get("is_termux"))
    if termux:
        # claude falls back to a HARDCODED /tmp when TMPDIR is unset. On
        # Android /tmp is owned by shell:shell, and an app UID cannot write
        # there even at 0777 — SELinux domain restriction, not Unix modes
        # (verified by creating the dir as shell via Shizuku and still
        # getting EACCES). So TMPDIR must be exported, durably: without it
        # the on-device agent silently loses the ability to run ANY command
        # and reports it as a /tmp permission error the human cannot fix
        # from the shell. Deleting this export is not a cleanup.
        acts.append({"step": "tmpdir",
                     "profiles": _profile_files(facts.get("login_shell"))})
        # The login shell's own answer is the authority: it is definitionally
        # what a shell on that device sees, resolved past quoting, command
        # substitution and reassignment. The FILE list is kept only as a
        # diagnostic, for the case where an export exists but in a profile
        # this shell never reads (bash reads .bash_profile and stops — a
        # live case: the export sat in .bashrc and .profile, no shell on the
        # phone had it, and the daemon worked only because it was set by
        # hand at launch).
        name = (facts.get("com8_self_value") or facts.get("com8_self") or "")
        if not name:
            mine = set(os.path.basename(f) for f in
                       _profile_files(facts.get("login_shell")))
            stray = [f for f in (facts.get("com8_self_files") or [])
                     if f not in mine]
            # NEVER invent a name: a wrong one does not fail, it FORKS the
            # identity — mail routes to a name nobody listens on while every
            # layer reports success.
            if stray:
                checklist.append(
                    "COM8_SELF is exported in %s, but this device's login "
                    "shell (%s) does not read that file — so the daemon I "
                    "just started has no fabric identity. Move the export "
                    "into %s and re-run adopt"
                    % (", ".join(stray),
                       os.path.basename(facts.get("login_shell") or "sh"),
                       ", ".join(sorted(mine))))
            else:
                checklist.append(
                    "COM8_SELF is not configured and Android has no MagicDNS "
                    "to derive it, so the daemon I just started has no fabric "
                    "identity — export COM8_SELF=<its fabric name> in %s, "
                    "then re-run adopt" % ", ".join(sorted(mine)))

    if not facts.get("tmux_bin"):
        if facts.get("os") == "Darwin":
            checklist.append("tmux missing and no static build for macOS — "
                             "install it (brew install tmux), then re-run")
        elif termux:
            # The static build we fetch is linux-amd64; a phone is arm64,
            # and Termux ships tmux in its own repo anyway.
            checklist.append("tmux missing — on the device run: "
                             "pkg install tmux")
        else:
            acts.append({"step": "install_tmux_static"})
    if not facts.get("claude_bin"):
        # claude.ai/install.sh publishes no linux-arm64-android binary, so
        # Android is pinned to the last build that runs there.
        acts.append({"step": "install_claude",
                     "pin": ANDROID_CLAUDE_PIN if termux else None})

    far, mine = facts.get("kernel_hash"), local.get("kernel_hash")
    need_restart = False
    if far and mine and far != mine:
        # A staged-but-stale kernel: refresh bytes AND restart the daemon
        # that loaded the old ones. An ABSENT kernel is pair's own step.
        acts.append({"step": "kernel_refresh"})
        need_restart = True
    if provisioned_rt:
        # The daemon must resolve the SAME cc-socks dir claude will use —
        # a daemon without the env delivers into /tmp/cc-socks while claude
        # listens in ~/.local/run/cc-socks (the split-brain, proven live).
        need_restart = True
    if need_restart:
        # Profiles do not reach a non-interactive daemon start — the same
        # reason XDG_RUNTIME_DIR is inlined. So the identity rides with it,
        # read back from the one source of truth rather than taken as a
        # second input that could disagree with the device.
        _self = (facts.get("com8_self_value") or facts.get("com8_self") or "")
        # `env`: this device was moved off the default cc-socks, so its
        # daemon must be restarted WITH that runtime dir — a profile is not
        # enough (non-interactive ssh sources none on macOS, and a no-systemd
        # Linux like Termux has no runtime dir at all).
        acts.append({"step": "restart_daemon",
                     "env": provisioned_rt,
                     "com8_self": _self})         # always LAST: post-pair

    return acts, checklist


# ------------------------------------------------------------ executors

def runtime_profile_lines():
    """Profile text for the per-user runtime dir. ${XDG_RUNTIME_DIR:-...}
    preserves a systemd-provided value — never shadow the real one."""
    return ('export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-$HOME/.local/run}"\n'
            '[ -d "$XDG_RUNTIME_DIR" ] || mkdir -p "$XDG_RUNTIME_DIR"\n'
            'chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null\n')


def settings_file_cmd():
    """Shell line writing ~/.com8-settings.json — printf-encoded so the
    JSON never meets nested shell quoting."""
    return (r'printf "{\"crossSessionInbound\":\"accept\"}\n" '
            r'> "$HOME/.com8-settings.json"')


def spawn_cmds(name, facts, steered=False):
    """Remote commands that spawn agent NAME in tmux session com8-NAME.
    Interactive shell + send-keys (claude needs a real pty; a launcher
    script as the pane command died twice). Returned as a list of remote
    sh lines to run in order, with waits handled by the caller. `steered`:
    the device was moved to ~/.local/run (collision / no-systemd), so claude
    must bind its socket there too — otherwise it keeps the OS default that
    its daemon also uses."""
    tmux = facts.get("tmux_bin") or "tmux"
    env = ('export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-$HOME/.local/run}"; '
           if steered else "")
    return [
        settings_file_cmd(),
        "%s kill-session -t com8-%s 2>/dev/null; "
        "%s new-session -d -s com8-%s -x 200 -y 50" % (tmux, name, tmux, name),
        "%s send-keys -t com8-%s \"%sexport PATH=%s; "
        "cd ~ && claude --settings ~/.com8-settings.json\" Enter"
        % (tmux, name, env, _FAR_PATH.replace("$", "\\$")),
        "%s send-keys -t com8-%s Enter" % (tmux, name),          # trust prompt
        "%s send-keys -t com8-%s \"/rename %s\" Enter" % (tmux, name, name),
    ]


def _run_ssh(addr, cmd, timeout=60):
    """(rc, output). NEVER raises: a device that hangs (a pending Tailscale
    SSH check, a wedged sshd) must produce an honest report, not a traceback
    out of the operator's tool."""
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes",
                            "-o", "ConnectTimeout=10", addr, cmd],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "ssh timed out after %ss (device hung or unreachable)" % timeout
    except OSError as e:
        return 125, "ssh could not run: %s" % e


def hub_missing_files(here_dir):
    """Kernel files absent from the hub itself — an empty list is a healthy
    hub. A non-empty one means the hub can neither deploy nor honestly
    hash-compare those files."""
    return [f for f in KERNEL_FILES
            if not os.path.exists(os.path.join(here_dir, f))]


def _local_kernel_hash(here_dir):
    # Name-tagged so file PRESENCE is part of the identity: a file missing
    # here vs present there changes the hash (asymmetric mismatch -> refresh).
    # Hub completeness is guarded separately (hub_missing_files) so the
    # symmetric both-missing case can't silently mask an incomplete deploy.
    import hashlib
    h = hashlib.md5()
    for f in KERNEL_FILES:
        try:
            with open(os.path.join(here_dir, f), "rb") as fh:
                data = fh.read()
        except OSError:
            data = None
        h.update(("\0%s\0" % f).encode())
        h.update(b"ABSENT" if data is None else data)
    return h.hexdigest()


_PUBKEY_RE = None


def _authorize_here(pubkey, tag):
    """Append a far device's pubkey to ~/.ssh/authorized_keys. The fetch
    output is UNTRUSTED (a compromised device could answer anything): only
    the FIRST line is considered, it must be exactly `type material
    [comment]` with a plausible key type and base64 material — never an
    options prefix, never extra lines. Deduped on the exact material token.
    Exactly one clean line is ever written; anything else returns False."""
    import re as _re
    global _PUBKEY_RE
    if _PUBKEY_RE is None:
        _PUBKEY_RE = _re.compile(
            r"(ssh-(?:ed25519|rsa|dss)|ecdsa-sha2-[a-z0-9-]+|"
            r"sk-[a-z0-9@.-]+)\s+([A-Za-z0-9+/=]{16,})(?:\s+\S.*)?\Z")
    stripped = (pubkey or "").strip()
    first = stripped.splitlines()[0].strip() if stripped else ""
    m = _PUBKEY_RE.match(first)
    if not m:
        return False
    ktype, material = m.group(1), m.group(2)
    ak = os.path.expanduser("~/.ssh/authorized_keys")
    try:
        lines = open(ak).read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        if material in line.split():
            return True
    with open(ak, "a") as f:
        f.write("%s %s %s\n" % (ktype, material, tag))
    os.chmod(ak, 0o600)
    return True


def execute(addr, acts, facts, local, ssh=_run_ssh, say=print):
    """Run the planned actions. Each prints one honest line. Returns extra
    checklist items discovered during execution (e.g. no reverse address
    verified)."""
    devtag = "adopt-%s" % addr.split("@")[-1].split(".")[0]
    extra = []
    for a in acts:
        step = a["step"]
        if step == "gen_fabric_key":
            rc, _ = ssh(addr, "mkdir -p ~/.ssh && chmod 700 ~/.ssh; " +
                        fabric_keygen_cmd())
            say("  provision: fabric key    %s"
                % ("%s generated (no passphrase)" % FABRIC_KEY if rc == 0
                   else "KEYGEN FAILED"))
        elif step == "authorize_key_here":
            rc, out = ssh(addr, "cat %s.pub" % FABRIC_KEY)
            done = rc == 0 and _authorize_here(out, devtag)
            say("  provision: reverse key   %s" %
                ("authorized on this hub" if done else "FAILED to fetch"))
        elif step == "authorize_hub_key_there":
            pub = (local.get("hub_pubkey") or "").strip()
            parts = pub.split()
            if len(parts) >= 2 and parts[0].startswith(("ssh-", "ecdsa-")):
                rc, _ = ssh(addr,
                            "mkdir -p ~/.ssh && chmod 700 ~/.ssh; "
                            "touch ~/.ssh/authorized_keys; "
                            "grep -q %s ~/.ssh/authorized_keys || "
                            "printf '%%s %%s com8-hub\\n' %s %s "
                            ">> ~/.ssh/authorized_keys; "
                            "chmod 600 ~/.ssh/authorized_keys"
                            % (shlex.quote(parts[1]), shlex.quote(parts[0]),
                               shlex.quote(parts[1])))
                say("  provision: hub key       %s"
                    % ("trusted by this device (agent-free dialing)"
                       if rc == 0 else "INSTALL FAILED"))
            else:
                say("  provision: hub key       SKIPPED (no hub fleet key)")
        elif step == "reverse_alias":
            host = local["my_addr"].split("@")[-1]
            user = local["my_addr"].split("@")[0]
            winner = None
            for ip in a["candidates"]:
                rc, _ = ssh(addr, "ssh -o BatchMode=yes -o ConnectTimeout=6 "
                            "-i %s -o IdentitiesOnly=yes "
                            "-o StrictHostKeyChecking=accept-new %s true"
                            % (FABRIC_KEY,
                               shlex.quote("%s@%s" % (user, ip))))
                if rc == 0:
                    winner = ip
                    break
            if winner:
                # IdentityFile + IdentitiesOnly: the daemon's dial uses the
                # fabric key ONLY — a passphrase-locked personal key can
                # neither shadow it nor break BatchMode signing.
                ssh(addr, "mkdir -p ~/.ssh; chmod 700 ~/.ssh; "
                          "grep -q 'Host %s' ~/.ssh/config 2>/dev/null && "
                          "sed -i.bak '/Host %s/,+4d' ~/.ssh/config; "
                          "printf '\\nHost %s\\n  HostName %s\\n  User %s\\n"
                          "  IdentityFile %s\\n  IdentitiesOnly yes\\n' "
                          ">> ~/.ssh/config; chmod 600 ~/.ssh/config"
                          % (host, host, host, winner, user, FABRIC_KEY))
                say("  provision: dial alias    %s -> %s (verified)"
                    % (host, winner))
            else:
                extra.append("reverse path: none of the hub addresses (%s) "
                             "reach back from this device — run, ON %s: "
                             "ssh-copy-id %s, or add a working Host alias"
                             % (", ".join(a["candidates"]), addr,
                                local["my_addr"]))
                say("  provision: dial alias    NONE of %d candidates verified"
                    % len(a["candidates"]))
        elif step == "runtime_dir":
            payload = runtime_profile_lines().replace("\n", "\\n")
            rcs = []
            for prof in a["profiles"]:
                rc, _ = ssh(addr, "grep -q XDG_RUNTIME_DIR %s 2>/dev/null || "
                            "printf '%s' >> %s" % (prof, payload, prof))
                rcs.append(rc)
            rc2, _ = ssh(addr, "mkdir -p ~/.local/run && chmod 700 ~/.local/run")
            rcs.append(rc2)
            ok = all(r == 0 for r in rcs)
            say("  provision: runtime dir   %s (%s)"
                % ("~/.local/run" if ok else "FAILED", ", ".join(a["profiles"])))
        elif step == "tmpdir":
            payload = ('export TMPDIR="${TMPDIR:-$HOME/tmp}"\n'
                       '[ -d "$TMPDIR" ] || mkdir -p "$TMPDIR"\n'
                       ).replace("\n", "\\n")
            rcs = []
            for prof in a["profiles"]:
                rc, _ = ssh(addr, "grep -q TMPDIR %s 2>/dev/null || "
                            "printf '%s' >> %s" % (prof, payload, prof))
                rcs.append(rc)
            rc2, _ = ssh(addr, "mkdir -p ~/tmp")
            rcs.append(rc2)
            say("  provision: TMPDIR        %s (%s)"
                % ("~/tmp" if all(r == 0 for r in rcs) else "FAILED",
                   ", ".join(a["profiles"])))
        elif step == "shim":
            rc, _ = ssh(addr, "mkdir -p ~/.local/bin; "
                        "printf '#!/bin/sh\\n[ \"$1\" = com8 ] && shift\\n"
                        "exec python3 \"$HOME/.local/share/com8/daemon/current/"
                        "com8.py\" call \"$@\"\\n' > ~/.local/bin/communicate; "
                        "chmod +x ~/.local/bin/communicate; "
                        "ln -sf ~/.local/bin/communicate ~/.local/bin/com8")
            say("  provision: CLI shim      %s"
                % ("~/.local/bin/{communicate,com8}" if rc == 0 else "FAILED"))
        elif step == "install_tmux_static":
            rc, _ = ssh(addr, "mkdir -p ~/.local/bin && curl -fsSL -o /tmp/tmux.gz "
                        "https://github.com/mjakob-gh/build-static-tmux/releases/"
                        "latest/download/tmux.linux-amd64.gz && gunzip -f /tmp/tmux.gz "
                        "&& mv /tmp/tmux ~/.local/bin/tmux && chmod +x ~/.local/bin/tmux "
                        "&& ~/.local/bin/tmux -V", timeout=180)
            say("  provision: tmux          %s"
                % ("static build installed" if rc == 0 else "INSTALL FAILED"))
        elif step == "install_claude":
            pin = a.get("pin")
            cmd = ("curl -fsSL https://claude.ai/install.sh | bash -s %s"
                   % pin) if pin else "curl -fsSL https://claude.ai/install.sh | bash"
            rc, _ = ssh(addr, "%s >/dev/null 2>&1; PATH=%s command -v claude"
                        % (cmd, _FAR_PATH), timeout=300)
            say("  provision: claude        %s%s"
                % ("installed" if rc == 0 else "INSTALL FAILED",
                   (" (pinned %s)" % pin) if pin else ""))
        elif step == "kernel_refresh":
            here_dir = local["here_dir"]
            files = [os.path.join(here_dir, f) for f in KERNEL_FILES
                     if os.path.exists(os.path.join(here_dir, f))]
            r = subprocess.run(["scp", "-q", "-o", "BatchMode=yes"] + files +
                               ["%s:.local/share/com8/daemon/current/" % addr],
                               capture_output=True, text=True, timeout=120)
            say("  provision: kernel        %s"
                % ("refreshed (hash mismatch)" if r.returncode == 0
                   else "REFRESH FAILED"))
        elif step == "restart_daemon":
            # Bake the runtime dir into the command whenever this device was
            # steered. A profile is not enough: macOS non-interactive ssh
            # sources none, and on a no-systemd Linux (Termux/Android) the
            # unsteered daemon dies outright on /tmp/cc-socks (PermissionError
            # — /tmp is not writable there). Keying this off the OS instead of
            # the plan's own decision is what broke that case.
            env = ('XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-$HOME/.local/run}" '
                   if (a.get("env") or facts.get("os") == "Darwin") else "")
            _self = a.get("com8_self") or ""
            if _self:
                env += "COM8_SELF=%s " % shlex.quote(_self)
            rc, out = ssh(addr,
                          "pkill -f 'com8.py daemon' 2>/dev/null; sleep 1; "
                          "%snohup python3 "
                          "~/.local/share/com8/daemon/current/com8.py daemon "
                          ">> ~/.local/state/communicate/com8/daemon.log 2>&1 "
                          "& sleep 2; pgrep -f 'com8.py daemon' | head -1"
                          % env)
            say("  provision: daemon        %s"
                % ("restarted on the new kernel" if (rc == 0 and out.strip())
                   else "RESTART UNCONFIRMED"))
    return extra


def spawn(addr, name, facts, fardev, ssh=_run_ssh, say=print, ask=None,
          steered=False):
    """Spawn agent NAME on the device and prove it end to end."""
    cmds = spawn_cmds(name, facts, steered=steered)
    ssh(addr, cmds[0])
    ssh(addr, cmds[1])
    ssh(addr, cmds[2])
    time.sleep(15)
    ssh(addr, cmds[3])            # trust prompt: accept
    time.sleep(12)
    ssh(addr, cmds[4])            # /rename
    time.sleep(8)
    rc, out = ssh(addr,
                  "python3 -c \"import glob,json,os\n"
                  "for f in glob.glob(os.path.expanduser('~/.claude/sessions/*.json')):\n"
                  "    try: d=json.load(open(f))\n"
                  "    except Exception: continue\n"
                  "    if d.get('name')=='%s' and d.get('version')!='communicate-com8':\n"
                  "        print('REG', d.get('pid')); break\"" % name)
    registered = "REG" in out
    say("  spawn: %-18s %s" % (name, "registered" if registered
                               else "NOT REGISTERED — check the pane"))
    if registered and ask:
        okd, reply = ask("%s@%s" % (name, fardev),
                         "reply exactly: %s-adopted" % name)
        say("  spawn: round trip        %s"
            % (("verified (%s)" % reply) if okd else "no reply yet — "
               "if claude needs /login it will answer after"))
        return okd
    return registered
