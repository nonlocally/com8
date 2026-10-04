# Boxed agent as com8 peer — LIVE-VERIFIED sandbox study (apple/container 1.0.0)

Date: 2026-08-16 · Host macOS 26, arm64, container 1.0.0. Every result below was MEASURED by
running commands (probe names com8-probe-*, all cleaned up, no sudo). This resolves the Layer-4
must-measures from the v1 spec.

## Measured results (the load-bearing facts)

1. **Container socket → host via `--publish-socket`: WORKS.** Guest binds a UDS inside; the host
   path appears at container start owned by the invoking user; host connects first try; bytes
   flow both ways. Multiple `--publish-socket` per container work. **Connect-success proves
   nothing** — an unbound guest side still accepts connect then EOFs immediately, so a recv-probe
   is required — which is EXACTLY com8's existing liveness ladder. COM8's measured-liveness
   semantics survive the vsock forwarder unchanged.
2. **Host socket → container via virtiofs mount: DOES NOT WORK.** A host-bound UDS mounted into
   the guest is *visible* (`listdir` shows it) but INERT: `stat` → `EOPNOTSUPP (95)`, `connect` →
   `ECONNREFUSED (111)`; the host server never sees the guest. **Definitive: unix-socket bridging
   is one-directional — guest-listener / host-connector only. A boxed agent can NEVER dial
   /tmp/cc-socks/com8-*.sock through a mount.**
3. **`--network none` box exchanged mail BOTH ways over two published sockets** (inbound frame in,
   outbox drained out) — with ENETUNREACH to everything else. The maximally hardened posture is
   the one verified working end to end, not a compromise.
4. **Host → container by IP: works but TCC-gated + lazy-ARP.** macOS Local Network Privacy is
   per-process: Apple-signed nc/curl connect to a guest IP; homebrew `python3` gets EHOSTUNREACH.
   A launchd com8 could be silently TCC-denied. **Never build inbound-to-box on host→guest IP —
   use vsock published sockets** (measured working from homebrew python). Guest→host TCP works
   from any host process, with the guest's real source IP visible (per-agent attribution).
5. **Tailnet from a default-network box: PASS** (SSH banner from air-2 100.122.154.97:22).
   Blocked under `--network none` and `--internal`. Resolves the flagged unknown: a default box
   reaches mesh devices; a none/internal box cannot.
6. **Per-agent network matrix:** custom nets NAT to internet by default; same-net containers reach
   each other; cross-net + foreign-gateway isolation confirmed; `--internal` = host-only (reaches
   its own gateway, nothing else) with no sudo; host listeners bound to a specific gateway IP are
   honored, lo-bound (127.0.0.1) services are invisible to guests.
7. **Identity primitives all work:** `--user 1000:1000` (caps all 0), `--cap-drop ALL`, `-l`
   labels (persist in inspect → roster filtering), `--name` = guest hostname + distinct IP/MAC,
   `--memory`/`--cpus` honored (guest CPU count runs requested+1; ~100MB VM memory floor),
   `container exec` warm-injection works.

## The design forced by measurement: the box publishes, the host com8 adopts

The box cannot reach out (2) and host→box IP is fragile (4), but the host can always dial into the
box over vsock (1) and that carries mail both ways with zero network (3). So NOT "host reaches in
over IP", NOT "mount the sockdir" — both dead by measurement. **A box is a device one vsock-hop
away.** Reuse com8's link machinery (per-peer inbound socket for attribution, outbound queue with
acks+dedup, from-rewrite at the boundary); only the transport differs (`--publish-socket` pair vs
`ssh -N -L`).

- **Inbound (host→box):** container publishes its in-box socket AS the identity's claimed socket
  in the host sockdir: `--publish-socket $SOCKDIR/com8-<name>.sock:/run/com8/agent.sock`. The
  boxed agent is then indistinguishable from any peer (same path shape, same newline-JSON frames,
  same EOF/hold liveness). `claim --boxed`: com8 does NOT bind the socket; it records the published
  path as authoritative, plants its sweep-proof sidecar pointing at it, probes with provenance
  `boxed`. Container dies → probe reads dead → replant/hold mail. Store→wake works verbatim with
  "session appears" → "published socket answers".
- **Outbound (box→host):** a second published socket = the outbox; the guest binds it, the host
  com8 holds a persistent drain connection (reconnect loop) and receives frames as the agent
  emits them (measured under `--network none`). At-least-once via msg_id + ack-into-agent.sock;
  guest clears its spool on ack (mirror of out/<device>/ + seen/).
- **In-box shim (~100 lines stdlib, NOT a mini-com8):** owns the two stable sockets, relays
  inbound frames into the ephemeral Claude/codex session socket INSIDE the box (both ends
  in-guest — trivial UDS relay; what's impossible across the boundary is ordinary within it),
  holds mail in a tiny in-box inbox.jsonl while the session restarts, spools outbound to the
  outbox. Stable name outside, ephemeral session inside — the in-box analog of store→wake. Also
  solves path fixation (publish paths are fixed at create; Claude's socket is pid-derived).
- **from-rewrite at the boundary (wire requirement):** cc-socks replies dial the `from` path. A
  guest `from` is meaningless on the host; a host `from` the guest can never dial (2). So com8
  rewrites `from` on every drained frame to `uds:$SOCKDIR/com8-<name>.sock`, and the in-box rule
  is **replies never dial from — they go to the outbox addressed by name.** Make this asymmetry
  explicit or replies silently die.

## The spawn recipe (concrete, for `com8 spawn <name> --sandbox box`)

```
NAME=worker-7; SOCKDIR=/tmp/cc-socks; BOXDIR=$COM8_STATE/boxes/$NAME
container run -d --name com8-$NAME \
  -l com8.identity=$NAME -l com8.epoch=$(date +%s) \
  -e COM8_SELF=$NAME -e IS_SANDBOX=1 \
  --user 1000:1000 --cap-drop ALL --cpus 4 --memory 4G \
  --network none \
  --publish-socket $SOCKDIR/com8-$NAME.sock:/run/com8/agent.sock \
  --publish-socket $BOXDIR/outbox.sock:/run/com8/outbox.sock \
  -v <per-agent-worktree>:<real path> \
  anu-agent com8-boxed-init claude --dangerously-skip-permissions
```
Then `com8 claim $NAME --boxed com8-$NAME`. `container exec com8-$NAME …` is the natural `box:`
seat driver the v2 spec reserved. Constraint: `sun_path` is 104 bytes — sockdir must be short
(/tmp/cc-socks fine; deep scratchpad paths cannot hold sockets).

## Hardening ladder — 3 measured egress tiers (all keep --user/--cap-drop/per-agent mounts+socketpair)

1. **Mail-only (default unattended):** `--network none` + socket pair. Only egress is com8 mail,
   all attributable/loggable. Verified end-to-end.
2. **Broker-mediated:** per-cohort `container network create --internal` + a host-side allowlist
   proxy bound to that network's gateway IP only (no sudo). The "no fine-grained egress → host
   proxy" answer with a real enforcement substrate.
3. **Full egress (trusted interactive):** default network — internet + tailnet reachable; mail
   still via the socket pair.

## Hard limits to design around

No virtiofs UDS passthrough (never mount a socket). Host→guest IP is per-process TCC-gated + lazy
ARP (keep inbound on vsock). Published-socket connect always succeeds (must recv-probe). Guest
publish paths fixed at create (shim relays to pid-derived session socket). from asymmetry (rewrite
at host boundary; in-box replies via outbox). No per-destination egress filter (tier-2 proxy is
the only allowlist). sun_path 104 bytes. Guest CPU = requested+1; ~100MB memory floor. macOS 26
required for custom nets/--internal; DNS domains need sudo. OAuth single-copy → per-agent Claude
identity = per-agent accounts/keys, never copies.
