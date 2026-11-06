#!/usr/bin/env python3
"""Daemon-side unit tests for the branches a live daemon will not reproduce on
demand: _do_restart's ORDER and its refusal to kill what it has not measured as
its own (tmux happily falls back to a default cwd when the recorded one is
gone, so a real tmux cannot be made to fail a spawn); _call against a daemon
that goes quiet; and the check-then-write race in _do_describe."""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import com8  # noqa: E402

pass_ = [0]
fail_ = [0]


def ok(m):
    pass_[0] += 1
    print("ok   " + m)


def bad(m):
    fail_[0] += 1
    print("FAIL " + m)


class Rig(com8.Com8):
    """A Com8 with only what _do_restart touches — no sockets, no daemon."""

    def __init__(self, identities, spawn_ok=True, surfaces=None, kill_ok=True):
        self.mu = threading.RLock()
        self.identities = identities
        self.surfaces = surfaces or {}
        self.spawn_ok = spawn_ok
        self.kill_ok = kill_ok
        self.killed = []
        self.spawned = []
        self.persisted = 0

    def log(self, *a):
        pass

    def _persist_identities(self):
        self.persisted += 1

    def _measure_surface(self, seat):
        return self.surfaces.get(seat)

    def _do_seat(self, sub, req):
        if sub == "spawn":
            self.spawned.append(req)
            if not self.spawn_ok:
                return {"ok": False, "err": "no such directory"}
            return {"ok": True, "seat": "%99"}
        if sub == "kill":
            self.killed.append(req.get("seat"))
            if not self.kill_ok:
                return {"ok": False, "err": "device not linked"}
            return {"ok": True}
        return {"ok": False, "err": "unexpected op %s" % sub}

    def _do_seat_bind(self, seat, name):
        with self.mu:
            self.identities[name]["seat"] = seat
        return {"ok": True}


def ident(seat, cwd="/tmp", spawned_at=100.0):
    return {"kind": "local", "seat": seat,
            "supervision": {"cmd": "bash", "cli": None, "cwd": cwd,
                            "spawned_at": spawned_at}}


def live(seat):
    return {"driver": "tmux", "handle": seat, "state": "idle",
            "measured_at": 1.0}


# 1. A failed respawn must leave the agent with the surface it had. Kill-first
#    would have left it with no surface at all AND ok:false.
r = Rig({"a": ident("%1")}, spawn_ok=False, surfaces={"%1": live("%1")})
res = r._do_restart("a")
if not res.get("ok") and not r.killed and r.identities["a"]["seat"] == "%1":
    ok("a failed respawn kills nothing and keeps the original seat")
else:
    bad("failed respawn: res=%s killed=%s seat=%s"
        % (res, r.killed, r.identities["a"]["seat"]))

# 2. The happy path: spawn, bind, THEN kill the old seat; spawned_at refreshed.
r = Rig({"a": ident("%1")}, surfaces={"%1": live("%1")})
res = r._do_restart("a")
if (res.get("ok") and r.killed == ["%1"] and r.identities["a"]["seat"] == "%99"
        and r.identities["a"]["supervision"]["spawned_at"] > 100.0):
    ok("a successful restart binds the new seat, retires the old, restamps spawned_at")
else:
    bad("happy path: res=%s killed=%s ident=%s" % (res, r.killed, r.identities["a"]))

# 3. A seat measured DEAD is not killed — there is nothing there, and the id may
#    now name somebody else's pane (tmux restarts ids at %0 with its server).
r = Rig({"a": ident("%1")},
        surfaces={"%1": {"driver": "tmux", "handle": "%1", "state": "dead",
                         "measured_at": 1.0}})
res = r._do_restart("a")
if res.get("ok") and not r.killed:
    ok("a dead seat is not killed (nothing to kill, and the id may be recycled)")
else:
    bad("dead seat: res=%s killed=%s" % (res, r.killed))

# 4. UNKNOWN is not a licence to kill: an unmeasurable seat (tmux timed out) is
#    left alone. Never act on what you have not measured.
r = Rig({"a": ident("%1")},
        surfaces={"%1": {"driver": "tmux", "handle": "%1", "state": "unknown",
                         "measured_at": 1.0}})
res = r._do_restart("a")
if res.get("ok") and not r.killed:
    ok("an unmeasurable (unknown) seat is not killed")
else:
    bad("unknown seat: res=%s killed=%s" % (res, r.killed))

# 5. The recycled-pane case: the stored id is live, but another identity is
#    bound to it now. Killing it would take out a different agent.
r = Rig({"a": ident("%1"), "b": ident("%1")}, surfaces={"%1": live("%1")})
res = r._do_restart("a")
if res.get("ok") and not r.killed and r.identities["b"]["seat"] == "%1":
    ok("a seat another identity is bound to is never killed")
else:
    bad("recycled seat: res=%s killed=%s" % (res, r.killed))

# 6. No supervision record => nothing to restart, and nothing killed.
r = Rig({"a": {"kind": "local", "seat": "%1"}}, surfaces={"%1": live("%1")})
res = r._do_restart("a")
if not res.get("ok") and not r.killed and not r.spawned:
    ok("no supervision record: refuses without touching the seat")
else:
    bad("no supervision: res=%s killed=%s spawned=%s" % (res, r.killed, r.spawned))

# 7. previous_killed must report what _do_seat("kill", ...) actually did, not
#    whether the call raised. _do_seat returns {"ok": False, ...} on failure
#    (e.g. a device-qualified seat that is no longer linked) rather than
#    raising, so a bare try/except around the call cannot see the failure.
#    A failed retirement of the OLD seat must not fail a restart whose NEW
#    seat came up fine -- but it must not be reported as a kill that happened.
r = Rig({"a": ident("%1")}, surfaces={"%1": live("%1")}, kill_ok=False)
res = r._do_restart("a")
if res.get("ok") and r.killed == ["%1"] and res.get("previous_killed") is False:
    ok("a failed kill is reported as previous_killed=False, restart still ok")
else:
    bad("failed kill: res=%s killed=%s" % (res, r.killed))

# 8. _do_describe must write through the entry it validated. Taking self.mu
#    twice let a concurrent release land between the check and the write, and
#    the second lookup raised KeyError — which reached the agent as
#    {"ok": false, "err": "'name'"}. The release is staged here by having the
#    clock tick pull the identity out mid-call, at exactly that window.
class Describer(com8.Com8):
    def __init__(self):
        self.mu = threading.Lock()
        self.identities = {"a": {"kind": "local", "card": None}}
        self.persisted = 0

    def log(self, *a):
        pass

    def _persist_identities(self):
        self.persisted += 1


d = Describer()
_real_time = com8.time.time


def _release_mid_call():
    com8.time.time = _real_time          # only the first tick releases
    d.identities.pop("a", None)          # a concurrent `com8 release a`
    return _real_time()


com8.time.time = _release_mid_call
try:
    res = d._do_describe("a", what="I prove things")
finally:
    com8.time.time = _real_time
if res.get("ok") and (res.get("card") or {}).get("what") == "I prove things":
    ok("describe writes through the entry it checked (no KeyError on a race)")
else:
    bad("describe raced with release: %s" % (res,))

# 9. _call must not die with a traceback when the daemon accepts the connection
#    and then says nothing (a large fleet, or one unresponsive pane, now that
#    `agents`/`status` measure every seat through tmux).
import shutil          # noqa: E402
import socket          # noqa: E402
import tempfile        # noqa: E402

tmp = tempfile.mkdtemp(prefix="com8-call-")
try:
    root = os.path.join(tmp, "com8")
    os.makedirs(root)
    os.environ["COMM_STATE"] = tmp
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(os.path.join(root, "com8.sock"))
    srv.listen(4)
    res = com8._call({"op": "status"}, timeout=0.4)
    if isinstance(res, dict) and res.get("ok") is False and res.get("err"):
        ok("a silent daemon returns an error dict: %s" % res["err"])
    else:
        bad("silent daemon produced %r" % (res,))
    srv.close()
finally:
    os.environ.pop("COMM_STATE", None)
    shutil.rmtree(tmp, ignore_errors=True)

# -- _safe_sender: the link boundary never lets a hostile sender name
# through to attribution (conforming names verbatim; everything else,
# including attacker-length strings, normalizes to "unknown").
if (com8.Com8._safe_sender("librarian") == "librarian"
        and com8.Com8._safe_sender("a.b-c_9") == "a.b-c_9"
        and com8.Com8._safe_sender("e" * 200) == "unknown"
        and com8.Com8._safe_sender('x" junk="y') == "unknown"
        and com8.Com8._safe_sender("") == "unknown"
        and com8.Com8._safe_sender(None) == "unknown"
        and com8.Com8._safe_sender("UPPER") == "unknown"):
    ok("_safe_sender: conforming verbatim, hostile/absent -> unknown")
else:
    bad("_safe_sender normalization")

# -- _seat_wrap: keyboard-surface mail carries attribution and teaches the
# reply path exactly once (ask tokens already embed their own).
w = com8.Com8._seat_wrap({"from_name": "alice", "text": "hi there"}, "relaybot")
w2 = com8.Com8._seat_wrap(
    {"from_name": "alice",
     "text": 'q [reply with: communicate com8 reply alice@dev~ab12 "<answer>" --from relaybot]'},
    "relaybot")
if (w.startswith("[com8 mail from @alice] hi there")
        and 'communicate com8 send alice' in w
        and "communicate com8 send alice" not in w2
        and "communicate com8 reply" in w2):
    ok("_seat_wrap: attribution + reply path, never doubled over ask tokens")
else:
    bad("_seat_wrap shape (w=%r w2=%r)" % (w[:80], w2[:80]))

# 10. _do_spawn's adoption. Claude Code's workspace-trust dialog highlights
#     "No, exit" by default and /rename's submit is a bare Enter: the boot
#     loop used to break on "approval" and type /rename into that dialog, so
#     every spawn in a directory not yet trusted exited within a second
#     (Claude Code 2.1.318, 2026-11-05). The seat must answer the dialog with
#     the affirmative row, type NOTHING while the pane is blank or holds any
#     other dialog, and /rename only once a real idle prompt is up.
import com8_seat   # noqa: E402


class Seat:
    """A scripted seat driver: state() reports `states` in order (the last
    repeats). With trust=True the approval on screen is the trust dialog,
    and answering it boots on to an idle composer."""

    def __init__(self, states, trust=False):
        self.states, self.trust, self.calls = list(states), trust, []

    def state(self, seat):
        st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        self.calls.append(("state", st))
        return st

    def is_trust_prompt(self, seat):
        return self.trust

    def respond(self, seat, decision="allow"):
        self.calls.append(("respond", decision))
        if self.trust and decision == "allow":
            self.trust, self.states = False, ["booting", "idle"]
            return {"ok": True, "responded": "allow: Yes, I trust this folder"}
        return {"ok": False, "err": "no clearly-affirmative option"}

    def send(self, seat, text):
        # record the text and the state it was typed into
        self.calls.append(("send", text, self.calls[-1][1]))
        return {"ok": True, "sent": True}


class Spawner(com8.Com8):
    """A Com8 with only what _do_spawn touches: claim, seat spawn/bind, the
    seat driver and the sidecar scan — no sockets, no tmux, no daemon."""

    def __init__(self, drv):
        self.mu = threading.RLock()
        self.identities = {}
        self.drv = drv
        self.logged = []

    def log(self, *a):
        self.logged.append(" ".join(str(x) for x in a))

    def _persist_identities(self):
        pass

    def _do_claim(self, name, cwd=None, worktree=False):
        self.identities[name] = {"kind": "local"}
        return {"ok": True, "name": name, "workspace": {"path": cwd or "/tmp"}}

    def _do_seat(self, sub, req):
        return {"ok": True, "seat": "%7",
                "tmux_server": {"socket_path": None, "pid": None}}

    def _do_seat_bind(self, seat, name):
        self.identities[name]["seat"] = seat
        return {"ok": True}

    def _seat_drv(self):
        return self.drv

    def _scan_sidecars(self):
        # claude's sidecar carries the name only after /rename was submitted
        return {"w"} if any(c[0] == "send" for c in self.drv.calls) else set()


class Clock:
    """Virtual time: boot waits and settle sleeps cost nothing here."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s


def spawn_with(states, trust=False):
    drv = Seat(states, trust=trust)
    sp = Spawner(drv)
    saved = com8.time, com8_seat.time
    com8.time = com8_seat.time = Clock()
    try:
        res = sp._do_spawn("w", "claude", cwd="/tmp", adopt=True)
    finally:
        com8.time, com8_seat.time = saved
    return res, drv, [c for c in drv.calls if c[0] == "send"]


# blank -> trust dialog -> (answered) -> blank -> idle: ONE /rename, typed into idle
res, drv, sends = spawn_with(["booting", "booting", "approval"], trust=True)
responded = [c for c in drv.calls if c[0] == "respond"]
if (res.get("ok") and res.get("adopted") is True
        and responded == [("respond", "allow")]
        and sends == [("send", "/rename w", "idle")]
        and drv.calls.index(responded[0]) < drv.calls.index(sends[0])):
    ok("spawn answers the trust dialog with allow, then /renames once a real idle prompt is up")
else:
    bad("trust-dialog adoption: res=%s calls=%s" % (res, drv.calls))

# an already-trusted workspace: straight to /rename on the first idle prompt
res, drv, sends = spawn_with(["idle"])
if res.get("adopted") is True and sends == [("send", "/rename w", "idle")]:
    ok("a trusted workspace still adopts on the first idle prompt")
else:
    bad("trusted adoption: res=%s calls=%s" % (res, drv.calls))

# any OTHER dialog (a Write permission prompt, a resume picker) is never typed into
res, drv, sends = spawn_with(["booting", "approval"])
if (res.get("ok") and res.get("adopted") is False and not sends
        and not [c for c in drv.calls if c[0] == "respond"]
        and "approval" in (res.get("adopt_err") or "")):
    ok("a non-trust dialog is left alone: no /rename, and adopted:false says why")
else:
    bad("foreign dialog: res=%s calls=%s" % (res, drv.calls))

# a pane that never renders, or dies while booting: nothing typed
for states, what in ((["booting"], "a pane that never renders"),
                     (["booting", "dead"], "a seat that dies while booting")):
    res, drv, sends = spawn_with(states)
    if res.get("ok") and res.get("adopted") is False and not sends:
        ok("%s gets no /rename" % what)
    else:
        bad("%s: res=%s calls=%s" % (what, res, drv.calls))

print("\npass=%d fail=%d" % (pass_[0], fail_[0]))
sys.exit(1 if fail_[0] else 0)
