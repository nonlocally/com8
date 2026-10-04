#!/usr/bin/env python3
"""Unit tests for the seat driver's fail-closed respond logic (review I-4/M-1)
and the menu-block parser — no tmux needed, pure logic against captured screens."""
import sys
import os
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

print("\npass=%d fail=%d" % (pass_[0], fail_[0]))
sys.exit(1 if fail_[0] else 0)
