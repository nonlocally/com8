# Overnight handoff — the com8 fabric, v2 complete

*2026-08-16, overnight session. Everything below is committed on `fabric-v2`
(pushed), tested (**171/171 across 11 suites**), live-proven on real hardware
where noted, and hardened against an adversarial code review. Nothing in
the anu monorepo checkout (the retired predecessor repository) was touched or deleted.*

## What exists now, in one paragraph

One per-device daemon (**com8**, python stdlib, `lib/com8.py`) gives every agent
a durable name, a durable mailbox, and measured liveness; carries mail across
your devices over acked/deduped/backoff'd ssh links; drives interactive
terminal surfaces (**seats**) locally and across devices; **creates** agents
(`spawn` = claim+seat+bind+adopt), **relocates** them between machines with the
address alive mid-move (`move`), **contains** them in air-gapped VMs that
remain first-class peers (`claim --boxed`), and **federates** with another
operator's fleet under deny-by-default capability grants. Two faces, one
kernel: the CLI (`communicate com8 …`) is the complete definition; the npm
package (`packages/com8`, `@aadarwal/com8`) projects it as ~18 MCP tools for
any MCP-speaking agent (claude, codex, …) with zero fabric logic in Node.

## Try it in 2 minutes (both real daemons are already up + linked)

```sh
communicate com8 agents                          # the roster, measured
communicate com8 spawn helper --cli claude --cwd ~/src/aadarwal/communicate
communicate com8 ask helper "say hi in one word" --from you --timeout 120
communicate com8 seat spawn 'bash' --device aadarshs-mac-air-2   # a far seat
communicate com8 move helper aadarshs-mac-air-2  # relocate the agent-being
```

## The night's build order (each step committed + tested before the next)

| Step | What | Proof |
|---|---|---|
| A1 | `ask` (return token `asker@device~corr`, blocking; natural-reply fallback) + `group` + `notify` (durable human lane + `COM8_NOTIFY_CMD`) | 10/10 |
| A2 | Seat plane: clean-room tmux driver — classifier (dead>approval>busy>booting>idle), deliver-and-verify send, secret-redacting read, fail-closed respond | 10/10 real tmux |
| A3 | Cross-device seats: `{kind:"seat"}` envelopes, result-in-ack, **`--allow-seats` per link** (upgrade/revoke in place) | 9/9 two daemons |
| A4 | `spawn` / `fan` / `consult` — the fabric creates agents (no anu dependency) | 10/10 |
| Dist | `@aadarwal/com8` npm pkg: thin MCP server + vendored daemon; `npx com8 setup/doctor/serve`; daemon grew `agents`/`inbox`/`wait` (long-poll) ops | 5/5 via a real MCP client; **not published** (deferred to you) |
| Fleet | `federate invite/accept` cards, forward-only key install, deny-by-default `grant`s, fleet-qualified proxies, control token, boundary token-rewrite, full cross-fleet ask→reply | 17/17 |
| Move | `com8 move` — the user-requested agent transfer (see below) | 17/17 + live |
| Boxed | `claim --boxed` + outbox drain + `bin/com8-boxed-init` shim | 14/14 + live air-gapped VM |
| Dogfood | Everything live on mini-2 ↔ air-2; 5 frictions found & fixed | see below |

## The agent-transfer feature you asked for (mid-night message)

I read old `beam` critically first. It got right: identity travels *inside* the
transcript (custom-title record), `$HOME` translation, honest fork warnings.
It missed everything com8 exists for: no mailbox, no claim, and **the address
died at the origin** — mail kept landing in the old inbox.

`com8 move <name> <device>` moves the agent-being = **three JSON artifacts**
(your instinct was exactly right): transcript (rsync), mailbox
(`inbox.jsonl` + cursor), identity claim. The order keeps the address alive:

1. `premove` — verify local; refuse LIVE sessions (unless `--fork --as`)
2. transcript rsync (origin still owns the name — safe)
3. `depart` — **atomically** release + proxy(home=target); from this instant
   new mail routes over the link (target auto-claims on first delivery)
4. mailbox rsync (now frozen) → staged
5. `arrive` — claim + recomposing merge: delivered history stays behind the
   cursor (a resumed session never re-receives acted-on mail); every
   undelivered line — including stragglers that raced in mid-move — waits
   after it; idempotent (msg_id dedup)
6. `--spawn` — resume in a seat via the cross-device seat plane

Live-proven mini-2 → air-2: post-move bare-name mail at the origin landed in
the far inbox; the transcript resumed-able by `claude --resume <sid>` there.

## Live proofs (real hardware, tonight)

- **Real agent loop**: `spawn scout --cli claude` → adopted:true → `ask` →
  scout autonomously ran the reply command → ask resolved `"FABRIC-ALIVE"`,
  10.09s.
- **Cross-device seat**: spawned + drove a shell ON air-2 FROM mini-2
  (`LIVE_SEAT_E2E_42` read back), gated by the seat grant.
- **Boxed peer**: a real `--network none` VM published its pair; roster read
  `live [boxed-probed]`; mail both ways with correct attribution; spool
  cleared on ack; `ENETUNREACH` to 1.1.1.1 — **mail is the box's only
  egress**.
- The mini-2 ↔ air-2 links (seats granted both ways) are installed fabric, not
  test residue. air-2 is a laptop and sleeps; when it went offline overnight
  the mini-2 daemon stayed up untouched (decoupled failure domains, exactly as
  designed) — air-2's launchd daemon resumes on wake and the links reconnect,
  draining any queued mail. If `com8 status` on air-2 is unreachable in the
  morning, wake the laptop; nothing needs restarting.

## Frictions dogfood found (all fixed + committed)

1. Symlinked `bin/communicate` couldn't find `lib/` → resolves symlinks now.
2. A booting claude eats an early Enter → send verifies the composer CLEARED
   (growing retries; never re-sends text).
3. The held-message gate + unnumbered menus weren't classified as `approval`.
4. `seat respond` could mistake prose ("this is what will be delivered") for
   an option and pick a highlighted Deny → parses only the contiguous menu
   block; **no affirmative row ⇒ fail closed**.
5. Spawned workers now launch with `crossSessionInbound: accept` scoped to
   their own process (a mailbox worker needing a human click per message is
   broken by construction); ask instructions carry `--from <target>`; seat
   bindings survive daemon restarts.

## Security posture (what's enforced, not aspirational)

- Fleet links: deny-by-default grants; ONE ambiguous error (no enumeration);
  no auto-claim oracle; fleet-qualified proxies (`orchestrator@alice` can
  never shadow a local name); return-path auto-grant only for names YOU
  message outward; `control.token` gates the control socket once any fleet
  link exists (a forward-only peer can dial sockets but can't read files).
- The forward-only key line (recovered from the collaboration, verified
  against a throwaway sshd): `restrict,port-forwarding,command="/usr/bin/false"`
  — refuses exec/PTY; carries socket forwards; the grant is indivisible on
  OpenSSH 10.2 (documented honestly, bounded at other layers).
- Boxes: inbound rides published vsock sockets only (host→guest IP is
  TCC-fragile — measured); `--network none` default posture; per-agent
  socket pair = attribution by construction.
- Seats: opt-in per link, revocable; respond fails closed.

## Security review (ran it on my own work, fixed everything)

I dispatched an adversarial reviewer over the whole ~4,800-line diff. Findings
clustered at the new trust boundaries (the core message plane came back clean —
lock ordering sound, no deadlocks). Two were genuinely critical; all are now
fixed with regression tests that fail if the guard regresses:

- **Fixed — fleet status oracle** (critical): a fleet peer could call `status`
  unauthenticated and get your whole roster, defeating the name-masking. Now
  every control op needs the token once a fleet link exists.
- **Fixed — authorized_keys injection** (critical): a crafted card could smuggle
  a newline and write a second, unrestricted key. Card fields are now
  strict-validated; malformed cards are refused.
- **Fixed** — boxed re-offer dedup (double-delivery after a lost ack), boxed
  drain thread leak on release, arrive/deliver cursor race, `respond --deny`
  auto-approving on a highlighted Yes, `move --as` rsync injection, and a
  concurrent-autostart split-brain. See the fix commit for the 1:1 list.

Two MINOR items I judged as follow-ups rather than fix-tonight (both low-risk,
noted here so they're not lost):

- **M-3** — a granted fleet peer varying its `from` name mints a new proxy
  (socket + sidecar + thread) each time; a semi-trusted peer could exhaust
  resources. Wants a per-fleet proxy cap — a small policy decision I'd rather
  you weigh in on.
- **M-5** — `depart` has a sub-millisecond window (under `claim_mu`) where a
  send between release and proxy-rebind sees "unknown identity". Tiny; the
  clean fix is to build the proxy entry and swap atomically.

## Deferred to you (deliberately)

1. **`npm publish`** of `@aadarwal/com8` (+ `mcp-publisher` registry push) —
   built + tarball-verified locally; publishing is outward-facing.
2. **Retirement of the anu monorepo checkout** — you said you'd drive deletion in the morning.
   The harvest triage (what to keep/reject and why) is in
   `docs/studies/2026-08-16-harvest-old-com8.md`.
3. **Cross-fleet live test with a real second operator** — the machinery is
   tested between two local daemons; the human half (the collaborator's side) needs a
   willing peer.
4. **Boxed CLAUDE agent** — the shim + transport are live-proven; putting a
   real claude CLI inside the image (per-agent credentials, never copies) is
   a decision about credentials I didn't make unilaterally.

## Where everything is

- `lib/com8.py` (~2600 lines, the daemon) · `lib/com8_seat.py` (seat driver) ·
  `bin/com8-boxed-init` (in-box shim) · `packages/com8/` (npm+MCP)
- Tests: `scripts/test-com8-{core,ask,link,seat,seat-link,spawn,mcp,fleet,move,boxed}.sh`
- Studies (tonight's research, all live-verified): `docs/studies/2026-08-16-
  {harvest-old-com8,sandbox-boxed-peer,distribution-mcp,crossfleet-onboarding}.md`
- Specs: `docs/superpowers/specs/2026-08-15-agent-fabric-design.md` (v1),
  `2026-08-16-fabric-v2-seats-and-plugin-design.md` (v2)
