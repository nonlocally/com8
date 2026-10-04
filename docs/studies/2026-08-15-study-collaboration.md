# Study: the aadarwal ↔ <collaborator-handle> collaboration — recovered decisions (evidence base for the agent-fabric design)

Date: 2026-08-15 · Method: subagent investigation of local QPG-MIT repos + GitHub PR/issue
history across repos.

## 1. Verdict: where the "pane-to-pane identity work" lives

| Repo | What it is | Pane/identity work? |
|---|---|---|
| `~/src/appliedphotonics/com8` | pure photonics domain (paper→GDS pipeline, Streamlit, MCP servers) | No |
| `~/src/QPG-MIT/com8-anu-powered` | not a repo — one orphaned experiment `.gds` | No |
| `~/src/QPG-MIT/COM8-engine` | **the Go kernel extraction** — "standalone extraction of the anu agent engine" (15 commits, ~30 h) | **Yes — the distillation, not the origin** |

The pane-to-pane *implementation* (`pane send/ask/reply`, the classifier, swarm, mesh spawn)
lives in the anu monorepo `/Users/aadarwal/COM8` (= `QPG-MIT/COM8`); `COM8-engine` is the Go
rewrite that names the idea. Its architecture table (`COM8-engine/ARCHITECTURE.md:20-27`):

> | **Identity + state** | the pane id (`%82`) | the registry object (`<state-dir>/`) |
> | **Execution / PTY** | the pane | the **supervisor** (tmux, dedicated socket) |
> | **Isolation** | (usually none) or a hand-mounted box | the **substrate backend** |
> | **Display / control** | the pane | a **renderer** |

Caveats: **`QPG-MIT/COM8-engine` no longer exists on GitHub** ("Could not resolve") — the
local clone is the only surviving copy. `/Users/aadarwal/COM8-engine` (home dir) is a
*different thing* — a worktree of QPG-MIT/COM8, not the Go repo.

**Where the collaboration actually happened: `aadarwal/communicate`** — the collaborator has 2
commits here, authored PR #2 and issues #7/#8; the cross-fleet bridge negotiation is issues
#4/#6/#7/#8. Plus one issue in `QPG-MIT/PixCell-running` (#87).

## 2. Catalog of the exchanges

**communicate#2 (<collaborator-handle>, merged)** — *Add agent-registry.* One markdown file per agent,
named after the wire peer name, YAML frontmatter (`name/kind/model/device/operator/updated/
reach/availability`) + prose for skills. The decision: *the router maps name→socket, the
registry maps name→capability* — know *whom* before *how*. Covenant: "an entry describes
what the agent can do **now**, not aspirationally."

**communicate#3 (aadarwal, merged)** — *`communicate directory`.* Adopts the collaborator's schema
unchanged; adds the live-join command. aadarwal declined to add routing fields to the schema
without asking — schema authorship stayed with the proposer.

**communicate#4 (aadarwal, closed — the load-bearing thread).** The wall: peer sockets are
owned by the **Unix user** running the agent, 0700. aadarwal can ssh to that host only as
`aadarwal`; the collaborator's agent runs as `<their-user>`. "Same host, reachable over the tailnet, but a
different OS user = a wall I can't (and shouldn't) climb from my side… **one side has to
bridge toward the other, run by the user that owns the socket.**" Second wall: different
*tailnets* — 100.x doesn't route across. Resolution: the restricted key (§3). Result: first
cross-account, cross-tailnet, cross-Unix-user socket message, 2026-08-09 ~02:50 UTC. Findings
recorded: a planted sidecar with a fake pid gets reaped by the liveness sweep (needs a live
pid); `uds:` addressing works regardless.

**communicate#6 (aadarwal, closed)** — the channel doing real work: frequency-domain FDTD
fields delivered with full provenance (`tidy3d 2.9.1 on-prem @ Engaging, Slurm 19988227,
input_sha256 6747acc719, 12 s wall`).

**communicate#7 (<collaborator-handle>, open)** — *Bridge down.* An outage report with an evidence
table (socket down / tunnel down / GitHub up), each row a measurement (`lsof -U` on a socket
inode created 8 h earlier; staged data "never been pulled"). Design property, not bug: "we
just can't initiate, since **the restricted key is inbound-only by design**." aadarwal's
reply concedes fault (a Mac restart killed tunnel + `/tmp/cc-bridge/` + session `agent-2` at
once) and **declines to widen the grant**: "our key is forward-only (`command=/usr/bin/false`)
by design — so we cannot `rm` it ourselves, and **we're not asking you to widen the grant**"
— asking instead for one sshd line that grants no authority (§3). Also confesses:
`communicate directory` reported the collaborator's agent LIVE throughout (file-existence liveness).

**communicate#8 (<collaborator-handle>, open)** — *Replace the hand-rolled bridge with
herdr/firstmate.* The `-L`/`-R` pair "worked exactly once… and has been down since, because
it depends on a long-lived tunnel from your side plus a live listener on mine. Neither
survives a laptop sleeping, a network change, or an ssh-agent wedging, and **there is no
state to reattach to when it dies**." Proposes persistent runtimes; states plainly "I have
**not** run either tool"; and defends the boundary against its own proposal: "the restricted
key you hold on our side… exists precisely to bound that. I would want that boundary
preserved in whatever replaces the bridge — **I am not proposing to widen it**." Keep GitHub
as the durable fallback: "the only path that has worked reliably between us all day, and it
leaves an auditable trail."

**communicate#9 (aadarwal, open)** — *Switchboard* (see the communicate study, §5).

**PixCell-running#87 (<collaborator-handle>, open)** — *GPU account for @aadarwal.* The
device-permission grant: "Username: `aadarwal` (uid ####; groups: <lab groups>;
**no sudo**)… your **GitHub SSH keys are already installed** (all 8 from
github.com/aadarwal.keys)." Password in GCP Secrets Manager, "will not be posted here."
Then eight comments of adversarial network debugging: aadarwal proved TCP dropped on every
port while two other shared Mac Studios accepted :22; the collaborator proved the host firewall clean
(ufw 22/tcp ALLOW; ts-input ACCEPT) and the box owned `.13`; aadarwal proved via
`tailscale whois` that the *share record* presented `.12` for the same node ID. Neither host
could fix it — only the tailnet admin console regenerating the share.

## 3. The recovered SSH permission scheme

**A forced-command, forward-only SSH key** — authorizes *port forwarding and nothing else*.
The SHAPE of the line installed in `~<their-user>/.ssh/authorized_keys` on
`<collaborator-host>` (communicate#4, 2026-08-09T02:47:16Z) — the options are the
finding; the key material and its comment are redacted:

```
restrict,port-forwarding,command="/usr/bin/false" ssh-ed25519 <public-key> <operator>@<operator-device>
```

The collaborator's gloss, verbatim: "Forward-only: unix-socket/TCP forwarding works, but **no shell, no
exec, no pty** (any command runs `/usr/bin/false`). That means `communicate ls` /
`communicate claude bridge` — which exec remote commands — will **not** work over this grant
by design. Raw forwards do everything the bridge needs."

Read precisely: `restrict` turns *everything* off; `port-forwarding` re-enables exactly one
capability; `command="/usr/bin/false"` guarantees any exec request runs the forced command
and exits nonzero. **Deny-all-then-grant-one**, not allow-all-then-restrict.

Lived consequences: the only usable invocation was the raw double forward
(`ssh -N -L /tmp/cc-bridge/<their-agent>.sock:/tmp/cc-socks/32813.sock
-R /tmp/cc-socks/agent-2-aadarwal.sock:/tmp/cc-socks/67034.sock
<their-user>@<collaborator-host>`); the key is inbound-only (the collaborator's side can never
initiate, #7); and it **cannot clean up after itself** — a dead tunnel leaves a socket file
the key can't `rm`, forcing path rotation until one bound. The fix (proposed in #7):
**`StreamLocalBindUnlink yes` in sshd_config** — "It grants no new authority — still
forward-only, still `command=/usr/bin/false`, still no shell. That single line converts this
channel from 'renegotiate after every laptop restart' into something that just reconnects."

Complementary layers:
- **OS-user boundary as the primitive**: `/tmp/cc-socks` is 0700; "Foreign fleets are 0700 by
  design: we cannot enumerate them and do not pretend to" (SWITCHBOARD.md). Matrix encoding:
  `=` same host+user, `~` granted (negotiated, not self-serve), `x` no path.
- **Tailnet ACL as the network grant** (#87):
  `{"action":"accept","src":["autogroup:shared"],"dst":["<collaborator-node>:22"]}` — and the
  discovery that **Tailscale SSH does not apply to shared-in external users**, so real
  authorized_keys entries are still required.
- **Client-side key hygiene** (`~/.ssh/config`): per-host `IdentityFile` + `IdentitiesOnly
  yes`; agent-forwarding repointed at the newest forwarded socket so key approvals happen on
  the laptop's 1Password — private keys never leave the laptop. Forwarded sockets
  mode-hardened in transit (`-o StreamLocalBindMask=0177`, `lib/common.sh:56`).

## 4. Cluster control (MIT Engaging)

**(a) Indirect, via the peer agent — what actually ran.** aadarwal's fleet never touched
Engaging. It messaged `<their-agent>`; *that* agent dispatched Slurm
on its own credentials. Registry-declared capability: "FDTD orchestration — Tidy3D cloud
submits, MIT Engaging Slurm dispatch (`engaging-sim` reuse-ladder…). Policy: 2D pre-flight →
short 3D validation → full 3D runs." Delivered: "a real FDTD grating campaign: 25 serial
tidy3d-on-Engaging solves, an 11-point coupling-vs-wavelength spectrum… delivered inline over
the socket" (#4). Operational findings: results carry `Slurm <jobid>` + `input_sha256` + wall
time as provenance; the on-prem tidy3d container is single-seat — `--dependency=singleton`
chains work. **Credentials were never shared. The capability crossed the boundary; the
account did not.**

**(b) Direct, via `ncn`** (`COM8/plugins/ncn/skills/ncn-cluster/SKILL.md`): a host conductor
drives a live human-authenticated SSH pane (send-keys/capture-pane), **inheriting Duo/2FA
without ever holding a credential**; `sbatch` is consent-gated ("it consumes node-hours, so
get a yes first"); the job script is cluster-agnostic (resources from the submit line,
tunables from env), publishes `NCN_NODE=$(hostname)` and runs
`apptainer exec --nv … jupyter lab --ip=0.0.0.0 --port=$PORT`; the tunnel hops *through* the
login node (compute nodes firewalled); the Jupyter token is "the kernel's **only** access
control on a shared node"; handoff is the human's browser or a boxed cxc worker driving
`localhost:<port>` over the Jupyter REST/WebSocket API — an RL agent gets a GPU kernel
through a localhost port, holding a token, never an SSH credential. Teardown mandatory.
`~/.ssh/config:36-42`: `Host engaging orcd` + `ControlMaster auto` + `ControlPersist 4h`.

## 5. Ideas worth carrying

1. **Identity is a registry object, not a pane id** (COM8-engine) — separate identity/state,
   execution/PTY, isolation, display.
2. **The confirmed-reply channel**: "a reply is proof of completion, never a guess from
   scraping a screen" — paired with probed liveness (measure time-to-EOF).
3. **Two tables, not one** (router name→socket; registry name→capability) **plus the
   switchboard's third**: name→measured liveness with provenance
   (probed/reported/reachable-host/declared), always displayed.
4. **Deny-all-then-grant-one SSH** (`restrict,port-forwarding,command="/usr/bin/false"`) +
   sshd-side `StreamLocalBindUnlink yes`.
5. **Grant capability, not credentials** — the FDTD campaign ran without an Engaging
   allocation; a registry entry + a message channel beat account provisioning.
6. **Files-as-API** — "a dead engine still leaves a fully inspectable registry."
7. **GitHub as the durable fallback channel** — auditable; degrade fast→slow, never to
   nothing.
8. **The role/session identity split** — operator declares `aliases:`; "Guessing is what made
   discovery lie in the first place."
9. **Patching does not move an agent** — home is where it runs; a patch is a line to it.
10. **Honesty conventions that cost something** — dated entries; "I have not run either
    tool"; refusing to widen a grant you'd benefit from; signing comments with the
    addressable name so a GitHub comment, a socket peer, and a tunnel endpoint are provably
    one identity.

## Dead ends to avoid

1. A bare `ssh -L/-R` pair as the primary channel — down ~26 h twice; "no state to reattach
   to when it dies." Persistence must be a substrate property.
2. Inferring liveness from a file existing.
3. Pid-derived socket paths + session-child processes (the board empty every morning — how
   the outage went unnoticed). Fix: launchd KeepAlive/RunAtLoad + stable paths.
4. Planted sidecars with fake pids (the discovery sweep is a GC).
5. Caching that can authorize an action ("a stale entry must never authorize a patch").
6. Trusting a shared-in node's address (share record served `.12` for a box that owned
   `.13`; `tailscale ping` ponged anyway — disco-level). Verify with `tailscale whois` + node
   ID on both sides.
7. Assuming Tailscale SSH covers shared-in external users — it does not.
8. Cross-user socket access as a workaround — 0700 is the boundary; one side bridges toward
   the other, run by the user that owns the socket.
9. Adopting a tool on README + star count.
10. Scattering one idea across three repos — the unified design needs one home.

**Key threads:** `aadarwal/communicate#2,#3,#4,#6,#7,#8,#9` · `QPG-MIT/PixCell-running#87` ·
`QPG-MIT/COM8#43,#52,#53,#55,#64,#26,#28` ·
`~/src/QPG-MIT/COM8-engine/{ARCHITECTURE.md,STATUS.md}` ·
`COM8/plugins/ncn/skills/ncn-cluster/{SKILL.md,jlab.sbatch}` · `~/.ssh/config:36`
