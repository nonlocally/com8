# Study: anu/COM8 comm, identity, and cross-device layers (evidence base for the agent-fabric design)

Date: 2026-08-15 · Method: subagent deep-read of /Users/aadarwal/COM8 (= QPG-MIT/COM8)
Repo: 513 commits, all self-authored; 100+ PRs. Comm-layer arc: #4 taa/tra/tscale → #7 al/alw
→ #22 agentlog → #25 meshsync → #26 mesh spawn → #43 the anu plugin (`pane`) → #44 MCP →
#49 conduct/see a team → #51 reply channel → #52 contained reply (box) → #53/54/55
reliability → #64 working/idle + consult → beam. The center of gravity moved decisively from
`swarm` to `pane`.

## 1. Identity census

| Name/address | Stored where | Scope | Durability | Assigned by |
|---|---|---|---|---|
| `%NN` tmux pane id | tmux server memory | tmux server | dies with pane; not unique across devices; recycled per restart | tmux |
| `@anu_role` (reviewer, worker, ctx-<proj>) | tmux per-pane option | pane | dies with pane | `pane name`/`spawn --as`/`fan --as` (`pane:750`, `:698`); `context` (`context:446`) |
| `@anu_expected_cli` | per-pane option | pane | dies with pane | `pane spawn` (`pane:697`) — powers `booting` |
| `@anu_replyid` | per-pane option | pane | dies with pane; **the reply FILE outlives it** | `pane fan` (`:883`), `consult` (`:1367`), `context` (`:447`) |
| reply id → file | `~/.local/share/anu/pane/replies/<id>` (`pane:112`, `:759`) | host FS, **crosses the box boundary** | survives pane death, tmux restart, reboot; `pane reply gc` | worker writes via `pane reply <id> <text>` (atomic `.part` → `mv`) |
| needs ledger `need_<pid>` | `~/.local/share/anu/notify/` | host | survives; GC'd | `pane call` / watchd |
| watchd FSM `w_<sockhash>_<N>` | `~/.local/share/anu/notify/` | host+socket | survives; GC'd | `pane watchd` |
| `agent-N` (swarm) | `@swarm_panes` window option + `swarms/<id>/agents/agent-N.json` | window / global | option dies with window; JSON survives but **goes stale (lies)** | swarm/mesh |
| `@swarm_id`, swarm name | window option + dir + swarm.json | window/swarm | dir persists | swarm |
| `@agent_panes` CSV | window option | window | dies with window | taa/al/`pane spawn` |
| `@mesh_panes` | window option (`mesh:1213`) | window | dies with window | mesh |
| `@ncn_*` family | window options (`ncn:104-110`) | window | dies with window; documented recovery record | nc/ncn |
| `@nv_sock`/`@nv_pane` | window options (`nv:51-58`) | window | dies with window | nv / pane present |
| mesh device name | `~/.local/share/anu/mesh/devices.json` (tailscale cache), `hosts.json`, `users.json` | global | persists; **the only stable cross-device identity in the system** | Tailscale / `mesh host add` |
| tmux session name | tmux; snapshots in `sessions/<name>` | device | tss/tsr snapshot | human / mesh (`anu-<swarm_id>`) / beam |
| claude session name | the last `{"type":"custom-title"}` record **inside** the transcript jsonl (`bin/beam:46`) | global | **survives beam — the name travels inside the file** | claude `/rename`; `beam send --as` appends a record (`beam:326`) |
| claude session uuid | transcript filename | global | survives beam; **forks** on send | claude |
| mailbox name | `swarms/<id>/mailbox/<agent-N>/NNN.msg` | swarm | persists | swarm |

**No agent has a durable, device-independent address.** `%NN` is the working address and the
least durable name in the table.

## 2. Transport mechanics

**`pane send` — the good path (`pane:380-460`):** read inline/stdin/`-f file`; refuse self;
per-target atomic-mkdir lock (~5 s); sanitize (`tr '\n\r\t' '   '` + strip control bytes);
exit copy-mode; stage literally (`send-keys -l --` + `sleep 0.3`); **submit with a separate
Enter retried up to 4×** ("a welded C-m gets absorbed into the paste and never submits.
Retrying the Enter is safe; re-sending the TEXT could duplicate a submitted prompt, so we
never do"); verify via the `[Pasted Content` composer marker; **exit 2** for
sent-but-unconfirmed. No tmux buffers, no bracketed paste, no file mailbox — keystrokes +
verification.

**`swarm send` — the old path (`swarm:628-662`):** dual channel, both weak. (a) Mailbox
`swarms/<id>/mailbox/<target>/NNN.msg` (`FROM/TO/TIME/---/msg`), sequence via `wc -l` (racy);
**nothing polls it** — read only by explicit `swarm read`. (b) `_swarm_send_keys` =
`tmux send-keys -t "$pane" "$text" C-m` — no `-l`, welded C-m, no verification, no lock —
precisely the failure mode `pane send` fixes. Remote variant shells
`tailscale ssh <device> -- tmux send-keys …`.

**Reply capture:** `pane ask` (`:462-500`) delivers in a child process, then a 1 s/tick turn
loop: while busy → wait with zero clock pressure; only after `sawbusy=1` does a 2-tick settle
end the wait (so the prompt echo isn't returned as the reply); never-busy within 20 s →
return screen + warn. Output = chrome-stripped last 40 lines. `pane wait` adds `--for/--gone`
regex. **`pane reply`/`gather` is the durable channel**: gather prefers `[confirmed]` (reply
file exists), falling back through `[no reply yet — still working]` / `[best-effort screen]`,
with counts and timeout honesty (`:920-944`). **`pane consult`** (`:1340-1385`) is the
cleanest composite: private consultant pane, consultant-level lock, `rm -f` reply file, set
`@anu_replyid`, brief instructs `pane reply <rid> "<answer>"`, `pane wait` 600 s, read file.

**The state classifier — the thing everything stands on (`pane:346-373`):** precedence
`dead → approval → busy → booting → idle`. Key discovery (`:236-243`): the CLI's **title
spinner** is "the ONE working-signal present continuously through a whole turn — including
answer streaming, where the bottom 'esc to interrupt' hint is OVERWRITTEN by the streamed
text." Codex clears the title at idle (glyph ⇒ busy); Claude freezes the last frame, so
detect the glyph *animating* — sampled at jittered intervals `0.11 0.17 0.23 0.29` to defeat
aliasing (`:258-267`); `_animated_ids` batches the sample across all panes in a window
(`:273-281`).

**Failure modes handled:** staged-but-unsubmitted → retries then exit 2 + screen; approval
dialogs via two-signal detection (phrase AND affordance), checked before busy; `pane respond`
**fails closed** (refuses unless classifier says `approval`; never picks an "always allow"
variant, `:594-600`); copy-mode; non-agent target warning; >16000-char warning; Claude
reporting `pane_current_command` as a version string (`:135-158`).

## 3. Cross-device reality

**`pane` has zero device awareness** — no `device:%id` syntax; all targets resolve against
the local tmux socket. What crosses a device boundary:

1. **`beam send` / `session_send` — works, best-designed.** Probe remote HOME/claude/tmux in
   one `bash -lc` round trip; translate `$HOME`-relative project path; compute remote slug
   (verified 37/37); `rsync -az` transcript + subagent sidecar; optionally append a
   `custom-title` record to retitle; remote `tmux new-window` + `send-keys -l --` + separate
   Enter. **Identity that travels: the transcript, its uuid, and its name.** Explicitly
   forks: both copies diverge.
2. **`mesh spawn` (`mesh:1337-1500`):** remote session `anu-$swarm_id`; records both
   `pane_id` (local ssh proxy) and `remote_pane` — but `_swarm_send_keys` **only ever uses
   `.pane_id`**, so remote sends type into the local proxy (works by accident);
   `_swarm_pane_alive` returns 0 unconditionally for non-local devices (`swarm:140`).
3. **`swarm mesh` (`swarm:1453`):** local pane runs `tailscale ssh $device -t '$cmd'`;
   the remote-send path is wrong-by-construction for mesh-spawn agents.
4. **`meshsync`:** background `rsync -az --delete` of the swarm dir, one-way, per-device,
   no conflict handling (last writer destroys). Syncs swarm state, **not** `pane/replies`.
5. **`mesh run <device> <cmd>`** — plain ssh. DESIGN.md (`plugins/anu/DESIGN.md:82-85`):
   "`pane` is on every mesh device, so `mesh run <device> pane send %id …` already drives a
   remote pane today; what's left is ergonomic addressing (`pane send <device>:%id`) and
   cross-device confirmed reply (`gather` ssh-fetches each device's reply file) — needs a
   second device to validate."
6. **`ncn`** — conductor drives an ssh pane by send-keys; recovery state in `@ncn_*`.
7. **`ANU_NV_TCP`** — the editor bridge's explicit off-host transport
   (`NV_SOCK=<host>:<port>`).

**The seams:** (a) `%NN` has no device qualifier; (b) the reply dir is host-local, only ever
mounted into a box, never fetched over ssh; (c) `_classify` requires local tmux; (d) meshsync
syncs the wrong dir, one-way with `--delete`.

**Live evidence:** `~/.local/share/anu/swarms/` is **empty**; viz reports `"swarms": []`
alongside 53 panes / 45 agents. The swarm/mesh distributed layer is effectively dead code;
`pane` + reply files are what runs.

## 4. Permissions reality

**It is always full user SSH. No restricted-key scheme exists anywhere in the repo** —
exhaustive grep for `authorized_keys`, `ForceCommand`, `permitopen`, `command=`, `restrict`,
`no-port-forwarding`, `ssh-keygen` returns zero hits in anu's own code.

- `bin/beam:213`: `-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new
  -o ControlMaster=auto -o ControlPersist=60` — the entire hardening surface; `accept-new` is
  TOFU.
- `mesh sshconfig` (`mesh:606-660`): generates Host/HostName/User blocks; no IdentityFile, no
  per-agent key. Identity: `mesh/users.json` → `$MESH_SSH_USER` → `$USER`.
- Auth delegated wholesale to Tailscale SSH or the user's normal agent/keys. Whatever the
  human can do, any agent that can run bash can do — on every mesh device.
- `beam:270` refuses an expired tailscale node key — the only access-control-shaped check,
  and it's liveness, not permission.

**`box` — the one real containment boundary** (`config/bash/fns/box:60-120`): strips host
credentials *by omission* ("Nothing else crosses from the host — no ssh keys, no gh auth").
Crosses: worktree (+ main repo if linked) at real paths; `~/.local/share/anu/box/claude` →
`/root/.claude` (persistent login, shared); `plugins/anu/bin` → `/opt/anu/bin`;
`$ANU_PANE_DIR` at its real path **RW + shared across all workers** ("trusted-worker model; a
per-run subdir would isolate further"); env `TERM`, `ANTHROPIC_API_KEY` (if set),
`GIT_AUTHOR_*`/`GIT_COMMITTER_*`.

**`anu secrets`** (Infisical wrapper): fixed scope table — `host`, `host.chat`,
`agent.claude`, `agent.codex`, `bootstrap.health` — **per CLI vendor, not per agent
instance**. Real property: durable creds off argv (keychain client secret, 0600 files, stdin,
short-lived minted tokens). Nothing in the comm layer calls it.

## 5. The plugin (`plugins/anu/`)

A zero-dependency stdlib-only Python MCP server (`mcp/server.py`, 470 lines) that is a **pure
argv shim over the `pane` bin** — no duplicated logic, no state. 24 tools: pane_ls, send,
ask, spawn, read, wait, status, state, name, consult, broadcast, fan, gather, reply, respond,
stop, call, focus, present, session_ls, session_send (+ `anu://panes` resource). Timeouts
double-bounded per tool (subprocess cap + the bin's env budget: ask 300/240, consult 660/600,
session_send 300). Refusals live in the shim too (broadcast/gather demand a role/cli scope).
The `instructions` string doubles as the SessionStart hook text. The bin has a superset
(`pane id/whoami/approve/watch/needs/watchd/notify/reply gc/wait --for`), with the
needs/watchd/notify triad deliberately not MCP-exposed (system machinery, not agent-facing).
DESIGN.md: "The bin and the MCP server are two faces of one kernel."

## 6. Architecture judgment

**Core-and-clean (keep, verbatim if possible):** the classifier (`_classify` +
`_state_word`); `pane send`'s deliver-and-verify; the reply channel (transport-independent,
tmux-independent, boundary-crossing, atomic, GC'd); the two-faces rule; `pane consult` (a
complete locked request/response — the template for a routed RPC); the needs ledger + watchd
(a per-tmux-socket singleton daemon with pidfile, atomic-mkdir lock, self-enforcement,
auto-exit — the lifecycle shape a router daemon should reuse); `beam` (probe, path
translation, verified slug, honest snapshot semantics).

**Accreted mess (do not carry forward):** the 2926-line `swarm` fn (8 topologies on the
unverified `send-keys C-m` primitive; state dir empty; only surviving contribution is the
`agent-N` vocabulary and the agents/*.json shape `pane`'s `_resolve_agent` still reads);
three parallel pane registries (`@agent_panes` vs `@swarm_panes` vs `@anu_role`) plus
`@mesh_panes`/`@ncn_*`; mailboxes nobody reads; `agentlog` only wired to `_swarm_send` (no
unified message log); meshsync (one-way `rsync --delete` of the wrong dir); mesh spawn's
dual ids where messaging honors the wrong one.

**The natural integration seam for an external agent router:** `pane` is one bin with a verb
dispatcher, and everything calls it as a subprocess. So: (1) **replace `_resolve` only** —
teach it `<device>:<name>` and return a *route* (local tmux pane / remote pane over the
router / socket peer) rather than a bare `%id`; `_classify` + `cmd_send` become the local
tmux driver, one backend among several. (2) **Adopt `pane/replies/<id>` as the router's
response bus** — already the confirmed-result contract for gather/consult/context, already
crosses the container boundary, already survives the pane. (3) **Own the identity table
`pane` lacks** (durable `device × name → route`), replacing the tmux-option registries and
stale swarm JSON. (4) Reuse the watchd daemon lifecycle. (5) Consume viz/state.json +
`pane needs --json` + `pane status --json` as stable contracts. (6) **Cross-device auth is a
greenfield decision** — no prior art to preserve.

## What anu/COM8 got right (must survive any merge)

1. One command, no fumbling — the substrate owns tmux/Enter/quoting/timing.
2. Verify by observed state, never hope; exit 2 for sent-but-unconfirmed.
3. One classifier, one source of truth — "no second heuristic to drift."
4. A durable confirmed-reply channel beats screen scraping; plain file write, no tmux
   dependency; label results honestly.
5. Fail closed on anything destructive.
6. Escape hatches for text (`-f`, stdin) — inline quoting is the #1 failure.
7. Names for meaning with declared precedence (`%id → next → agent-N → CLI → role`);
   ambiguity is an error.
8. Two faces, one kernel (bin + MCP shim).
9. Small, honest cross-device semantics (beam: "both copies diverge").
10. Notification as a first-class lane (needs ledger, precedence, cooldowns, focus-jump).
11. The container carries the kernel (pane bin + reply dir mounted into every box).

## Sharp edges / mess

1. No durable agent identity — `%NN` is the working address; richer names die with
   pane/window; swarm JSON becomes lies.
2. Three+ overlapping pane registries, no reconciliation.
3. Cross-device pane messaging unformalized (`mesh run <dev> pane send %id` requires knowing
   a remote %id).
4. mesh spawn records `remote_pane` and never uses it.
5. meshsync: one-way `rsync --delete`, wrong dir.
6. Everything is full-privilege SSH; TOFU host keys; secrets scoped per vendor not per agent.
7. The shared reply dir is a trusted-worker free-for-all across boxes.
8. Screen-scraping is still the fallback everywhere (anchored chrome regexes tracking TUI
   chrome forever).
9. Load-bearing timing constants scattered (0.3/0.4/4×/jitter set/20 s/2-tick/…).
10. Mailboxes/agentlog exist but are not a delivery or audit system.
11. A 2926-line dead swarm layer that `pane` still reaches into for `agent-N` resolution.
