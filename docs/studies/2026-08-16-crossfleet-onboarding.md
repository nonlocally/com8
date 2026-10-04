# Cross-fleet onboarding + permissions for com8 — design (live-verified)

Date: 2026-08-16 · Two people, two machines, two tailnets, two Unix users. Goal: someone downloads
com8 and uses it; two people inter-communicate across shared cluster access OR shared agent comms.
Grounded in the recovered aadarwal↔collaborator scheme (communicate#4/#6/#7/#8, PixCell#87). Probes ran
against a throwaway sshd on localhost + one read-only check of air-2; no real authorized_keys touched.

## Verified facts (throwaway sshd, OpenSSH 10.2)
1. `restrict,port-forwarding,command="/usr/bin/false"` refuses exec (exit 1) and PTY — therefore
   **BREAKS `_remote_home()`** (`ssh addr 'echo $HOME'` → empty). **Cross-fleet invitations MUST
   carry the remote inbound socket path; it cannot be probed.**
2. The same key carries com8's exact envelope round-trip over a `-L` unix-socket forward (sent
   `{"v":1,"kind":"m"}`, got `{"ok":true,"ack":...}`). `-R` socket forwards work too.
3. **The forwarding grant is INDIVISIBLE on OpenSSH 10.2:** every attempt to allow socket
   forwarding while denying TCP (`permitopen`, `AllowTcpForwarding no`, `PermitOpen none`) ALSO
   denied streamlocal. The recovered line is already the minimal transport grant; its true scope
   is "any TCP/unix connect the target uid can make." Bound it at other layers, don't pretend.
4. `AllowStreamLocalForwarding local` alone keeps `-L` working (forbids `-R` litter, symmetric mode).
5. Litter recovery verified both ways: client `StreamLocalBindUnlink=yes` rebinds local; sshd-side
   `StreamLocalBindUnlink yes` lets a `-R` redial rebind remote litter (the issue-#7 fix, live twice).
6. `sun_path` ≈104 bytes: deep socket paths rejected. `com8 init` must check length, fall back to
   `/tmp/com8-$UID/in/`.
7. air-2 sshd has `Include /etc/ssh/sshd_config.d/*` — a drop-in config file works.

## 1. `com8 init` (download-and-use onboarding)
deps (python3, ssh≥8, tailscale optional) → state root 0700 + subdirs mail/in/links/out/seen/
keys/grants/invites (check sun_path length; fall back to /tmp/com8-$UID) → device name (tailscale
Self.DNSName short, else hostname) → **fleet name** (prompt, default OS username; fleet = operator's
trust domain spanning all their devices; fleet.json) → **per-device fleet keypair**
(`ssh-keygen -t ed25519 -N "" -f keys/fleet_ed25519 -C com8/<fleet>@<device>`, outbound dials only,
never leaves the machine) → `com8 install` (launchd/systemd) → **control.token** (32 bytes 0600;
every control op on com8.sock must present it — a forward-only peer can dial your sockets but can't
read files) → `pm` front door (already _RESERVED; every fleet pingable at pm@<fleet>).

## 2. Two-fleet connect (no login shell / account / password crosses)
What crosses: one pubkey + one JSON card each way, and one authorized_keys line appended BY THE
OWNER of each machine.
- **A: `com8 federate invite peer`** → a **com8-card** (send via GitHub issue preferably — the
  auditable slow path that never went down):
  `{"v":1,"kind":"com8-card","fleet":"aadarwal","addr":"aadarwal@air-2.tailXXXX.ts.net",
   "tailscale_ip":"100.64.10.7","pubkey":"ssh-ed25519 …","fingerprint":"SHA256:…",
   "inbound":"/Users/aadarwal/.local/state/communicate/com8/in/peer.sock"}`
  (inbound = where A receives the collaborator's envelopes — in the card because the key can't echo $HOME).
- **B: `com8 federate accept aadarwal --card '…'`** → confirm fingerprint → append to
  `~/.ssh/authorized_keys`:
  `restrict,port-forwarding,from="100.64.10.7",command="/usr/bin/false" ssh-ed25519 … com8-fleet:aadarwal`
  (from= pins A's gateway IP, optional) → sshd drop-in iff asymmetric (`StreamLocalBindUnlink yes`)
  → `com8 link aadarwal --fleet --remote-in <card.inbound> --addr <card.addr>` (binds in/aadarwal.sock,
  writes grants/aadarwal.json = {"granted":[]} default-deny) → prints B's counter-card.
- **A: `com8 federate accept peer --card '…B'`** → mirror. Both dial OUTBOUND ONLY:
  `ssh -N -o BatchMode=yes -o IdentitiesOnly=yes -i keys/fleet_ed25519 -o ExitOnForwardFailure=yes
   -o StreamLocalBindMask=0177 -o StreamLocalBindUnlink=yes
   -L links/peer.sock:/home/<their-user>/.local/state/communicate/com8/in/aadarwal.sock
   <their-user>@<collaborator-host>.tailYYYY.ts.net`
  = `_ssh_cmd` with two deltas: `-i`+IdentitiesOnly (fleet key not default agent keys), and the
  remote path VERBATIM from the card (not `_remote_home()` composition).
- **Network:** each owner Tailscale-shares their gateway into the other's tailnet; ACL row
  `{"action":"accept","src":["autogroup:shared"],"dst":["<collaborator-host>:22"]}`. Two #87 checks:
  Tailscale SSH does NOT cover shared-in external users (real authorized_keys required — which we
  install); verify the shared node with `tailscale whois` + node ID on both sides (a share once
  served .12 for a box owning .13).
- **Symmetric** (both share a gateway, two independent `-L` dials, inbound/outbound fail
  independently) preferred; **Asymmetric** (B can't dial out): A dials both halves (`-L` + `-R`),
  B uses the `sock=` link variant (dials nothing, writes into the forwarded socket) — the issue-#4
  shape with the #7 fix. Honest label: B's status shows `outbound: via A's tunnel (reported)`.

## 3. Shared agent communication + grant model
Addressing: `name@fleet` (@ illegal in claimed names → unambiguous; fleet petname is a link key).
B controls who A may reach via `grants/<fleet>.json`: `com8 grant aadarwal cluster-librarian` /
`com8 revoke`. Enforced in `_recv_envelope` when the arrival link is kind:"fleet":
- `to` must be in the grant set (else ONE deliberately-ambiguous error `unknown or ungranted` — no
  enumeration of which names exist).
- **No auto-claim** (the local-mail-never-bounces rule is a mailbox-creation oracle for foreigners;
  fleet links skip it).
- `pm` implicitly granted (hello, roster, ping; rate-limited).
- Attribution is ARRIVAL-LINE-derived: everything on in/aadarwal.sock IS fleet aadarwal; a third
  fleet can't spoof (no channel lands on that socket). Foreign senders → proxy peers
  FLEET-QUALIFIED (`orchestrator@aadarwal`, never bare) → can't shadow/squat a local name.
- Last gate: Claude's held-message approval for bypass-mode sessions; durable audit via inbox.
Discovery: `com8 roster peer` asks pm@peer, plants sidecars for granted names → ListAgents shows
B's granted agents with zero access beyond mail.

## 4. Shared cluster access: grant capability, not credentials
**cluster-librarian pattern:** B never makes an account for A; B publishes an AGENT whose
capability IS the cluster. `com8 claim cluster-librarian; com8 grant aadarwal cluster-librarian`;
registry/cluster-librarian.md is the files-as-API contract (policy: Slurm dispatch under B's own
allocation, 2D→3D ladder, sbatch consent-gated by B's operator, results carry provenance). The
librarian runs as B's Unix user with B's cluster creds (Engaging/Duo — never cross the boundary).
A invokes: `com8 send cluster-librarian@peer "REQUEST fdtd-sweep input_sha256=… budget≤2 GPU-h"` →
A's out/peer queue → forward-only tunnel → B's in/aadarwal.sock → grant check → durable store, ack,
wake. If librarian asleep the request WAITS (what the rendezvous bridge couldn't do). Librarian
wakes, applies policy, asks ITS OWN operator for node-hour consent (B approves spend not A),
dispatches Slurm, replies with provenance in-band (the #6 precedent: `tidy3d 2.9.1 @ Engaging,
Slurm 19988227, input_sha256 …, 12s wall`). The capability crossed; the account did not.
**Alternative (scoped account, PixCell #87):** a scoped uid, lab groups, NO sudo, keys from
github.com/aadarwal.keys, ACL to one host:22. Rule: grant capability first; escalate to a scoped
account only when the librarian conversation becomes the bottleneck (RL loops on a live GPU kernel);
middle rung: librarian stands up Jupyter-on-GPU and hands A localhost:port+token (a session
capability, still no credential).

## 5. The 6 stacked layers (a foreign message must pass ALL)
L0 NETWORK  tailnet ACL A-gw→B-gw:22 only            revoke: unshare/ACL row
L1 TRANSPORT restrict,port-forwarding,command=/usr/bin/false — no shell/exec/pty; CAN forward
             (TCP+unix, INDIVISIBLE) as B's uid       revoke: delete authorized_keys line
L2 LINK     arrival-line attribution; dedup; ack      revoke: com8 federate unlink
L3 IDENTITY grants/<fleet>.json; no auto-claim; no enumeration   revoke: com8 revoke
L4 DELIVERY mailbox-first → store→wake; Claude held-message gate  revoke: don't approve
L5 EXECUTION payload is text; capability only via receiving agent's own sandbox (codex read-only,
             box VM, consent-gated sbatch)            revoke: agent's config
**Residual (honest):** L1's grant = "connect anywhere B's uid can" including B's other sockets +
TCP egress — cannot be narrowed at ssh without breaking socket forwards. Mitigations: Claude holds
foreign turns + drops session_id-mismatched frames; **com8.sock requires control.token (a
forward-only peer can connect to sockets but can't read files)**; from= + one-host ACL. This is the
boundary the collaborator accepted and twice declined to widen.

## 6. One naming scheme across fleets
Bare name = fleet-local. `name@suffix`: suffix resolves against YOUR routes (own devices = full
trust; fleet petnames = forward-only) — one namespace; invite refuses a petname colliding with a
device link. A fleet petname is YOUR local binding anchored to the card's key fingerprint (the
fingerprint is the identity; the petname is how you say it — no global registry). The PLANE is the
reach type not the namespace; cross-fleet ONLY `mail:` is exported (never pane/box). Collision law:
local claim > foreign proxy (proxies stored fleet-qualified). pm reserved everywhere. Global
uniqueness = (suffix unique in your table) × (name unique in its fleet), both locally enforceable.

## 7. Implementation deltas (surgical)
1. links.json gains kind:"device"|"fleet", identity_file, remote_in, key_fp; `_ssh_cmd` adds
   `-i`+IdentitiesOnly, uses remote_in verbatim; `_ensure_ssh` skips `_remote_home()` when remote_in
   set (REQUIRED — verified fact 1).
2. `_recv_envelope`: fleet links → grant check, no auto-claim, fleet-qualified proxies.
3. Control ops on com8.sock require control.token.
4. New verbs: `com8 federate invite|accept|revoke|status`, `com8 grant|revoke`, `com8 roster`.
Queues/acks/dedup/store→wake/launchd all carry over unchanged.
