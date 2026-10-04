# Four-Axis Agent Ontology Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every com8 identity four independent axes — **identity · workspace · place · surface** — plus a capability **card** that is derived as a side effect of creating the agent, so the fabric can restart, relocate, and describe its agents honestly.

**Architecture:** The daemon's identity record grows from 5 flat fields into four namespaced sub-records. Everything is derived at claim/spawn time (never a separate remembered write), persisted in `identities.json`, and surfaced through both faces (CLI + MCP). No new store, no new daemon, no new process supervisor — com8 records *what would be needed* to restart, and leaves the deciding to whoever restarts.

**Tech Stack:** Python 3.9+ stdlib only (`lib/com8.py`, `lib/com8_seat.py`), bash verb layer (`lib/com8.sh`), TypeScript MCP face (`packages/com8/src/server.ts`), bash test suites (`scripts/test-com8-*.sh`).

**Spec:** This plan argues from the four studies in `docs/studies/2026-08-16-*.md` (harvest, sandbox, distribution, crossfleet) and the ontology agreed with the user, restated in **The Ontology** below. Executors should read the ontology section and `docs/2026-08-16-overnight-handoff.md`.

## The Ontology (the spec this plan implements)

Four axes, independent. Each can change without the others.

| Axis | Answers | Today | After this plan |
|---|---|---|---|
| **identity** | *who* | ✅ name + durable mailbox | unchanged (+ `aliases`) |
| **workspace** | *on what material* | ❌ nothing — `cwd` is passed to tmux and forgotten | `{path, ref, branch, worktree}` recorded at spawn |
| **place** | *where the process runs* | ⚠️ 3 ad-hoc fields (`kind`/`home`/`boxed`) | one typed `place` record |
| **surface** | *how you see/drive it* | ⚠️ `seat: "%9"`, never measured | `{driver, handle, state, measured_at}` |
| **card** | *what it is for* | ❌ nothing | derived at claim, agent-updatable |

**Why these five and nothing else** — the studies were explicit that an artifact store, a scheduler, and fabric-enforced budgets must be *refused*: git, launchd, and Slurm already do those, and building them into the fabric repeats the tournament-swarm mistake (workflow opinion in the substrate).

## Global Constraints

- **Python 3.9 floor.** `lib/com8.py` must import on macOS system python3 (3.9.6 on air-2). No walrus-in-comprehension tricks, no `match`, no `X | Y` type syntax.
- **Stdlib only** in the daemon. No pip, ever. The npm package vendors it verbatim.
- **The four invariants hold** (`docs/2026-08-16-overnight-handoff.md`): an address must not die; never advertise what you have not measured; durability before delivery; decouple failure domains.
- **The registry law:** any field that requires a separate remembered write will die. Every field added here is derived at claim/spawn and may be *overridden* afterward, never *required*.
- **Backward compatibility:** existing `identities.json` files (5 flat fields) must load without error and without data loss. New sub-records default to `None`/absent.
- **All 173 existing checks stay green.** Run the full suite before every commit.
- **Commits unsigned:** `git -c commit.gpgsign=false commit`. Push over HTTPS with `credential.helper='!gh auth git-credential'`. No AI-attribution trailers.
- **Trust verbs stay human-only:** `federate`, `grant`, `ungrant`, `link` must never become MCP tools.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `lib/com8.py` | the daemon: identity records, claim/spawn/move, ops | modify — the bulk of this plan |
| `lib/com8_workspace.py` | **new** — git interrogation + worktree creation, no daemon deps | create |
| `lib/com8_seat.py` | seat driver: classify, send, read | modify — `measure()` for surface state |
| `lib/com8.sh` | bash verb pass-through | modify — new verbs |
| `packages/com8/src/server.ts` | MCP face | modify — new tools + instructions |
| `scripts/test-com8-workspace.sh` | **new** — workspace recording + worktree | create |
| `scripts/test-com8-card.sh` | **new** — card derivation + override | create |
| `scripts/test-com8-move.sh` | move suite | modify — workspace carriage |
| `registry/` | 5 stale cards | **delete entries**, keep one example |
| `lib/directory.sh` | name→capability join with file-existence liveness | **delete** |

`com8_workspace.py` is a separate file on purpose: git interrogation is pure, testable without a daemon, and `com8.py` is already 3,394 lines.

---

## Task 0: Remove what is superseded and lying

Two surfaces are both replaced by com8 *and* actively wrong. The user's instruction: *"just because something was there before does not mean that we need to keep it or that it is correct."*

**Delete:**
1. `registry/*.md` entries — superseded by the derived card (Task 5), stale since 2026-08-09, and they publish another person's hostname, GitHub handle, private repo name, and a 1,126-run failure to a repo intended for open source.
2. `lib/directory.sh` + the `directory` verb — its liveness is a **file-existence join**, the exact defect the v1 spec flagged as a merge hazard and the switchboard doc records as having *"reported a peer LIVE for a 26-hour outage."* `com8 agents` replaces it with measured liveness.

**Explicitly NOT deleted, with reasons** (so a later reader knows this was decided, not missed):
- `lib/router.sh` (16 call sites), `lib/peer.sh` (17), `lib/wake.sh` (28), `lib/claude.sh`, `lib/codex.sh` — these still do things com8 does not: bridging a remote Claude session as a native peer, presenting Codex as a peer, and event triggers (`wake --on-pr`). Removing them is its own migration plan, not a side effect of this one.
- `origin/switchboard` — its provenance ladder is already absorbed into com8; its `aliases` idea is adopted in Task 1. Delete the branch **after** Task 1 lands, when nothing in it is unrepresented.

**Files:**
- Delete: `registry/aadarwal-communicate-maintainer.md`, `registry/<their-agent>.md`, `registry/gds-agent.md`, `registry/mini-agent.md`, `registry/tidy3d-agent.md`, `lib/directory.sh`
- Create: `registry/EXAMPLE.md`
- Modify: `bin/communicate` (remove the `directory` case + source line), `registry/README.md`, `.gitignore`, `openwebui/dispatch_tool.py`

**Interfaces:**
- Produces: nothing consumed by later tasks. Pure removal.

- [ ] **Step 1: Confirm what would leak, so the removal is evidence-based**

```bash
cd /Users/aadarwal/src/aadarwal/communicate
git ls-files registry/
grep -n 'device:\|operator:\|<their-org>' registry/<their-agent>.md
```
Expected: 6 tracked files; the collaborator card discloses `<collaborator-host>`, `<collaborator-handle>`, `<their-org>/<their-repo>`.

- [ ] **Step 2: Delete the entries and the lying join**

```bash
git rm -q registry/aadarwal-communicate-maintainer.md \
          registry/<their-agent>.md \
          registry/gds-agent.md registry/mini-agent.md registry/tidy3d-agent.md \
          lib/directory.sh
```

- [ ] **Step 3: Keep the schema, not the entries**

Create `registry/EXAMPLE.md`:

```markdown
---
name: example-agent
what: one line — what this agent or corpus IS
ask-me-for: what a peer should send it
workspace: ~/src/project @ main
availability: on demand
---

# example-agent

This directory is the *shareable* form of a capability card. Cards are
generated into the identity record by `com8 claim`/`com8 spawn` and live in
the daemon's state; this folder is for cards you deliberately choose to write
by hand and share.

Entries here are gitignored by default: a card describes a real machine, a
real corpus, and sometimes another person's infrastructure. Ship the schema,
never the entries.
```

Append to `.gitignore`:

```gitignore
# Capability cards describe real hosts/corpora — schema is shared, entries are not.
registry/*.md
!registry/README.md
!registry/EXAMPLE.md
```

- [ ] **Step 4: Remove the verb from the dispatcher**

In `bin/communicate`, delete the `source "$COMM_HOME/lib/directory.sh"` line and the `directory) directory_show "$@";;` case, and remove the `communicate directory` line from the usage header block.

- [ ] **Step 5: Repoint the one real consumer**

`openwebui/dispatch_tool.py` reads the registry to answer `list_agents`. Repoint it at the measured roster. Replace its registry-reading body with:

```python
def list_agents():
    """Agents reachable through com8, with measured liveness."""
    out = subprocess.run(["communicate", "com8", "agents", "--json"],
                         capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        return {"agents": [], "error": out.stderr.strip()}
    data = json.loads(out.stdout or "{}")
    return {"agents": [
        {"name": a["name"], "what": (a.get("card") or {}).get("what", ""),
         "live": a.get("state") == "live", "device": a.get("home") or data.get("device")}
        for a in data.get("agents", [])]}
```

- [ ] **Step 6: Verify nothing else referenced the removed surfaces**

```bash
grep -rn 'directory_show\|lib/directory' bin/ lib/ scripts/ packages/ || echo "clean"
./scripts/test-com8-core.sh 2>&1 | tail -1
```
Expected: `clean`, then `pass=46 fail=0`.

- [ ] **Step 7: Commit**

```bash
git add -A registry/ .gitignore bin/communicate openwebui/dispatch_tool.py
git -c commit.gpgsign=false commit -m "remove: stale capability cards + the file-existence directory join

registry/*.md entries were superseded by derived cards, stale since 2026-08-09,
and published another operator's hostname, GitHub handle, private repo, and a
1,126-run failure to a repo intended for open source. Ship the schema, never the
entries: EXAMPLE.md stays, entries are gitignored.

lib/directory.sh joined name->capability against FILE EXISTENCE for liveness --
the defect the v1 spec flagged as a merge hazard and which once reported a peer
LIVE through a 26-hour outage. com8 agents replaces it with measured liveness.

Kept deliberately: router/peer/wake/claude/codex (16-28 call sites; they still do
what com8 does not). Their removal is its own migration."
```

---

## Task 1: The identity record grows four axes

**Files:**
- Modify: `lib/com8.py` — `_do_claim` (~line 1177), `_persist_identities` (~line 362), `_load_identities` (~line 1356)
- Test: `scripts/test-com8-core.sh` (append)

**Interfaces:**
- Produces:
  - `Com8._blank_axes() -> dict` returning `{"workspace": None, "place": {...}, "surface": None, "card": None, "aliases": []}`
  - persisted identity shape: `{claimed_at, kind, home, boxed, aliases, workspace, place, surface, card}`
  - `place` record: `{"kind": "local"|"boxed"|"remote", "device": str|None}`
- Consumes: nothing.

- [ ] **Step 1: Write the failing test**

Append to `scripts/test-com8-core.sh` before its final `echo`:

```bash
echo "== identity record carries the four axes"
"$COMM" com8 claim axistest >/dev/null 2>&1
python3 - "$COMM_STATE/com8/identities.json" <<'PY' && ok "identity has workspace/place/surface/card/aliases keys" || bad "four-axis record"
import json,sys
d=json.load(open(sys.argv[1]))["axistest"]
for k in ("workspace","place","surface","card","aliases"):
    assert k in d, (k, d)
assert d["place"]["kind"]=="local", d["place"]
assert d["aliases"]==[], d["aliases"]
PY
```

- [ ] **Step 2: Run it to verify it fails**

```bash
./scripts/test-com8-core.sh 2>&1 | grep -E 'four-axis|pass='
```
Expected: `FAIL four-axis record`.

- [ ] **Step 3: Implement the blank axes and persist them**

In `lib/com8.py`, add the helper method to the `Com8` class (place it directly above `_do_claim`):

```python
    def _blank_axes(self, boxed=False):
        """The four axes, empty. Every identity carries them from birth so a
        later writer never has to remember to create them (the registry law:
        a field that needs a separate remembered write dies)."""
        return {
            "workspace": None,                       # {path, ref, branch, worktree}
            "place": {"kind": "boxed" if boxed else "local", "device": None},
            "surface": None,                         # {driver, handle, state, measured_at}
            "card": None,                            # {what, ask_me_for, derived, updated}
            "aliases": [],                           # durable role names for this identity
        }
```

In `_do_claim`, both the boxed and unboxed branches build `ent = {...}`. Add the axes to each by merging:

```python
            ent = {"sock": sock, "claimed_at": time.time(), "_srv": srv,
                   "kind": "local"}
            ent.update(self._blank_axes())
```

and for the boxed branch:

```python
                ent = {"sock": sock, "claimed_at": time.time(), "_srv": None,
                       "kind": "local", "boxed": True}
                ent.update(self._blank_axes(boxed=True))
```

In `_persist_identities`, extend the persisted projection:

```python
            data = {n: {"claimed_at": e["claimed_at"],
                        "kind": e.get("kind", "local"),
                        "home": e.get("home"),
                        "seat": e.get("seat"),
                        "boxed": e.get("boxed", False),
                        "aliases": e.get("aliases") or [],
                        "workspace": e.get("workspace"),
                        "place": e.get("place") or {
                            "kind": "boxed" if e.get("boxed") else "local",
                            "device": e.get("home")},
                        "surface": e.get("surface"),
                        "card": e.get("card")}
                    for n, e in self.identities.items()}
```

In `_load_identities`, restore them after a successful claim (mirroring the existing `seat` restore):

```python
            r = self._do_claim(name, boxed=bool(e.get("boxed")))
            if not r.get("ok"):
                self.log("re-claim failed:", name, r.get("err"))
                continue
            with self.mu:
                if name in self.identities:
                    for k in ("seat", "aliases", "workspace", "place",
                              "surface", "card"):
                        if e.get(k) is not None:
                            self.identities[name][k] = e[k]
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./scripts/test-com8-core.sh 2>&1 | tail -1
```
Expected: `pass=47 fail=0`.

- [ ] **Step 5: Verify backward compatibility with an old-format file**

```bash
T=$(mktemp -d); mkdir -p "$T/sess"
export COMM_STATE="$T/s" COM8_SOCK_DIR="$T/k" COM8_SESSIONS_DIR="$T/sess" COM8_SELF=compat COM8_TICK=1
mkdir -p "$T/s/com8"
printf '{"legacy": {"claimed_at": 1.0, "kind": "local", "home": null}}' > "$T/s/com8/identities.json"
bin/communicate com8 start >/dev/null 2>&1 && sleep 1
bin/communicate com8 agents | grep -q legacy && echo "OK legacy identity loaded" || echo "FAIL"
bin/communicate com8 stop >/dev/null 2>&1; rm -rf "$T"
```
Expected: `OK legacy identity loaded`.

- [ ] **Step 6: Commit**

```bash
git add lib/com8.py scripts/test-com8-core.sh
git -c commit.gpgsign=false commit -m "feat(com8): identity record carries four axes from birth

Every identity now has workspace/place/surface/card/aliases the moment it is
claimed, defaulting to empty. Derived-at-birth, never a separate remembered
write -- the pattern that survived in both repos. place collapses the ad-hoc
kind/home/boxed trio into one typed record. Old identities.json files load
unchanged."
```

---

## Task 2: Workspace — recorded always, worktree opt-in

**Files:**
- Create: `lib/com8_workspace.py`
- Modify: `lib/com8.py` — `_do_claim` signature, `_do_spawn` (~line 852), `op()` dispatch, `cli_call`
- Modify: `lib/com8.sh` (pass-through unchanged; verbs already forwarded)
- Test: `scripts/test-com8-workspace.sh` (new)

**Interfaces:**
- Produces:
  - `com8_workspace.describe(path) -> dict|None` → `{"path": abs, "ref": sha|None, "branch": str|None, "worktree": False}`
  - `com8_workspace.make_worktree(repo, name) -> dict` → same shape with `"worktree": True`; raises `WorkspaceError`
  - `Com8._do_claim(name, boxed=False, cwd=None, worktree=False)`
- Consumes: `_blank_axes()` from Task 1.

- [ ] **Step 1: Write the failing test**

Create `scripts/test-com8-workspace.sh`:

```bash
#!/usr/bin/env bash
# Workspace axis: recorded always (path + git ref + branch), worktree opt-in.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM="$HERE/bin/communicate"
T="$(mktemp -d /tmp/com8-ws.XXXXXX)"
export COMM_STATE="$T/state" COM8_SOCK_DIR="$T/socks" COM8_SESSIONS_DIR="$T/sess"
export COM8_SELF=wshost COM8_TICK=1
mkdir -p "$COM8_SESSIONS_DIR"
pass=0; fail=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad(){ fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
cleanup(){ "$COMM" com8 stop >/dev/null 2>&1||true; rm -rf "$T"; }
trap cleanup EXIT
ID="$COMM_STATE/com8/identities.json"

# a real git repo to point at
REPO="$T/repo"; mkdir -p "$REPO"; cd "$REPO"
git init -q .; git config user.email t@t; git config user.name t
echo hello > README.md; git add README.md
git -c commit.gpgsign=false commit -qm "first"
SHA="$(git rev-parse --short HEAD)"; BR="$(git rev-parse --abbrev-ref HEAD)"
cd "$HERE"

"$COMM" com8 start >/dev/null 2>&1

echo "== a git workspace is recorded with ref + branch"
"$COMM" com8 claim ws1 --cwd "$REPO" >/dev/null 2>&1
python3 - "$ID" "$REPO" "$SHA" "$BR" <<'PY' && ok "git workspace recorded (path/ref/branch)" || bad "git workspace record"
import json,sys,os
d=json.load(open(sys.argv[1]))["ws1"]["workspace"]
assert d and os.path.realpath(d["path"])==os.path.realpath(sys.argv[2]), d
assert d["ref"].startswith(sys.argv[3]), d
assert d["branch"]==sys.argv[4], d
assert d["worktree"] is False, d
PY

echo "== a non-git directory still records its path (no ref)"
mkdir -p "$T/plain"
"$COMM" com8 claim ws2 --cwd "$T/plain" >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "non-git workspace records path, ref=None" || bad "non-git workspace"
import json,sys
d=json.load(open(sys.argv[1]))["ws2"]["workspace"]
assert d["path"].endswith("/plain"), d
assert d["ref"] is None and d["branch"] is None, d
PY

echo "== no --cwd means no workspace (never invented)"
"$COMM" com8 claim ws3 >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "workspace stays null when not given" || bad "workspace invented"
import json,sys
assert json.load(open(sys.argv[1]))["ws3"]["workspace"] is None
PY

echo "== --worktree creates a real worktree on its own branch"
"$COMM" com8 claim ws4 --cwd "$REPO" --worktree >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "worktree recorded with worktree:true" || bad "worktree record"
import json,sys,os
d=json.load(open(sys.argv[1]))["ws4"]["workspace"]
assert d["worktree"] is True, d
assert d["branch"]=="com8/ws4", d
assert os.path.isdir(d["path"]), d
PY
( cd "$REPO" && git worktree list | grep -q 'com8/ws4' ) && ok "git agrees the worktree exists" || bad "git worktree list"

echo "== a second agent on the same repo gets its own worktree"
"$COMM" com8 claim ws5 --cwd "$REPO" --worktree >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "two worktree agents do not collide" || bad "worktree collision"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["ws4"]["workspace"]["path"]!=d["ws5"]["workspace"]["path"]
assert d["ws5"]["workspace"]["branch"]=="com8/ws5"
PY

"$COMM" com8 stop >/dev/null 2>&1
echo; echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
```

```bash
chmod +x scripts/test-com8-workspace.sh
```

- [ ] **Step 2: Run it to verify it fails**

```bash
./scripts/test-com8-workspace.sh 2>&1 | tail -3
```
Expected: failures — `--cwd` is not a recognised flag yet.

- [ ] **Step 3: Implement the workspace module**

Create `lib/com8_workspace.py`:

```python
#!/usr/bin/env python3
"""com8_workspace — the workspace axis: what material an agent works on.

A workspace is a directory, plus (when it is a git repo) the commit and branch
it was at. Recording it costs one subprocess and makes three things possible
that are impossible without it: restarting an agent where it was, moving one to
another machine honestly, and telling two agents apart on one repo.

Pure functions over paths — no daemon state, no imports from com8.py.
"""
import os
import subprocess


class WorkspaceError(Exception):
    pass


def _git(repo, *args, timeout=10):
    """Run a git command in `repo`; return stdout stripped, or None."""
    try:
        r = subprocess.run(("git", "-C", repo) + args, capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def describe(path):
    """{path, ref, branch, worktree} for a directory, or None if it is absent.
    ref/branch are None when the path is not inside a git repo — a workspace
    does not have to be a repo (a data corpus is a workspace too)."""
    if not path:
        return None
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(p):
        raise WorkspaceError("no such directory: %s" % p)
    top = _git(p, "rev-parse", "--show-toplevel")
    if not top:
        return {"path": p, "ref": None, "branch": None, "worktree": False}
    ref = _git(p, "rev-parse", "HEAD")
    branch = _git(p, "rev-parse", "--abbrev-ref", "HEAD")
    inside = _git(p, "rev-parse", "--is-inside-work-tree")
    return {"path": p, "ref": ref,
            "branch": None if branch == "HEAD" else branch,
            "worktree": bool(inside) and os.path.isfile(os.path.join(p, ".git"))}


def make_worktree(repo, name, base=None):
    """Create `<repo>-worktrees/<name>` on branch `com8/<name>`. Idempotent:
    an existing worktree for that branch is adopted, not duplicated."""
    src = os.path.abspath(os.path.expanduser(repo))
    top = _git(src, "rev-parse", "--show-toplevel")
    if not top:
        raise WorkspaceError("--worktree needs a git repo: %s" % src)
    branch = "com8/%s" % name
    dest = os.path.join(os.path.dirname(top),
                        "%s-worktrees" % os.path.basename(top), name)
    if os.path.isdir(dest):
        return describe(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    args = ["worktree", "add"]
    if _git(top, "rev-parse", "--verify", branch) is None:
        args += ["-b", branch]
    args.append(dest)
    if base:
        args.append(base)
    if _git(top, *args, timeout=60) is None:
        raise WorkspaceError("git worktree add failed for %s" % dest)
    return describe(dest)
```

- [ ] **Step 4: Wire it into claim**

In `lib/com8.py`, import it beside the seat driver (near line 52):

```python
import com8_seat  # the seat plane (tmux driver)
import com8_workspace  # the workspace axis (git interrogation, worktrees)
```

Change `_do_claim`'s signature and record the workspace after the axes are set (in **both** the boxed and unboxed branches, just before `self._persist_identities()`):

```python
    def _do_claim(self, name, boxed=False, cwd=None, worktree=False):
```

```python
            if cwd:
                try:
                    ws = (com8_workspace.make_worktree(cwd, name) if worktree
                          else com8_workspace.describe(cwd))
                    with self.mu:
                        self.identities[name]["workspace"] = ws
                except com8_workspace.WorkspaceError as e:
                    self.log("workspace not recorded for", name, ":", e)
```

In `op()`, forward the new arguments:

```python
        if op == "claim":
            return self._do_claim(req.get("name", ""),
                                  boxed=bool(req.get("boxed")),
                                  cwd=req.get("cwd"),
                                  worktree=bool(req.get("worktree")))
```

In `cli_call`'s `claim`/`release` handler, parse the flags (replace the flag-stripping block):

```python
    if op in ("claim", "release"):
        boxed = "--boxed" in args
        worktree = "--worktree" in args
        cwd = None
        if "--cwd" in args:
            i = args.index("--cwd")
            try:
                cwd = args[i + 1]
            except IndexError:
                sys.stderr.write("--cwd needs a path\n")
                return 1
            args = args[:i] + args[i + 2:]
        args = [a for a in args if a not in ("--boxed", "--worktree")]
        if not args:
            sys.stderr.write("usage: communicate com8 %s <name> "
                             "[--cwd DIR] [--worktree] [--boxed]\n" % op)
            return 1
        req = {"op": op, "name": args[0]}
        if op == "claim":
            if boxed:
                req["boxed"] = True
            if cwd:
                req["cwd"] = cwd
            if worktree:
                req["worktree"] = True
        r = _call(req, timeout=90)
```

(The rest of that handler — printing the boxed publish lines or `claimed <name>` — stays exactly as it is.)

- [ ] **Step 5: Run the test to verify it passes**

```bash
./scripts/test-com8-workspace.sh 2>&1 | tail -3
```
Expected: `pass=6 fail=0`.

- [ ] **Step 6: Run the full suite**

```bash
for t in core ask link seat seat-link spawn mcp fleet move boxed workspace; do
  printf '%-11s ' "$t:"; ./scripts/test-com8-$t.sh 2>&1 | tail -1
done
```
Expected: all `fail=0`.

- [ ] **Step 7: Commit**

```bash
git add lib/com8_workspace.py lib/com8.py scripts/test-com8-workspace.sh
git -c commit.gpgsign=false commit -m "feat(com8): the workspace axis -- recorded always, worktree opt-in

com8 claim/spawn --cwd records {path, ref, branch} so the fabric knows what
material an agent works on; --worktree additionally creates <repo>-worktrees/
<name> on branch com8/<name> so two agents on one repo cannot collide.

A workspace need not be a repo -- a data corpus records its path with ref=None.
Recording is best-effort and never blocks a claim: an unreadable path logs and
leaves the axis null rather than failing."
```

---

## Task 3: Supervision — spawn records what a restart would need

Today `_do_spawn` knows `cmd` and `cwd`, persists neither, and `com8_seat.py:22` claims otherwise. Consequence: **com8 cannot restart an agent it created.**

**Files:**
- Modify: `lib/com8.py` — `_do_spawn` (~line 852), `op()`, `cli_call`
- Test: `scripts/test-com8-spawn.sh` (append)

**Interfaces:**
- Produces: `supervision` sub-record `{"cmd": str, "cli": str|None, "cwd": str|None, "spawned_at": float}` on the identity; `Com8._do_restart(name) -> dict`
- Consumes: `_blank_axes()` (Task 1), workspace recording (Task 2).

- [ ] **Step 1: Write the failing test**

Append to `scripts/test-com8-spawn.sh` before its final `echo`:

```bash
echo "== spawn records what a restart would need"
"$COMM" com8 spawn sup1 --cwd "$PWD" -- bash --norc --noprofile >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "supervision record persisted (cmd/cwd)" || bad "supervision record"
import json,sys
d=json.load(open(sys.argv[1]))["sup1"]
s=d.get("supervision")
assert s and "bash" in s["cmd"], s
assert s["cwd"], s
assert d["workspace"] is not None, d
PY

echo "== restart brings the agent back on a new seat"
old="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sup1"]["seat"])' "$ID")"
"$COMM" com8 seat kill "$old" >/dev/null 2>&1
sleep 0.5
"$COMM" com8 restart sup1 >/dev/null 2>&1
new="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sup1"]["seat"])' "$ID")"
[ -n "$new" ] && [ "$new" != "$old" ] && ok "restart produced a new live seat ($old -> $new)" || bad "restart ($old -> $new)"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
./scripts/test-com8-spawn.sh 2>&1 | grep -E 'supervision|restart|pass='
```
Expected: `FAIL supervision record`.

- [ ] **Step 3: Record supervision in spawn**

In `_do_spawn`, after the seat is bound and before the return, add:

```python
        with self.mu:
            if name in self.identities:
                self.identities[name]["supervision"] = {
                    "cmd": cmd, "cli": cli, "cwd": cwd,
                    "spawned_at": time.time()}
        self._persist_identities()
```

Change the signature to accept `cli` so the record is honest about which CLI was meant:

```python
    def _do_spawn(self, name, cmd, cwd=None, adopt=False, boot_wait=25, cli=None):
```

and in `op()`:

```python
        if op == "spawn":
            return self._do_spawn(req.get("name", ""), req.get("cmd", ""),
                                  cwd=req.get("cwd"),
                                  adopt=bool(req.get("adopt")),
                                  cli=req.get("cli"))
```

Have spawn record the workspace too, by claiming with the cwd — change the claim call inside `_do_spawn`:

```python
        r = self._do_claim(name, cwd=cwd, worktree=worktree)
```

with `worktree=False` added to the signature (`def _do_spawn(self, name, cmd, cwd=None, adopt=False, boot_wait=25, cli=None, worktree=False)`) and forwarded from `op()` as `worktree=bool(req.get("worktree"))`.

Add `supervision` to `_persist_identities`'s projection and to the `_load_identities` restore list (both from Task 1):

```python
                        "supervision": e.get("supervision"),
```
```python
                    for k in ("seat", "aliases", "workspace", "place",
                              "surface", "card", "supervision"):
```

- [ ] **Step 4: Implement restart**

Add to the `Com8` class, directly after `_do_spawn`:

```python
    def _do_restart(self, name):
        """Bring a spawned agent back using the supervision record. com8 is not
        a process supervisor (deliberately) -- it stores what a restart WOULD
        need and performs one only when asked."""
        with self.mu:
            ent = dict(self.identities.get(name) or {})
        sup = ent.get("supervision")
        if not sup or not sup.get("cmd"):
            return {"ok": False, "err": "%s has no supervision record "
                    "(was it created with com8 spawn?)" % name}
        ws = ent.get("workspace") or {}
        cwd = sup.get("cwd") or ws.get("path")
        old = ent.get("seat")
        if old:
            try:
                self._do_seat("kill", {"seat": old})
            except Exception as e:
                self.log("restart: could not kill old seat", old, e)
        sp = self._do_seat("spawn", {"cmd": sup["cmd"], "cwd": cwd, "name": name})
        if not sp.get("ok"):
            return {"ok": False, "err": "seat spawn: %s" % sp.get("err")}
        self._do_seat_bind(sp["seat"], name)
        self.log("restarted", name, "on seat", sp["seat"])
        return {"ok": True, "name": name, "seat": sp["seat"],
                "previous": old, "cmd": sup["cmd"]}
```

Dispatch it in `op()` beside spawn:

```python
        if op == "restart":
            return self._do_restart(req.get("name", ""))
```

Add a CLI handler in `cli_call` (place beside the `premove` handler):

```python
    if op == "restart":
        if not args:
            sys.stderr.write("usage: communicate com8 restart <name>\n")
            return 1
        r = _call({"op": "restart", "name": args[0]}, timeout=60)
        if r.get("ok"):
            print("restarted %s on %s" % (r["name"], r["seat"]))
            return 0
        sys.stderr.write((r.get("err") or "failed") + "\n")
        return 1
```

Add `restart` to the verb list in `lib/com8.sh`'s pass-through case and to its usage string.

- [ ] **Step 5: Run the test to verify it passes**

```bash
./scripts/test-com8-spawn.sh 2>&1 | tail -3
```
Expected: `pass=12 fail=0`.

- [ ] **Step 6: Commit**

```bash
git add lib/com8.py lib/com8.sh scripts/test-com8-spawn.sh
git -c commit.gpgsign=false commit -m "feat(com8): supervision record + restart -- com8 can bring back what it spawned

spawn now persists {cmd, cli, cwd, spawned_at} and records the workspace, so a
crashed or killed agent can be restarted where it was. com8 remains NOT a
process supervisor: it stores what a restart would need and performs one only
when asked. com8_seat.py's docstring claimed this already existed; now it does."
```

---

## Task 4: move carries the workspace, or refuses

Today `move` rsyncs the transcript, `mkdir -p`s the project directory **empty**, and tells the agent to `cd` into it. The mind arrives; the world does not.

**Files:**
- Modify: `lib/com8.py` — `_move_run` (~line 2704)
- Test: `scripts/test-com8-move.sh` (append)

**Interfaces:**
- Consumes: `workspace` (Task 2), `com8_workspace.describe` (Task 2).
- Produces: `_move_run` gains `require_workspace` behaviour; report lines include a `workspace:` line.

- [ ] **Step 1: Write the failing test**

Append to `scripts/test-com8-move.sh` before its final `echo`:

```bash
echo "== move refuses when the target has no matching workspace"
python3 - <<'PYEOF' && ok "move refuses a missing target workspace" || bad "move workspace gate"
import sys; sys.path.insert(0, "lib")
import com8
com8._find_transcript = lambda name: None
# target reports the workspace path does NOT exist (exit 1 from the probe)
def fake_ssh(target, script, timeout=30):
    if "WSCHECK" in script:
        return (0, "WS:missing", "")
    return (0, "H:/home/u\nC:/usr/bin/communicate\nS:/home/u/.st", "")
com8._ssh_run = fake_ssh
def caller(req):
    op = req.get("op")
    if op == "premove":
        return {"ok": True, "live": False, "mailbox": "/nonexistent",
                "lines": 0, "cursor": 0,
                "workspace": {"path": "/src/proj", "ref": "abc", "branch": "main",
                              "worktree": False}}
    if op == "status":
        return {"ok": True, "links": {"dev": {"addr": "u@dev"}}}
    return {"ok": True}
r = com8._move_run(caller, "agent1", "dev", addr="u@dev")
assert r.get("ok") is False, r
assert "workspace" in (r.get("err") or "").lower(), r
PYEOF

echo "== --allow-missing-workspace proceeds with an explicit warning"
python3 - <<'PYEOF' && ok "--allow-missing-workspace proceeds and warns" || bad "workspace override"
import sys; sys.path.insert(0, "lib")
import com8
com8._find_transcript = lambda name: None
com8._ssh_run = lambda t, s, timeout=30: (0, "WS:missing", "") if "WSCHECK" in s \
    else (0, "H:/home/u\nC:/usr/bin/communicate\nS:/home/u/.st", "")
def caller(req):
    op = req.get("op")
    if op == "premove":
        return {"ok": True, "live": False, "mailbox": "/nonexistent", "lines": 0,
                "cursor": 0, "workspace": {"path": "/src/proj", "ref": "abc",
                                           "branch": "main", "worktree": False}}
    if op == "status":
        return {"ok": True, "links": {"dev": {"addr": "u@dev"}}}
    return {"ok": True}
r = com8._move_run(caller, "agent1", "dev", addr="u@dev", allow_missing_workspace=True)
assert r.get("ok"), r
assert any("workspace" in l.lower() for l in r.get("lines") or []), r
PYEOF
```

- [ ] **Step 2: Run it to verify it fails**

```bash
./scripts/test-com8-move.sh 2>&1 | grep -E 'workspace|pass='
```
Expected: `FAIL move workspace gate`.

- [ ] **Step 3: Implement the workspace check**

In `_move_run`, add the parameter:

```python
def _move_run(caller, name, dev, addr=None, as_name=None, spawn=False,
              fork=False, dry=False, allow_missing_workspace=False):
```

After the target probe resolves `rhome`/`rcomm`/`rstate` and before the transcript rsync, insert:

```python
    # The workspace is the material the agent works on. Moving the mind without
    # the world produces an agent with a complete memory of a repository that
    # does not exist on the target -- so check, and refuse by default.
    ws = pre.get("workspace") or {}
    ws_path = ws.get("path")
    ws_note = None
    if ws_path:
        wpath = ws_path
        home = os.path.expanduser("~")
        if wpath.startswith(home) and rhome != home:
            wpath = rhome + wpath[len(home):]
        rc, wout, _ = _ssh_run(addr, "WSCHECK; if [ -d %s ]; then "
                                     "printf 'WS:present\\n'; else printf 'WS:missing\\n'; fi"
                               % _shq(wpath))
        present = "WS:present" in (wout or "")
        if not present and not allow_missing_workspace:
            return {"ok": False, "lines": report,
                    "err": ("target %s has no workspace at %s -- the agent would "
                            "arrive with a memory of a repo that is not there. "
                            "Clone/checkout it there first, or pass "
                            "--allow-missing-workspace." % (dev, wpath))}
        if not present:
            ws_note = ("workspace : MISSING on %s (%s) -- proceeding by request"
                       % (dev, wpath))
        else:
            ws_note = "workspace : %s%s" % (
                wpath, (" @ %s" % ws["ref"][:8]) if ws.get("ref") else "")
```

In the dry-run block, add the workspace line:

```python
        report.append("workspace : %s" % (ws_path or "(none recorded)"))
```

And in the success report, after the transcript line:

```python
    if ws_note:
        report.append("  " + ws_note)
```

Thread the flag through `_cli_move` (parse `--allow-missing-workspace` alongside `--fork`) and through the daemon `move` op:

```python
        if op == "move":
            return _move_run(self.op, req.get("name", ""), req.get("device", ""),
                             addr=req.get("addr"), as_name=req.get("as"),
                             spawn=bool(req.get("spawn")),
                             fork=bool(req.get("fork")),
                             dry=bool(req.get("dry_run")),
                             allow_missing_workspace=bool(
                                 req.get("allow_missing_workspace")))
```

Also add `"workspace": ent.get("workspace")` to `_do_premove`'s return dict so the mover can see it.

- [ ] **Step 4: Run the test to verify it passes**

```bash
./scripts/test-com8-move.sh 2>&1 | tail -3
```
Expected: `pass=20 fail=0`.

- [ ] **Step 5: Commit**

```bash
git add lib/com8.py scripts/test-com8-move.sh
git -c commit.gpgsign=false commit -m "fix(com8): move carries the workspace, or refuses

move rsynced the transcript, created the project dir EMPTY, and told the agent
to cd into it -- delivering a complete memory of a repository that does not
exist on the target. move now checks the target for the recorded workspace path
and refuses by default, with --allow-missing-workspace as the explicit override
(which warns in the report rather than proceeding silently)."
```

---

## Task 5: The card — derived at birth, agent-updatable

**Files:**
- Modify: `lib/com8.py` — new `_derive_card`, `_do_describe`; `_do_claim`; `op()`; `cli_call`
- Modify: `lib/com8.sh` — add `describe` to the verb list
- Test: `scripts/test-com8-card.sh` (new)

**Interfaces:**
- Produces:
  - `Com8._derive_card(name, cwd) -> dict` → `{"what", "ask_me_for", "derived": True, "updated": float}`
  - `Com8._do_describe(name, what=None, ask_me_for=None) -> dict`
  - `agents_list` entries gain `"card"`
- Consumes: workspace (Task 2).

- [ ] **Step 1: Write the failing test**

Create `scripts/test-com8-card.sh`:

```bash
#!/usr/bin/env bash
# The card: derived as a side effect of claiming (never a remembered write),
# overridable by the agent or the human, and surfaced in the roster.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM="$HERE/bin/communicate"
T="$(mktemp -d /tmp/com8-card.XXXXXX)"
export COMM_STATE="$T/state" COM8_SOCK_DIR="$T/socks" COM8_SESSIONS_DIR="$T/sess"
export COM8_SELF=cardhost COM8_TICK=1
mkdir -p "$COM8_SESSIONS_DIR"
pass=0; fail=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad(){ fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
cleanup(){ "$COMM" com8 stop >/dev/null 2>&1||true; rm -rf "$T"; }
trap cleanup EXIT
ID="$COMM_STATE/com8/identities.json"

PROJ="$T/physlean"; mkdir -p "$PROJ"
printf '# PhysLean\n\nA Lean 4 formalisation of physics.\n' > "$PROJ/AGENTS.md"

"$COMM" com8 start >/dev/null 2>&1

echo "== a card is derived from the workspace, with no extra step"
"$COMM" com8 claim prover --cwd "$PROJ" >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "card derived at claim (marked derived:true)" || bad "card derivation"
import json,sys
c=json.load(open(sys.argv[1]))["prover"]["card"]
assert c and c["derived"] is True, c
assert "PhysLean" in c["what"], c
PY

echo "== describe overrides it and marks it authored"
"$COMM" com8 describe prover --what "proof automation over PhysLean" \
        --ask-me-for "tactic suggestions, proof state" >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "describe overrides and clears derived flag" || bad "describe override"
import json,sys
c=json.load(open(sys.argv[1]))["prover"]["card"]
assert c["what"]=="proof automation over PhysLean", c
assert c["ask_me_for"]=="tactic suggestions, proof state", c
assert c["derived"] is False, c
PY

echo "== a claim with no workspace still gets a card (never absent)"
"$COMM" com8 claim bare >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "cardless claim still carries a card stub" || bad "card stub"
import json,sys
c=json.load(open(sys.argv[1]))["bare"]["card"]
assert c is not None and "what" in c, c
PY

echo "== the roster shows the card"
"$COMM" com8 agents --json 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin)
a={x["name"]:x for x in d["agents"]}
assert a["prover"]["card"]["what"]=="proof automation over PhysLean", a["prover"]
' && ok "agents_list carries the card" || bad "roster card"

echo "== describe refuses an unknown identity"
"$COMM" com8 describe ghost --what "x" >/dev/null 2>&1 && bad "describe accepted a ghost" || ok "describe refuses unknown identity"

"$COMM" com8 stop >/dev/null 2>&1
echo; echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
```

```bash
chmod +x scripts/test-com8-card.sh
```

- [ ] **Step 2: Run it to verify it fails**

```bash
./scripts/test-com8-card.sh 2>&1 | tail -3
```
Expected: failures — `card` is null and `describe` does not exist.

- [ ] **Step 3: Implement derivation and describe**

Add to the `Com8` class, above `_do_claim`:

```python
    _CARD_DOC_NAMES = ("AGENTS.md", "CLAUDE.md", "README.md")

    def _derive_card(self, name, cwd):
        """A first-guess card, from what the fabric already knows. Derivation is
        the point: a field that needs a separate remembered write dies (every
        hand-curated registry in both repos did). A human or the agent itself
        can overwrite it later via `describe`."""
        what = None
        if cwd:
            for fn in self._CARD_DOC_NAMES:
                p = os.path.join(cwd, fn)
                if not os.path.isfile(p):
                    continue
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        head, body = None, []
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("#") and head is None:
                                head = line.lstrip("#").strip()
                                continue
                            if head is not None and not line.startswith("#"):
                                body.append(line)
                                break
                    if head:
                        what = ("%s — %s" % (head, body[0])) if body else head
                        break
                except OSError:
                    continue
            if not what:
                what = "works in %s" % os.path.basename(cwd.rstrip("/"))
        return {"what": what or "no description yet — set one with `com8 describe`",
                "ask_me_for": "", "derived": True, "updated": time.time()}

    def _do_describe(self, name, what=None, ask_me_for=None):
        """Author a card. This is the write an AGENT makes about itself."""
        with self.mu:
            ent = self.identities.get(name)
        if not ent or ent.get("kind") != "local":
            return {"ok": False, "err": "not a local identity: %s" % name}
        card = dict(ent.get("card") or {})
        if what is not None:
            card["what"] = what
        if ask_me_for is not None:
            card["ask_me_for"] = ask_me_for
        card["derived"] = False
        card["updated"] = time.time()
        with self.mu:
            self.identities[name]["card"] = card
        self._persist_identities()
        self.log("card authored for", name)
        return {"ok": True, "name": name, "card": card}
```

In `_do_claim`, after the workspace block, derive the card:

```python
            with self.mu:
                if name in self.identities and not self.identities[name].get("card"):
                    self.identities[name]["card"] = self._derive_card(name, cwd)
```

Dispatch in `op()`:

```python
        if op == "describe":
            return self._do_describe(req.get("name", ""), what=req.get("what"),
                                     ask_me_for=req.get("ask_me_for"))
```

Surface it in `_do_agents` — add to the appended dict:

```python
                "card": e.get("card"),
```

CLI handler in `cli_call`:

```python
    if op == "describe":
        what = askfor = None
        if "--what" in args:
            what = args[args.index("--what") + 1]
        if "--ask-me-for" in args:
            askfor = args[args.index("--ask-me-for") + 1]
        names = [a for a in args if not a.startswith("--")
                 and a not in (what, askfor)]
        if not names or (what is None and askfor is None):
            sys.stderr.write("usage: communicate com8 describe <name> "
                             "[--what TEXT] [--ask-me-for TEXT]\n")
            return 1
        r = _call({"op": "describe", "name": names[0], "what": what,
                   "ask_me_for": askfor})
        if r.get("ok"):
            print("described %s: %s" % (r["name"], r["card"]["what"]))
            return 0
        sys.stderr.write((r.get("err") or "failed") + "\n")
        return 1
```

Add `describe` to `lib/com8.sh`'s pass-through verb list and usage string.

- [ ] **Step 4: Run the test to verify it passes**

```bash
./scripts/test-com8-card.sh 2>&1 | tail -3
```
Expected: `pass=5 fail=0`.

- [ ] **Step 5: Commit**

```bash
git add lib/com8.py lib/com8.sh scripts/test-com8-card.sh
git -c commit.gpgsign=false commit -m "feat(com8): the capability card -- derived at birth, authored on demand

Every claim now derives a card (what this is) from AGENTS.md/CLAUDE.md/README.md
in the workspace, or the directory name. Derivation is the design: every
hand-curated registry in both repos died, and every derived one survived.
com8 describe lets an agent or a human author it, which clears the derived flag.
agents_list carries it, so a peer knows WHOM to message before it knows how."
```

---

## Task 6: The surface is measured, not asserted

`seat` is the one roster field never measured. After a reboot the tmux server is gone and every `seat` in the roster is a confident lie — a direct violation of invariant #2.

**Files:**
- Modify: `lib/com8_seat.py` — add `measure()`
- Modify: `lib/com8.py` — `build_status` (~line 2104), `_do_agents` (~line 969)
- Test: `scripts/test-com8-seat.sh` (append)

**Interfaces:**
- Produces: `SeatDriver.measure(seat) -> dict` → `{"driver": "tmux", "handle": str, "state": str, "measured_at": float}`; roster `surface` field.
- Consumes: `_blank_axes()` (Task 1).

- [ ] **Step 1: Write the failing test**

Append to `scripts/test-com8-seat.sh` before its final `echo`:

```bash
echo "== the surface is measured, never asserted"
"$COMM" com8 claim surf >/dev/null 2>&1
S2="$("$COMM" com8 seat spawn 'bash --norc --noprofile' 2>/dev/null)"
"$COMM" com8 seat bind "$S2" surf >/dev/null 2>&1
sleep 1
"$COMM" com8 agents --json 2>/dev/null | python3 -c '
import json,sys
a={x["name"]:x for x in json.load(sys.stdin)["agents"]}
s=a["surf"]["surface"]
assert s and s["driver"]=="tmux" and s["state"] in ("idle","busy","booting"), s
' && ok "a live seat reports a measured surface" || bad "surface measurement"

"$COMM" com8 seat kill "$S2" >/dev/null 2>&1
sleep 1
"$COMM" com8 agents --json 2>/dev/null | python3 -c '
import json,sys
a={x["name"]:x for x in json.load(sys.stdin)["agents"]}
s=a["surf"]["surface"]
assert s["state"]=="dead", s
' && ok "a dead seat reports dead, not a stale handle" || bad "dead surface honesty"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
./scripts/test-com8-seat.sh 2>&1 | grep -E 'surface|pass='
```
Expected: `FAIL surface measurement`.

- [ ] **Step 3: Implement measure()**

Add to `SeatDriver` in `lib/com8_seat.py`, after `state()`:

```python
    def measure(self, seat):
        """The surface axis, measured. Never assert a handle you have not just
        checked -- after a reboot the tmux server is gone and every stored pane
        id is a lie."""
        if not seat:
            return None
        try:
            st = self.state(seat)
        except SeatError as e:
            st = "dead"
            self.log("measure failed for", seat, e)
        return {"driver": "tmux", "handle": seat, "state": st,
                "measured_at": time.time()}
```

In `lib/com8.py`'s `build_status`, replace the raw `"seat": seat` emission with a measured surface (do this in both the boxed early-return block and the main local block):

```python
                "seat": seat,
                "surface": (self._seat_drv().measure(seat) if seat else None),
```

Guard it so a missing tmux never breaks `status` — wrap the driver call:

```python
    def _measure_surface(self, seat):
        if not seat:
            return None
        try:
            return self._seat_drv().measure(seat)
        except Exception as e:
            self.log("surface measure failed:", seat, e)
            return {"driver": "tmux", "handle": seat, "state": "unknown",
                    "measured_at": time.time()}
```

and use `self._measure_surface(seat)` at both sites plus in `_do_agents`:

```python
                "surface": self._measure_surface(e.get("seat")),
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
./scripts/test-com8-seat.sh 2>&1 | tail -3
```
Expected: `pass=12 fail=0`.

- [ ] **Step 5: Verify status still works with no tmux**

```bash
T=$(mktemp -d); mkdir -p "$T/sess" "$T/bin"
PATH="$T/bin:/usr/bin:/bin" COMM_STATE="$T/s" COM8_SOCK_DIR="$T/k" \
  COM8_SESSIONS_DIR="$T/sess" COM8_SELF=notmux COM8_TICK=1 \
  bash -c 'bin/communicate com8 start >/dev/null 2>&1; sleep 1;
           bin/communicate com8 claim x >/dev/null 2>&1;
           bin/communicate com8 agents; bin/communicate com8 stop >/dev/null 2>&1'
rm -rf "$T"
```
Expected: the roster prints; no traceback.

- [ ] **Step 6: Commit**

```bash
git add lib/com8_seat.py lib/com8.py scripts/test-com8-seat.sh
git -c commit.gpgsign=false commit -m "fix(com8): the surface axis is measured, not asserted

seat was the one roster field never measured -- after a reboot the tmux server
is gone and every stored pane id was advertised as fact, violating the founding
invariant (never advertise what you have not measured). The roster now carries
a measured surface {driver, handle, state, measured_at}, and a dead seat says
dead. Measurement failures degrade to state:unknown rather than breaking status."
```

---

## Task 7: Teach the agents — instructions, tools, and descriptions

The user's note: *"since all of this would be agent driven/created, these kinds of instructions for how to populate this identity json would also have to be in the plugin/tools/skills."* Correct — a field agents are expected to write must be taught at the point of use.

**Files:**
- Modify: `packages/com8/src/server.ts` — `INSTRUCTIONS`, new tools appended
- Modify: `packages/com8/test/mcp-smoke.mjs`
- Test: `scripts/test-com8-mcp.sh` (runs the smoke)

**Interfaces:**
- Consumes: daemon ops `describe` (Task 5), `restart` (Task 3), workspace/card fields.
- Produces: MCP tools `describe`, `restart`; extended `spawn`/`claim` schemas with `cwd`/`worktree`.

- [ ] **Step 1: Write the failing test**

In `packages/com8/test/mcp-smoke.mjs`, after the existing instructions assertion, add:

```js
// The card is agent-authored, so the orientation must teach it.
if (instr && /describe/.test(instr) && /workspace/i.test(instr))
  ok("instructions teach the card and the workspace");
else bad("instructions omit card/workspace guidance");

// The new verbs must be reachable.
const names2 = (await client.listTools()).tools.map((t) => t.name);
if (names2.includes("describe") && names2.includes("restart"))
  ok("describe + restart are exposed as tools");
else bad("missing describe/restart: " + names2.join(","));
```

- [ ] **Step 2: Run it to verify it fails**

```bash
./scripts/test-com8-mcp.sh 2>&1 | tail -5
```
Expected: `FAIL instructions omit card/workspace guidance`.

- [ ] **Step 3: Extend the instructions**

In `packages/com8/src/server.ts`, insert into `INSTRUCTIONS` before the final "Not exposed here" paragraph:

```
YOUR IDENTITY HAS FOUR PARTS, and you can fill two of them in:
• workspace — the directory and git ref you work on. Set it when you are created
  (spawn/claim take cwd); the fabric records the commit so you can be restarted
  or relocated honestly.
• card — one line saying what you ARE and what to ask you for. It is derived from
  your workspace when you are created; if it is wrong or empty, fix it with
  describe. This is how other agents find you: they read cards to decide whom to
  message before they know how. A good card is specific ("proof automation over
  the PhysLean corpus; ask me for tactic suggestions and proof state"), not
  generic ("a helpful assistant").
The other two — place (where you run) and surface (your terminal, if any) — are
measured for you; never assert them.
```

- [ ] **Step 4: Add the tools**

Append to the `TOOLS` array (never reorder — the list must stay byte-stable):

```ts
  {
    name: "describe",
    description:
      "Set your own card: what you are and what peers should ask you for. Other agents read cards to choose whom to message, so be specific and honest. Derived cards are a first guess — replace yours the first time you know better.",
    schema: {
      name: z.string().describe("the identity to describe (usually your own)"),
      what: z.string().optional(),
      ask_me_for: z.string().optional(),
    },
    run: (a) => call({ op: "describe", name: a.name, what: a.what, ask_me_for: a.ask_me_for }),
  },
  {
    name: "restart",
    description:
      "Bring a spawned agent back using its supervision record (the command, cli and cwd captured at spawn). Use when a seat died or the agent is wedged; it does not lose the mailbox.",
    schema: { name: z.string() },
    run: (a) => call({ op: "restart", name: a.name }, { timeoutMs: 60_000 }),
  },
```

Extend `claim`'s schema so an agent can bind its workspace:

```ts
    schema: {
      name: z.string(),
      cwd: z.string().optional().describe("the directory this identity works in — recorded with its git ref"),
      worktree: z.boolean().optional().describe("create a dedicated git worktree + branch for this agent"),
    },
    run: (a) => call({ op: "claim", name: a.name, cwd: a.cwd, worktree: !!a.worktree }, { timeoutMs: 90_000 }),
```

and `spawn`'s to forward `worktree`:

```ts
      return call({ op: "spawn", name: a.name, cmd, cwd: a.cwd, adopt,
                    cli: a.cli, worktree: !!a.worktree },
                  { timeoutMs: 90_000 });
```
with `worktree: z.boolean().optional()` added to its schema.

- [ ] **Step 5: Rebuild and run the test**

```bash
( cd packages/com8 && npm run build ) && ./scripts/test-com8-mcp.sh 2>&1 | tail -6
```
Expected: `pass=9 fail=0`.

- [ ] **Step 6: Run the entire suite**

```bash
python3 scripts/test-com8-seat-unit.py && \
for t in core ask link seat seat-link spawn mcp fleet move boxed workspace card; do
  printf '%-11s ' "$t:"; ./scripts/test-com8-$t.sh 2>&1 | tail -1
done
```
Expected: every line `fail=0`.

- [ ] **Step 7: Commit and push**

```bash
git add packages/com8/src/server.ts packages/com8/test/mcp-smoke.mjs
git -c commit.gpgsign=false commit -m "feat(com8/mcp): teach the four axes -- describe + restart tools, extended orientation

A field agents are expected to write must be taught where they work. The
orientation now explains that an identity has four parts, that the agent can
fill in two (workspace, card), and that place/surface are measured for it.
describe and restart are exposed; claim/spawn take cwd and worktree."
git -c credential.helper='!gh auth git-credential' push origin main
```

---

## Self-Review

**1. Spec coverage.** Each ontology row maps to a task: identity+aliases → Task 1; workspace → Task 2 (+ Task 4 for carriage); place → Task 1 (typed record); surface → Task 6 (measured); card → Task 5 (+ Task 7 for teaching). Supervision, which the ontology implies by "restart is possible," is Task 3. The removal the user asked to think through is Task 0.

**2. Placeholder scan.** No TBDs. Every code step carries real code; every test step carries a real assertion and an expected result.

**3. Type consistency.** `workspace` is `{path, ref, branch, worktree}` in `com8_workspace.describe`, in `_do_claim`, in `_do_premove`'s return, and in `_move_run`'s check. `card` is `{what, ask_me_for, derived, updated}` in `_derive_card`, `_do_describe`, `_do_agents`, and the MCP `describe` tool. `surface` is `{driver, handle, state, measured_at}` in `SeatDriver.measure`, `_measure_surface`, `build_status`, and `_do_agents`. `supervision` is `{cmd, cli, cwd, spawned_at}` in `_do_spawn`, `_do_restart`, and both persistence sites.

**Known gaps, deliberately deferred to a later plan** (not silent omissions):
- The **socket-driven surface driver** (nvim msgpack-RPC / Lean proof state). Task 6 makes `surface` a measured record with a `driver` field, which is the seam a second driver plugs into — but no second driver is built here.
- **Cards travelling per-grant** across fleets. The card exists and is local; attaching it to `grant` is fleet work.
- **The plugin** (hooks + skills). Task 7 teaches through the MCP instructions, which is what `claude mcp add` can deliver; hooks and skills need the plugin package.
- **`com8 restart` on boxed identities** — the supervision record is stored, but restarting a container is out of scope here.
