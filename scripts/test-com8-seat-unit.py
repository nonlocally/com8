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
IDLE = ("\n ▐▛███▛█   Claude Code v2.1.318\n"
        "▝▜██████▀  Opus 5.5 with high effort\n"
        " ▝▝   ▝▝   ~/project\n" + "\n" * 24 +
        "──────────────────────────────────────────────────────────────────────\n"
        "❯ Try \"create a util logging.py that...\"\n"
        "──────────────────────────────────────────────────────────────────────\n"
        "  ⏵⏵ auto mode on (shift+tab to cycle)")


class Pane(com8_seat.SeatDriver):
    """A pane as tmux answers for it — screen, foreground command, dead flag —
    plus the two keys the trust dialog reacts to: Down moves the highlight,
    Enter confirms the highlighted row."""

    def __init__(self, screen, cmd="2.1.318", dead=False):
        super().__init__()
        self.screen, self.cmd, self.dead = screen, cmd, dead
        self.exists = True
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
            if a[-1] == "Down" and self.screen == TRUST:
                self.screen = TRUST_YES
            elif a[-1] == "Enter" and self.screen == TRUST_YES:
                self.screen = IDLE
            elif a[-1] == "Enter" and self.screen == TRUST:
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
if st == "approval" and p.is_trust_prompt("%1"):
    ok("the trust dialog reads approval and is recognized as the trust prompt")
else:
    bad("trust dialog read %s (trust prompt: %s)" % (st, p.is_trust_prompt("%1")))
if not Pane(IDLE).is_trust_prompt("%1") and Pane(IDLE).state("%1") == "idle":
    ok("an idle composer is idle and not the trust prompt")
else:
    bad("idle composer: state=%s trust=%s" % (Pane(IDLE).state("%1"),
                                              Pane(IDLE).is_trust_prompt("%1")))

# allow drives the affirmative row: Down off "No, exit", verify, Enter.
r = p.respond("%1", "allow")
pressed = [k[-1] for k in p.keys if k[:1] == ("send-keys",)]
if r.get("ok") and pressed == ["Down", "Enter"] and p.state("%1") == "idle":
    ok("allow steps the highlight onto 'Yes, I trust this folder' and confirms; "
       "the composer then reads idle")
else:
    bad("allow on the trust dialog: %s keys=%s state=%s" % (r, pressed, p.state("%1")))

# -- remain-on-exit: an exited program's pane is dead, not a live seat -------
d = Pane("", cmd="zsh", dead=True)
if d.state("%1") == "dead":
    ok("a kept pane whose program exited reads dead, not an idle shell")
else:
    bad("exited pane read %s" % d.state("%1"))
seats = d.ls()["seats"]
if seats and seats[0]["seat"] == "%1" and seats[0].get("dead") is True:
    ok("seat ls flags the exited pane dead")
else:
    bad("seat ls listed the exited pane as live: %s" % seats)
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
