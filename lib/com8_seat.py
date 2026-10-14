#!/usr/bin/env python3
"""com8_seat — the seat plane: drive interactive surfaces (tmux panes) as a
first-class peer of the message plane.

A *seat* is an occupied, drivable surface — a tmux pane running a shell, an
agent CLI, or an ssh session into a cluster. Seats are the explicit escape
hatch for things you cannot mailbox (HPC login nodes, REPLs, TUIs). This module
is a CLEAN-ROOM reimplementation of the anu `pane` bin's two hard-won
disciplines, ported from a behavioral study of that tool, not its code:

  * the state classifier — dead > approval > busy > booting > idle, using the
    signals that actually survive answer-streaming (the CLI title spinner
    animates while working; claude reports its version as pane_current_command,
    codex reports "codex"; the idle footer and the "esc to interrupt" hint);
  * the send discipline — sanitize, stage literally with `send-keys -l`, submit
    with a SEPARATE retried Enter (a welded C-m gets absorbed into a paste and
    never submits; re-sending the TEXT could duplicate a submitted prompt, so we
    never do), and verify by the composer's `[Pasted Content` marker, returning
    an explicit "unconfirmed" instead of lying about delivery.

tmux is the source of truth for a seat's existence and screen; the daemon keeps
only light metadata (spawn cmd, bound identity). All state is queried live.
Stdlib only. Works against the ambient tmux server, or a throwaway one via
$COM8_TMUX_SOCKET (tests).
"""
import os
import re
import shlex
import subprocess
import time

# Jittered sampling intervals defeat a fixed-period spinner aliasing to "static".
_JITTER = (0.11, 0.17, 0.23)
_SANITIZE = {ord(c): None for c in
             "".join(chr(i) for i in range(0, 32)) + chr(127)}
_PASTE_MARKER = "[Pasted Content"
# An agent CLI reports either "codex" or its own version string as the command.
_VERSION_CMD = re.compile(r"^\d+\.\d+")
_SHELLS = {"bash", "zsh", "sh", "fish", "dash"}
# Transport commands whose pane_current_command stays constant whether the far
# end is idle or working — for these, fall back to screen-change like a shell.
_TRANSPORTS = {"ssh", "mosh", "et", "sshpass", "sftp"}
# Busy: the interrupt hint the CLI shows while it works (may be overwritten
# by streamed text mid-answer — hence the animation check as a second signal).
_BUSY_HINT = re.compile(r"esc to interrupt|to interrupt\)|interrupt\b", re.I)
# Approval: a permission ASK and a confirm/choice AFFORDANCE must BOTH be
# present (a lone "yes" in prose is not an approval prompt).
_PERM_ASK = re.compile(
    r"do you want|would you like|allow this|proceed\?|permission to|"
    r"grant access|approve|confirm|\(y/n\)|\[y/n\]|"
    r"held message|deliver this message|drop it and tell the sender", re.I)
_AFFORD = re.compile(r"❯\s*\d|›\s*\d|^\s*\d[.)]\s|\(y/n\)|\[y/n\]|\by/n\b"
                     r"|^\s*[❯›]\s+\S", re.M)  # bare highlighted row (unnumbered menus)
# Choosing a menu row. An "allow" must be the plain, one-shot affirmative: never a row that would
# widen permissions beyond this one action. Claude Code 2.1.278 offers "Yes, and switch to accept
# edits (auto-approve file edits ... for this session)" as option 2 of a Write prompt; the older
# skip list (always / don't ask / all future) let it through, and only row order kept respond()
# from approving the whole session. Measured 2026-09-22 (scripts/fixtures/seat-claude-write-prompt.txt).
_AFF = re.compile(r"yes|allow|approve|proceed|deliver|accept|ok\b", re.I)
_STICKY = re.compile(r"always|don'?t ask|all future|auto-approve|accept edits|for this session"
                     r"|switch to|remember|from now on|every time", re.I)
_NEG = re.compile(r"\bno\b|deny|decline|drop|reject|cancel", re.I)


def choose_option(menu, decision="allow"):
    """Pure: pick the row to drive for `decision`, or None (fail closed) when no row is
    unambiguously right. allow = affirmative, not negative, not sticky/session-wide.
    deny = negative, not affirmative."""
    if decision in ("deny", "no", "reject"):
        return next((r for r in menu if _NEG.search(r["text"]) and not _AFF.search(r["text"])), None)
    return next((r for r in menu if _AFF.search(r["text"]) and not _STICKY.search(r["text"])
                 and not _NEG.search(r["text"])), None)
# Secret shapes to mask on read unless --raw.
_SECRETS = [
    # Distinctive credential prefixes — matched anywhere (a leading word char,
    # e.g. `tok_sk-…`, must not defeat masking).
    re.compile(r"(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|"
               r"xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{12,})"),
    re.compile(r"(?i)(bearer|authorization:|token=|api[_-]?key=?)\s*"
               r"([A-Za-z0-9._~+/=-]{12,})"),
]


class SeatError(Exception):
    """tmux answered, and the answer was an error about this seat."""


class SeatUnavailable(SeatError):
    """We could not ASK tmux at all — it timed out, or is not there. This is
    the absence of a measurement, not a measurement of absence: a caller may
    report "unknown", never "dead". (Subclasses SeatError so every existing
    handler still catches it.)"""


class SeatDriver:
    def __init__(self, session=None, log=None, *, socket_path=None, ambient=False):
        self.session = session or os.environ.get("COM8_SEAT_SESSION", "com8-seats")
        self.log = log or (lambda *a: None)
        # A dedicated named tmux server ("com8") by default, so seats are stable
        # regardless of the daemon's TMPDIR under launchd; COM8_TMUX_SOCKET="" opts
        # into the ambient server (visible in the user's own tmux).
        sock = os.environ.get("COM8_TMUX_SOCKET")
        if sock is None:
            sock = "com8"
        self._base = (["tmux", "-S", socket_path] if socket_path else
                      ["tmux"] if ambient else
                      ["tmux"] + (["-L", sock] if sock else []))

    # -- tmux plumbing ---------------------------------------------------------
    def _tmux(self, *args, check=False, timeout=10):
        try:
            r = subprocess.run(self._base + list(args), capture_output=True,
                               text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as e:
            # Distinguishable on purpose: nothing here says anything about the
            # SEAT, only about our ability to reach tmux.
            raise SeatUnavailable("tmux did not answer: %s" % e)
        if check and r.returncode != 0:
            raise SeatError((r.stderr or "tmux error").strip())
        return r

    def available(self):
        return self._tmux("has-server").returncode == 0 or True  # -L may need init

    def _pane_exists(self, seat):
        r = self._tmux("display", "-p", "-t", seat, "#{pane_id}")
        return r.returncode == 0 and r.stdout.strip() == seat

    def _field(self, seat, fmt):
        r = self._tmux("display", "-p", "-t", seat, fmt)
        return r.stdout.rstrip("\n") if r.returncode == 0 else ""

    def server(self, observe=True):
        """Report our selection and its observed server, without starting one.

        Pane numbers are only meaningful on this server during its lifetime.
        Missing or unreachable servers have no invented socket/PID observation.
        """
        result = {"argv": list(self._base), "socket_path": None, "pid": None}
        if not observe:
            return result
        try:
            response = self._tmux("display-message", "-p", "#{socket_path}\t#{pid}")
            fields = response.stdout.strip().split("\t")
            if (response.returncode == 0 and len(fields) == 2
                    and os.path.isabs(fields[0]) and fields[1].isdigit()
                    and int(fields[1]) > 0):
                result.update(socket_path=fields[0], pid=int(fields[1]))
        except SeatUnavailable:
            pass
        return result

    def _missing(self, seat):
        return SeatError("no such seat: %s on configured server %s; "
                         "inspect com8 seat ls --json" % (seat, shlex.join(self._base)))

    def _ensure_session(self):
        if self._tmux("has-session", "-t", self.session).returncode != 0:
            self._tmux("new-session", "-d", "-s", self.session,
                       "-x", "220", "-y", "50", check=True)

    # -- capture ---------------------------------------------------------------
    def _capture(self, seat, lines=None):
        r = self._tmux("capture-pane", "-p", "-t", seat)
        if r.returncode != 0:
            return []
        out = r.stdout.split("\n")
        while out and out[-1] == "":
            out.pop()
        return out[-lines:] if lines else out

    @staticmethod
    def _redact(text):
        for rx in _SECRETS:
            text = rx.sub(lambda m: m.group(0)[:4] + "…REDACTED", text)
        return text

    # -- classifier: dead > approval > busy > booting > idle -------------------
    @staticmethod
    def _cmd_is_agent(cmd):
        """An agent CLI treats typed input as a PROMPT (the agent's own
        autonomy governs what it does with it); a shell/REPL/transport treats
        it as a COMMAND. The seat relay types mail only into the former."""
        return (cmd == "codex" or bool(_VERSION_CMD.match(cmd or ""))
                or cmd in ("node", "claude"))

    def is_agent_seat(self, seat):
        """True only if the pane's foreground is an agent CLI. The mail→seat
        relay gates on this: typing into a bare shell would turn mail-send
        into command execution. A dead/unreachable pane is not an agent."""
        if not self._pane_exists(seat):
            return False
        try:
            return self._cmd_is_agent(self._field(seat, "#{pane_current_command}"))
        except Exception:
            return False

    def state(self, seat):
        if not self._pane_exists(seat):
            return "dead"
        cmd = self._field(seat, "#{pane_current_command}")
        bottom = "\n".join(self._capture(seat, 14))
        if _PERM_ASK.search(bottom) and _AFFORD.search(bottom):
            return "approval"
        is_agent = self._cmd_is_agent(cmd)
        if _BUSY_HINT.search(bottom):
            return "busy"
        if is_agent:
            # Agent CLI: the title spinner is the one signal that survives
            # answer-streaming. Animation => busy; frozen frame => idle.
            if self._title_animates(seat):
                return "busy"
            if not self._has_idle_footer(bottom) and self._is_booting(seat):
                return "booting"
            return "idle"
        # A shell/transport seat: pane_current_command tells us directly whether
        # a foreground process is running (bash → the shell is idle; `sleep`,
        # `make`, a build → busy). ssh/mosh keep a constant command whether the
        # far end is idle or working, so for those fall back to screen-change.
        if cmd in _SHELLS or cmd in _TRANSPORTS or cmd in ("", "tmux"):
            return "busy" if self._screen_changing(seat) else "idle"
        return "busy"  # some foreground process holds the pane

    def _title(self, seat):
        return self._field(seat, "#{pane_title}")

    def _title_animates(self, seat):
        """Two jittered samples of the leading title glyph; a change => the
        spinner is animating => busy. Claude freezes the last frame at idle, so
        a single sample cannot tell busy-frozen from idle — animation can."""
        first = self._title(seat)[:4]
        for j in _JITTER[:2]:
            time.sleep(j)
            if self._title(seat)[:4] != first:
                return True
        return False

    def _screen_changing(self, seat):
        a = "\n".join(self._capture(seat, 6))
        time.sleep(_JITTER[1])
        return "\n".join(self._capture(seat, 6)) != a

    @staticmethod
    def _has_idle_footer(bottom):
        return ("bypass permissions" in bottom or "new task?" in bottom
                or "shift+tab to cycle" in bottom)

    def _is_booting(self, seat):
        # Very young pane with no prompt/footer yet, or a resume/startup banner.
        head = "\n".join(self._capture(seat, 20))
        return ("Welcome to" in head or "· resume" in self._title(seat)
                or "Choose" in head and "resume" in head.lower())

    # -- surface: the seat, measured (never asserted) --------------------------
    def measure(self, seat):
        """The surface axis, measured. Never assert a handle you have not just
        checked -- after a reboot the tmux server is gone and every stored pane
        id is a lie."""
        if not seat:
            return None
        try:
            st = self.state(seat)
        except SeatUnavailable as e:
            # tmux timed out or is not installed. We learned NOTHING about the
            # seat -- publishing "dead" here would advertise what we never
            # measured, and would make callers act on it (restart kills a seat
            # it believes is gone).
            st = "unknown"
            self.log("measure could not reach tmux for", seat, e)
        except SeatError as e:
            st = "dead"
            self.log("measure failed for", seat, e)
        return {"driver": "tmux", "handle": seat, "state": st,
                "measured_at": time.time(),
                "tmux_server": self.server(observe=st not in ("dead", "unknown"))}

    # -- send discipline -------------------------------------------------------
    def send(self, seat, text):
        if not self._pane_exists(seat):
            raise self._missing(seat)
        # Flatten newlines/tabs to spaces and strip control bytes, so the
        # payload cannot drive the target's terminal.
        msg = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
        msg = msg.translate(_SANITIZE)
        # exit copy-mode if the pane is scrolled back
        if self._field(seat, "#{pane_in_mode}") == "1":
            self._tmux("send-keys", "-t", seat, "-X", "cancel")
        self._tmux("send-keys", "-t", seat, "-l", "--", msg, check=True)
        time.sleep(0.3)
        # Submit with a SEPARATE Enter, retried with growing delays; verify by
        # the composer actually CLEARING (neither the paste marker nor the
        # staged text still visible on the prompt line — a booting CLI can eat
        # an early Enter). Never re-send the text (would duplicate a prompt).
        probe = msg[:60]
        for attempt in range(5):
            self._tmux("send-keys", "-t", seat, "Enter")
            time.sleep(0.4 + 0.4 * attempt)
            if self._has_paste_marker(seat):
                continue
            if not self._composer_holds(seat, probe):
                return {"ok": True, "sent": True}
        # Still staged: report unconfirmed rather than claim success.
        return {"ok": False, "sent": True, "confirmed": False,
                "err": "staged but not submitted (composer still holds the text)"}

    def _composer_holds(self, seat, probe):
        """True if the staged text still sits on a prompt line near the bottom
        (an unsubmitted composer). Echoed history lines don't match: we only
        look at the last few rows and require a prompt glyph before the text."""
        if not probe:
            return False
        tail = self._capture(seat, 8)
        for ln in tail:
            s = ln.strip()
            if (s.startswith(("❯", ">", "›")) and probe in s):
                return True
        return False

    def _has_paste_marker(self, seat):
        # Only a marker on a COMPOSER line (prompt-glyph row) means the text is
        # still staged. A rendered "[Pasted Content #n]" placeholder in the
        # transcript/scrollback after a successful submit must not trigger more
        # Enters, so we require the prompt glyph on the same row.
        for ln in self._capture(seat, 8):
            if _PASTE_MARKER in ln and re.search(r"[❯›>]", ln):
                return True
        return False

    # -- spawn / read / wait / respond / interrupt -----------------------------
    def spawn(self, cmd, cwd=None, window_name=None):
        self._ensure_session()
        args = ["new-window", "-t", self.session, "-P", "-F", "#{pane_id}"]
        if cwd:
            args += ["-c", cwd]
        if window_name:
            args += ["-n", window_name]
        args += [cmd]  # the command the window runs
        r = self._tmux(*args, check=True)
        seat = r.stdout.strip()
        return {"ok": True, "seat": seat, "tmux_server": self.server()}

    def read(self, seat, lines=40, raw=False):
        if not self._pane_exists(seat):
            raise self._missing(seat)
        text = "\n".join(self._capture(seat, lines))
        return {"ok": True, "seat": seat, "state": self.state(seat),
                "screen": text if raw else self._redact(text)}

    def wait(self, seat, timeout=120):
        """Block until the seat settles to idle. Mirrors the anu turn loop: no
        clock pressure while busy; only after we've SEEN busy does a 2-tick
        quiet+not-busy settle end the wait (so a prompt echo right after a send
        isn't mistaken for completion). If never busy within a start window,
        return the current state honestly."""
        t0 = time.time()
        sawbusy = False
        quiet = 0
        startwin = 20
        while time.time() - t0 < timeout:
            st = self.state(seat)
            if st in ("dead", "approval"):
                return {"ok": True, "state": st, "seat": seat,
                        "tail": "\n".join(self._capture(seat, 12))}
            if st == "busy":
                sawbusy, quiet = True, 0
                time.sleep(0.6)
                continue
            # idle/booting
            if not sawbusy and time.time() - t0 > startwin:
                return {"ok": True, "state": st, "seat": seat, "sawbusy": False,
                        "tail": "\n".join(self._capture(seat, 12))}
            quiet += 1
            if sawbusy and quiet >= 2:
                return {"ok": True, "state": "idle", "seat": seat, "sawbusy": True,
                        "tail": "\n".join(self._capture(seat, 12))}
            time.sleep(0.5)
        return {"ok": False, "state": self.state(seat), "seat": seat,
                "err": "timeout after %ss" % timeout,
                "tail": "\n".join(self._capture(seat, 12))}

    def respond(self, seat, decision="allow"):
        """Answer a TUI approval — FAIL CLOSED. Refuse unless the classifier
        actually sees an approval prompt; never blindly send keys into a pane.
        Picks an affirmative that is NOT an 'always/don't-ask' variant."""
        st = self.state(seat)
        if st != "approval":
            return {"ok": False, "err": "no approval prompt (state=%s)" % st}
        bottom = self._capture(seat, 16)
        # Parse ONLY the contiguous menu block around the highlighted row — prose
        # above it ("this is what will be delivered") must never count as an
        # option. Both allow and deny drive to a matching row and Enter; NEITHER
        # ever presses a bare Enter on an unknown default (that could confirm the
        # opposite of what was asked). No matching row => FAIL CLOSED.
        menu = self._menu_block(bottom)
        if decision in ("deny", "no", "reject"):
            if not menu:
                if any(re.search(r"\(y/n\)|\[y/n\]|\by/n\b", ln, re.I) for ln in bottom):
                    self._tmux("send-keys", "-t", seat, "-l", "--", "n")
                    self._tmux("send-keys", "-t", seat, "Enter")
                    return {"ok": True, "responded": "n"}
                return {"ok": False, "err": "no menu to deny in — refusing to guess"}
            tgt = choose_option(menu, "deny")
            if tgt is None:
                return {"ok": False, "err": "no clear deny option — refusing to guess"}
            return self._drive_menu(seat, menu, tgt, "deny")
        if not menu:
            return {"ok": False, "err": "no menu block found on screen"}
        target = choose_option(menu, "allow")
        if target is None:
            return {"ok": False,
                    "err": "no clearly-affirmative one-shot option in the menu (sticky/"
                           "session-wide rows are never chosen) — refusing to guess "
                           "(respond by hand or seat send)"}
        return self._drive_menu(seat, menu, target, "allow")

    def _drive_menu(self, seat, menu, target, kind):
        """Select `target` in `menu` and confirm. Prefer the digit (unambiguous);
        else step the highlight to it and Enter. Verify the highlight actually
        LANDED on the target before Enter — a wrapped/multi-line row can make
        positional counts wrong, and pressing Enter on the wrong row could
        confirm the opposite. If we can't confirm the landing, fail closed."""
        if target["digit"]:
            self._tmux("send-keys", "-t", seat, "-l", "--", target["digit"])
            self._tmux("send-keys", "-t", seat, "Enter")
            return {"ok": True, "responded": "%s option %s" % (kind, target["digit"])}
        cur = next((i for i, r in enumerate(menu) if r["hl"]), None)
        if cur is None:
            return {"ok": False, "err": "no highlighted row to move from — refusing"}
        delta = menu.index(target) - cur
        key = "Down" if delta > 0 else "Up"
        for _ in range(abs(delta)):
            self._tmux("send-keys", "-t", seat, key)
            time.sleep(0.12)
        # Verify: the highlighted row's text must now match the target's.
        landed = self._menu_block(self._capture(seat, 16))
        hl = next((r for r in landed if r["hl"]), None)
        want = target["text"][:24]
        if hl is None or want not in hl["text"]:
            return {"ok": False,
                    "err": "could not land the highlight on the intended option "
                           "(menu may wrap) — refusing to guess"}
        self._tmux("send-keys", "-t", seat, "Enter")
        return {"ok": True, "responded": "%s: %s" % (kind, target["text"][:40])}

    @staticmethod
    def _menu_block(lines):
        """The contiguous option rows around the highlighted (❯/›) line: walk up
        and down from it until a blank, border, or prose-shaped line. Option
        rows are short-ish and either highlighted, numbered, or indented like
        their highlighted sibling."""
        hl_idx = None
        for i, ln in enumerate(lines):
            if re.match(r"^\s*[❯›]\s+\S", ln):
                hl_idx = i
        if hl_idx is None:
            # numbered menu without a highlight glyph
            rows = []
            for ln in lines:
                m = re.match(r"^\s*(\d)[.)]\s+(\S.*)", ln)
                if m:
                    rows.append({"hl": False, "digit": m.group(1),
                                 "text": m.group(2).strip()})
            return rows
        hl_indent = len(lines[hl_idx]) - len(lines[hl_idx].lstrip())
        # In a NUMBERED menu every option carries a digit; an unnumbered line is a wrapped
        # continuation or a footer ("Esc to cancel · Tab to amend"), never an option.
        numbered = bool(re.match(r"^\s*[❯›]\s+\d[.)]\s", lines[hl_idx]))

        def row_of(ln):
            m = re.match(r"^(\s*)([❯›]\s+)?(?:(\d)[.)]\s+)?(\S.*)$", ln)
            if not m:
                return None
            indent = len(m.group(1))
            hl = bool(m.group(2))
            text = m.group(4).strip()
            # The ASK itself ("Do you want to create x?") sits right above the options and is
            # never one of them; nor is anything from a diff/file preview above the ask.
            if not hl and not m.group(3) and text.endswith("?"):
                return None
            # The highlighted row is always in. For non-highlighted candidates,
            # exclude PROSE that happens to share the indent: a message body in
            # guillemets, or a label/sentence ending in a colon (e.g. "…what
            # will be delivered:"). Otherwise "delivered" in prose would be
            # picked as the affirmative option. Options never end with ':'.
            if not hl:
                if abs(indent - hl_indent) > 3:
                    return None
                if text.endswith(":") or "«" in text or "»" in text:
                    return None
                if numbered and not m.group(3):
                    return None
            return {"hl": hl, "digit": m.group(3), "text": text}

        border = re.compile(r"^\s*[│╰╭─└┌╌┄═]")   # box, rule AND the dashed diff-preview rule
        block = []
        i = hl_idx
        while i >= 0:
            r = row_of(lines[i])
            if r is None or border.match(lines[i]):
                break
            block.insert(0, r)
            i -= 1
        i = hl_idx + 1
        while i < len(lines):
            ln = lines[i]
            if border.match(ln):
                break
            r = row_of(ln)
            if r is None:
                # A wrapped option continues on deeper-indented lines ("Yes, and switch to accept
                # edits (auto-approve file / edits and common ...) for this session"). Fold them
                # into the previous row instead of ending the menu there, which used to hide "3. No".
                m = re.match(r"^(\s*)(\S.*)$", ln)
                if block and m and len(m.group(1)) > hl_indent and not m.group(2).endswith(":"):
                    block[-1]["text"] = (block[-1]["text"] + " " + m.group(2).strip()).strip()
                    i += 1
                    continue
                break
            block.append(r)
            i += 1
        return block

    def interrupt(self, seat):
        if not self._pane_exists(seat):
            return {"ok": False, "err": "no such seat"}
        self._tmux("send-keys", "-t", seat, "Escape")
        return {"ok": True}

    def kill(self, seat):
        self._tmux("kill-pane", "-t", seat)
        return {"ok": True}

    def ls(self):
        r = self._tmux("list-panes", "-a", "-F",
                       "#{pane_id}\t#{pane_current_command}\t#{pane_title}")
        seats = []
        if r.returncode == 0:
            for ln in r.stdout.splitlines():
                parts = ln.split("\t")
                if len(parts) >= 3:
                    seats.append({"seat": parts[0], "cmd": parts[1],
                                  "title": parts[2]})
        return {"ok": True, "seats": seats, "tmux_server": self.server()}


def adopt_pane(name, seat, sessions_dir, *, socket_path=None, timeout=6):
    """Rename one verified Claude session through its own terminal composer.

    This is intentionally a caller-side operation on the selected tmux server,
    not the daemon's default seat server. A same-named sidecar elsewhere cannot
    confirm it. No dependency on Anu or an external ``pane`` executable.
    """
    import json
    from pathlib import Path

    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name or ""):
        raise SeatError("invalid agent name")
    if not re.fullmatch(r"%[0-9]+", seat or ""):
        raise SeatError("adopt requires an exact pane ID such as %12")
    drv = SeatDriver(socket_path=socket_path, ambient=True)
    pane_pid = drv._field(seat, "#{pane_pid}")
    if not pane_pid.isdigit():
        raise SeatError("no such pane on the selected tmux server")
    process = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True,
                             text=True, timeout=5, check=True)
    children = {}
    for line in process.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and all(value.isdigit() for value in fields):
            children.setdefault(int(fields[1]), []).append(int(fields[0]))
    descendants, pending = set(), [int(pane_pid)]
    while pending:
        pid = pending.pop()
        if pid not in descendants:
            descendants.add(pid)
            pending.extend(children.get(pid, []))

    def read(path):
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    matches = []
    for path in Path(sessions_dir).glob("*.json"):
        value = read(path)
        if (path.stem.isdigit() and value.get("pid") in descendants
                and value.get("messagingSocketPath")
                and value.get("version") != "communicate-com8"):
            matches.append((path, value))
    if len(matches) != 1:
        raise SeatError("cannot identify exactly one Claude session in that pane; nothing sent")
    path, original = matches[0]
    identity_keys = ("pid", "sessionId", "messagingSocketPath")
    if original.get("name") == name:
        return {"ok": True, "already": True, "name": name, "seat": seat}
    state = drv.state(seat)
    if state != "idle":
        raise SeatError("pane is %s; wait for its idle composer before adopting" % state)
    sent = drv.send(seat, "/rename " + name)
    if not sent.get("ok"):
        return sent
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if drv._field(seat, "#{pane_pid}") != pane_pid:
            raise SeatError("pane execution changed while confirming adoption")
        current = read(path)
        if all(current.get(key) == original.get(key) for key in identity_keys):
            if current.get("name") == name:
                return {"ok": True, "name": name, "seat": seat}
        else:
            raise SeatError("session identity changed while confirming adoption")
        time.sleep(0.1)
    return {"ok": False, "sent": True, "err": "rename submitted, but this pane's session has not confirmed it"}
