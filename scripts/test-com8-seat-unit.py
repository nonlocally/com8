#!/usr/bin/env python3
"""Unit tests for the seat driver's fail-closed respond logic (review I-4/M-1)
and the menu-block parser — no tmux needed, pure logic against captured screens."""
import sys
import os
import re
import subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import com8_seat

pass_ = [0]
fail_ = [0]


def ok(m):
    pass_[0] += 1
    print("ok   " + m)


def bad(m):
    fail_[0] += 1
    print("FAIL " + m)


class Fake(com8_seat.SeatDriver):
    def __init__(self, screen):
        super().__init__()
        self.screen = screen
        self.keys = []

    def _pane_exists(self, seat):
        return True

    def _capture(self, seat, lines=None):
        s = self.screen.split("\n")
        return s[-lines:] if lines else s

    def _tmux(self, *a, **k):
        self.keys.append(a)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    def state(self, seat):
        return "approval"


HELD = ("  Message body (this is what will be delivered):\n"
        "  «do a thing»\n"
        "❯ Deny — drop it and tell the sender it was declined\n"
        "  Deliver this message to Claude")

# I-4: deny on a menu whose highlighted row is already Deny must Enter in place,
# never step toward the affirmative "Deliver" row.
d = Fake(HELD)
r = d.respond("%1", "deny")
downs = [k for k in d.keys if len(k) > 2 and "Down" in k]
if r.get("ok") and not downs:
    ok("deny confirms the highlighted Deny row without moving toward Deliver")
else:
    bad("deny moved toward the affirmative row: %s" % (d.keys,))

# The prose line "this is what will be delivered" must NOT be parsed as an option.
block = d._menu_block(HELD.split("\n"))
texts = [b["text"] for b in block]
if not any("what will be delivered" in t for t in texts):
    ok("prose above the menu is not parsed as an option")
else:
    bad("prose leaked into the menu block: %s" % texts)

# allow with no affirmative option => fail closed (never a bare Enter).
d2 = Fake("  Choose:\n❯ Cancel\n  Abort")
r = d2.respond("%1", "allow")
if not r.get("ok") and not any(("Enter",) == k[-1:] for k in d2.keys):
    ok("allow fails closed when no affirmative option exists (no Enter pressed)")
else:
    bad("allow did not fail closed: %s / %s" % (r, d2.keys))

# deny with no deny option => fail closed too.
d3 = Fake("  Proceed?\n❯ Yes, allow\n  Yes, and don't ask again")
r = d3.respond("%1", "deny")
if not r.get("ok"):
    ok("deny fails closed when no deny option exists")
else:
    bad("deny did not fail closed: %s" % r)


# -- measure(): "I could not ask" is not "it is dead" ------------------------
# A tmux that does not answer within the timeout used to surface as a MEASURED
# state:"dead" — the surface axis asserting the seat is gone on no evidence.


class Gone(com8_seat.SeatDriver):
    """tmux answers, and its answer is that the pane does not exist."""

    def _pane_exists(self, seat):
        return False


def measure_when_run_raises(exc, seat="%7"):
    """Measure through the REAL _tmux with subprocess.run raising `exc` — the
    conversion from OS failure to seat state is the thing under test."""
    real = com8_seat.subprocess.run

    def boom(*a, **k):
        raise exc
    com8_seat.subprocess.run = boom
    try:
        return com8_seat.SeatDriver().measure(seat)
    finally:
        com8_seat.subprocess.run = real


m = measure_when_run_raises(subprocess.TimeoutExpired(cmd="tmux", timeout=10))
if m and m["state"] == "unknown" and m["handle"] == "%7":
    ok("a tmux timeout measures as unknown, never as dead")
else:
    bad("tmux timeout measured as %s" % (m,))

m = measure_when_run_raises(OSError(2, "No such file or directory: 'tmux'"))
if m and m["state"] == "unknown":
    ok("an unreachable tmux measures as unknown")
else:
    bad("missing tmux measured as %s" % (m,))

m = Gone().measure("%7")
if m and m["state"] == "dead":
    ok("a pane tmux says is gone still measures as dead")
else:
    bad("dead pane measured as %s" % (m,))

# -- a freshly spawned claude: blank, then the workspace-trust dialog --------
# Measured 2026-11-05 on Claude Code 2.1.318 in a throwaway tmux server: for
# about a second the pane runs claude (pane_current_command is its version
# string) with NOTHING drawn, then shows the workspace-trust dialog whose
# highlighted default row is "No, exit". The old adopt loop read the blank
# pane as idle and the dialog as any approval, then typed /rename: its submit
# Enter chose "No, exit", claude exited 1, and the window vanished.

TRUST = (
    "\n"
    "──────────────────────────────────────────────────────────────────────\n"
    " Accessing workspace:\n"
    "\n"
    " /home/example/project\n"
    "\n"
    " Quick safety check: Is this a project you created or one you trust?\n"
    " If not, take a moment to review what's in this folder first.\n"
    "\n"
    " Claude Code'll be able to read, edit, and execute files here.\n"
    "\n"
    " Security guide\n"
    "\n"
    " ❯ No, exit\n"
    "   Yes, I trust this folder\n"
    "\n"
    " Enter to confirm · Esc to cancel")
TRUST_YES = TRUST.replace(" ❯ No, exit\n   Yes, I trust this folder",
                          "   No, exit\n ❯ Yes, I trust this folder")
# The same dialog for a folder that ships .claude/settings.json allow rules
# (also: added directories, a headersHelper) — captured the same day. Same two
# rows, but "Yes" also enables every pre-approved permission.
WIDEN = TRUST.replace(
    " Claude Code'll be able to read, edit, and execute files here.\n",
    " Claude Code'll be able to read, edit, and execute files here.\n"
    "\n"
    " ⚠ This folder pre-approves 2 tool permissions in .claude/settings.json:\n"
    "   Bash(ls:*) and Read\n"
    " These will apply without asking. Only proceed if you trust this configuration.\n")
WIDEN_YES = WIDEN.replace(" ❯ No, exit\n   Yes, I trust this folder",
                          "   No, exit\n ❯ Yes, I trust this folder")
# The shape that offers to keep going WITHOUT the pre-approved permissions.
WCONT = WIDEN.replace(" ❯ No, exit\n   Yes, I trust this folder",
                      " ❯ No, continue without these permissions\n   Yes, I trust this folder")
WCONT_YES = WIDEN.replace(" ❯ No, exit\n   Yes, I trust this folder",
                          "   No, continue without these permissions\n ❯ Yes, I trust this folder")
# A permission prompt whose command preview happens to contain the words.
CMD_PREVIEW = ("\n⏺ Bash(echo 'I trust this folder')\n\n"
               "─────────────────────────────────────────────────────────\n"
               " Bash command\n"
               " echo 'I trust this folder'\n"
               " Do you want to proceed?\n"
               " ❯ 1. Yes\n"
               "   2. Yes, and don't ask again for echo commands in this project\n"
               "   3. No, and tell Claude what to do differently (esc)\n"
               "\n"
               " Esc to cancel · Tab to amend")
IDLE = ("\n ▐▛███▛█   Claude Code v2.1.318\n"
        "▝▜██████▀  Opus 5.5 with high effort\n"
        " ▝▝   ▝▝   ~/project\n" + "\n" * 24 +
        "──────────────────────────────────────────────────────────────────────\n"
        "❯ Try \"create a util logging.py that...\"\n"
        "──────────────────────────────────────────────────────────────────────\n"
        "  ⏵⏵ auto mode on (shift+tab to cycle)")


# What the dialog does with Down/Up (the highlight moves) and Enter (the
# highlighted row is confirmed): "No, exit" ends claude; the Yes rows of the
# pre-approval variants ALSO enable the folder's permissions.
_DOWN = {TRUST: TRUST_YES, WIDEN: WIDEN_YES, WCONT: WCONT_YES}
_UP = {v: k for k, v in _DOWN.items()}
_CONTINUES = {TRUST_YES, WIDEN_YES, WCONT, WCONT_YES}
_WIDENS = {WIDEN_YES, WCONT_YES}


class Pane(com8_seat.SeatDriver):
    """A pane as tmux answers for it — screen, foreground command, dead flag —
    plus the keys the trust dialog reacts to."""

    def __init__(self, screen, cmd="2.1.318", dead=False):
        super().__init__()
        self.screen, self.cmd, self.dead = screen, cmd, dead
        self.exists = True
        self.widened = False
        self.keys = []

    def _vals(self, seat):
        return {"pane_id": seat, "pane_dead": "1" if self.dead else "0",
                "pane_current_command": self.cmd, "pane_title": "✳ Claude Code",
                "pane_in_mode": "0"}

    def _pane_exists(self, seat):
        return self.exists

    def _field(self, seat, fmt):
        return re.sub(r"#\{(\w+)\}", lambda m: self._vals(seat).get(m.group(1), ""), fmt)

    def _capture(self, seat, lines=None):
        s = self.screen.split("\n")
        while s and s[-1] == "":
            s.pop()
        return s[-lines:] if lines else s

    def _title_animates(self, seat):
        return False

    def _screen_changing(self, seat):
        return False

    def _tmux(self, *a, **k):
        self.keys.append(a)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        if a[:1] == ("send-keys",):
            key = a[-1]
            if key == "Down":
                self.screen = _DOWN.get(self.screen, self.screen)
            elif key == "Up":
                self.screen = _UP.get(self.screen, self.screen)
            elif key == "Enter" and self.screen in _CONTINUES:
                self.widened = self.widened or self.screen in _WIDENS
                self.screen = IDLE
            elif key == "Enter" and self.screen in (TRUST, WIDEN):
                self.screen, self.exists = "", False   # "No, exit": claude is gone
        elif a[:1] == ("list-panes",):
            R.stdout = re.sub(r"#\{(\w+)\}",
                              lambda m: self._vals("%1").get(m.group(1), ""), a[-1]) + "\n"
        return R()


# Nothing drawn yet is booting, not idle — for the agent CLI (claude reports
# its version string as the command), the `zsh -c claude` instant before the
# exec, and node alike. Nothing may be typed into a pane that has not rendered.
for cmd in ("2.1.318", "zsh", "node"):
    st = Pane("", cmd=cmd).state("%1")
    if st == "booting":
        ok("a blank pane running %s reads booting, not idle" % cmd)
    else:
        bad("a blank pane running %s read %s" % (cmd, st))

p = Pane(TRUST)
st = p.state("%1")
if st == "approval" and p.trust_prompt("%1") == "trust":
    ok("the trust dialog reads approval and is recognized as the plain trust prompt")
else:
    bad("trust dialog read %s (trust prompt: %s)" % (st, p.trust_prompt("%1")))
if Pane(IDLE).trust_prompt("%1") is None and Pane(IDLE).state("%1") == "idle":
    ok("an idle composer is idle and not the trust prompt")
else:
    bad("idle composer: state=%s trust=%s" % (Pane(IDLE).state("%1"),
                                              Pane(IDLE).trust_prompt("%1")))
# The matcher is anchored to the dialog's own rows: a permission prompt whose
# command preview contains the words is an approval, not the trust dialog.
cp = Pane(CMD_PREVIEW)
if cp.state("%1") == "approval" and cp.trust_prompt("%1") is None:
    ok("a command preview containing 'trust this folder' is not the trust dialog")
else:
    bad("command preview: state=%s trust=%s" % (cp.state("%1"), cp.trust_prompt("%1")))

# allow drives the affirmative row: Down off "No, exit", verify, Enter.
r = p.respond("%1", "allow")
pressed = [k[-1] for k in p.keys if k[:1] == ("send-keys",)]
if r.get("ok") and pressed == ["Down", "Enter"] and p.state("%1") == "idle":
    ok("allow steps the highlight onto 'Yes, I trust this folder' and confirms; "
       "the composer then reads idle")
else:
    bad("allow on the trust dialog: %s keys=%s state=%s" % (r, pressed, p.state("%1")))

# -- the pre-approval variant: Yes would also enable the folder's permissions
w = Pane(WIDEN)
if w.state("%1") == "approval" and w.trust_prompt("%1") == "widen":
    ok("the pre-approval dialog is recognized as the widening variant")
else:
    bad("pre-approval dialog: state=%s trust=%s" % (w.state("%1"), w.trust_prompt("%1")))
r = w.respond("%1", "allow")
pressed = [k[-1] for k in w.keys if k[:1] == ("send-keys",)]
if (not r.get("ok") and not pressed and w.screen == WIDEN
        and "pre-approve" in (r.get("err") or "")):
    ok("allow refuses to widen: no key pressed, the dialog stays up, the error says why")
else:
    bad("allow on the pre-approval dialog: %s keys=%s" % (r, pressed))
res = com8_seat.rename_when_ready(w, "%1", "w", boot_wait=2)
pressed = [k[-1] for k in w.keys if k[:1] == ("send-keys",)]
if (not res.get("ok") and res.get("state") == "approval" and not pressed
        and not w.widened and w.exists and "pre-approve" in (res.get("err") or "")):
    ok("adoption leaves the pre-approval dialog to a human: seat alive, nothing widened, "
       "no /rename, the reason reported")
else:
    bad("adoption on the pre-approval dialog: %s keys=%s widened=%s exists=%s"
        % (res, pressed, w.widened, w.exists))

# The shape that offers "No, continue without these permissions": that row
# keeps the seat and widens nothing — chosen whether it is highlighted or not.
for screen, where in ((WCONT, "highlighted"), (WCONT_YES, "below a highlighted Yes")):
    c = Pane(screen)
    kind = c.trust_prompt("%1")
    res = com8_seat.rename_when_ready(c, "%1", "w", boot_wait=5)
    pressed = [k[-1] for k in c.keys if k[:1] == ("send-keys",)]
    if (kind == "widen-continue" and res.get("ok") and not c.widened
            and pressed[-2:] == ["/rename w", "Enter"] and "Enter" in pressed[:-2]
            and not c.keys.count(("send-keys", "-t", "%1", "Down"))):
        ok("'No, continue without these permissions' (%s) is chosen, then /rename on idle"
           % where)
    else:
        bad("continue-without (%s): kind=%s res=%s keys=%s widened=%s"
            % (where, kind, res, pressed, c.widened))


# -- SeatUnavailable mid-dialog (an ssh timeout) is reported, never raised ----
class Flaky(Pane):
    def __init__(self, screen, where):
        super().__init__(screen)
        self.where = where

    def trust_prompt(self, seat):
        if self.where == "trust_prompt":
            raise com8_seat.SeatUnavailable("tmux did not answer: timed out")
        return super().trust_prompt(seat)

    def respond(self, seat, decision="allow"):
        if self.where == "respond":
            raise com8_seat.SeatUnavailable("tmux did not answer: timed out")
        return super().respond(seat, decision)

    def send(self, seat, text):
        if self.where == "send":
            raise com8_seat.SeatUnavailable("tmux did not answer: timed out")
        return super().send(seat, text)


for where, screen in (("trust_prompt", TRUST), ("respond", TRUST), ("send", IDLE)):
    try:
        res = com8_seat.rename_when_ready(Flaky(screen, where), "%1", "w", boot_wait=2)
    except Exception as e:   # noqa: BLE001 — the point is that nothing escapes
        res = {"raised": repr(e)}
    if (not res.get("ok") and not res.get("raised") and res.get("state") == "unknown"
            and "did not answer" in (res.get("err") or "")):
        ok("an unreachable tmux inside %s() is reported as unknown, not raised" % where)
    else:
        bad("unreachable tmux inside %s(): %s" % (where, res))

# -- remain-on-exit: an exited program's pane is dead, not a live seat -------
d = Pane("", cmd="zsh", dead=True)
if d.state("%1") == "dead":
    ok("a kept pane whose program exited reads dead, not an idle shell")
else:
    bad("exited pane read %s" % d.state("%1"))
seats = d.ls()["seats"]
if (seats and seats[0]["seat"] == "%1" and seats[0].get("dead") is True
        and "seat kill %1" in (seats[0].get("hint") or "")):
    ok("seat ls flags the exited pane dead and says how to clear it")
else:
    bad("seat ls listed the exited pane as live or without a hint: %s" % seats)
live = Pane(IDLE).ls()["seats"]
if live and live[0].get("dead") is False:
    ok("seat ls lists a live pane as not dead")
else:
    bad("seat ls live flag: %s" % live)
try:
    d.send("%1", "hello")
    bad("send typed into a dead pane and reported a submit")
except com8_seat.SeatError:
    ok("send refuses a dead pane")
if not d.is_agent_seat("%1") and d.measure("%1")["state"] == "dead":
    ok("a dead pane is not an agent seat and measures dead")
else:
    bad("dead pane: agent=%s measure=%s" % (d.is_agent_seat("%1"), d.measure("%1")))
# measure() tells a kept corpse (reapable) from a pane that is gone.
gone = Pane("")
gone.exists = False
if d.measure("%1").get("kept") is True and gone.measure("%1").get("kept") is False:
    ok("measure marks the kept corpse reapable and a gone pane not")
else:
    bad("measure kept flags: %s / %s" % (d.measure("%1"), gone.measure("%1")))


# -- wait(): a booting tick (nothing rendered) never counts as settling -----
class Scripted(Pane):
    def __init__(self, states):
        super().__init__("")
        self.states, self.seen = list(states), 0

    def state(self, seat):
        self.seen += 1
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


_sleep = com8_seat.time.sleep
com8_seat.time.sleep = lambda s: None
try:
    sc = Scripted(["busy", "booting", "booting", "idle", "idle"])
    w = sc.wait("%1", timeout=5)
finally:
    com8_seat.time.sleep = _sleep
if w.get("ok") and w.get("state") == "idle" and w.get("sawbusy") and sc.seen == 5:
    ok("wait keeps waiting through booting ticks and settles on two idle ticks")
else:
    bad("wait through booting: %s after %d samples" % (w, sc.seen))

print("\npass=%d fail=%d" % (pass_[0], fail_[0]))
sys.exit(1 if fail_[0] else 0)
