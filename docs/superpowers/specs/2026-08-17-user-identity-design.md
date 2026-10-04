# The user layer — @handles for the com8 fabric

Status: phases A–C implemented on this branch (init/user, pair, connect);
the switchboard directory is DESIGNED HERE, DEFERRED deliberately.

## The thesis

The fabric modelled agents, devices, and fleets — never the person. Yet every
seam was already reserved: `fleet` was defined as "the operator's trust
domain, one fleet spans all their devices" (default `$USER`, never persisted
— `fleet.json` was specified and never built); `agent@suffix` resolves
devices and fleet petnames through ONE table; foreign senders are attributed
by arrival line and stored fleet-qualified (`orchestrator@alice`). The user
layer **completes `fleet` into a claimed identity** rather than inventing a
parallel scheme.

**The user is the fifth element — the owner — orthogonal to the four axes.**
Place is mobile by locked design (`move`), so the device stays OUT of the
identity address: `communicate@aadarwal` is the address;
`aadarwal.macmini2.communicate` is at most a display/locator form; seats stay
honestly device-pinned (`dev:%N`).

## Security model (why key-first)

No passwords. At claim time the machine holds an ed25519 key (ssh-keygen —
already the federation key); the key's fingerprint is the unforgeable
identity; **`@aadarwal` is a human-readable label bound to that key by a
signature**. Anyone can say they're @aadarwal; only the key holder can prove
it. Peers confirm one fingerprint out-of-band once (Signal-safety-numbers
style) and everything after is pinned.

Why not a directory-as-authority (accounts): whoever controls or hacks the
authority can reassign the handle to another key and impersonate you to every
new contact, and nothing works offline. Key-first, a hacked phonebook can at
worst hand out a wrong number that FAILS verification — it can never sign as
you. This is the written law made real: "the fingerprint is the identity; the
petname is how you say it."

## What shipped (A–C)

- **A — the person exists**: `user.json` `{v, handle, display?, created_at,
  claimed_via}`, written ONLY by the claim ceremony (`com8 init` /
  `user-set`); missing = loud degraded mode, never lazy regeneration (the
  split-brain-mints-a-second-person failure). `fleet_name()` precedence
  env > file > OS-user replaces both inline sites; the handle re-derives into
  every card/status/agents/whoami (registry law: re-asserted by real work).
  `COM8_VERSION` surfaced in status — a stale KeepAlive'd daemon is now
  measurable; npx setup gained the ceremony, a version gate, a measured
  self-test, and the two installer-bug fixes (split-brain `COMM_STATE`,
  missing `PATH`).
- **B — own devices**: `ping` envelope kind (stateless; old-peer response
  defined: negative-ack = transport up, legacy peer), `link-check` (measured
  RTT), `com8 pair` (one-sided: probe → stage/upgrade far kernel from this
  install's own files → device names read from daemons, never typed → both
  links → handle sync → both round trips measured).
- **C — other people**: signed card v2 (`ssh-keygen -Y` over canonical
  sig-less bytes; fingerprint RECOMPUTED from the card's key — the claimed
  string is refused on mismatch; v1 only behind `--allow-unsigned`),
  `com8 connect` (typed-handle cross-check, collision + re-key refusal,
  petname == handle — which kills the petname-symmetry silent-queue bug by
  construction — honest "transport pending" + auto counter-code).
  Hardening shipped with it: TTL'd return-path auto-grant (`COM8_AUTOGRANT_
  TTL`), grant fingerprint pin, per-user proxy cap (`COM8_PROXY_CAP`, M-3),
  qualified names in inbox/wait (P19), sun_path guard (P6).
- Trust verbs (`init`, `pair`, `connect`, `grant`, `federate`, `link`) stay
  human-only CLI — never MCP tools (locked).

## The switchboard directory — designed, deferred

When a real second operator exists to test against, a thin **keyserver** on
Cloudflare Worker + KV (operator's chosen target):

- `claim(handle, pubkey, sig)` — first-come, signature over the claim, answer
  carries a directory-signed receipt (front-running becomes attributable).
- `lookup(handle) → {pubkey}` — exact match ONLY. No listing, no liveness,
  no timestamps, no rendezvous data (today's card carries addr/tailscale-IP/
  home paths — that never gets published). Rate-limited; a claimable
  namespace is dictionary-probeable regardless — bounded, not denied.
- **Handles tombstone on loss and are never reissued to a different key**
  (kills lapse-impersonation); succession only by old-key-signs-new-key
  statements — the hook where the deferred rotation ceremony lands.
- The directory is an UNTRUSTED INTRODUCER: trust still pins the fingerprint
  at connect; the fabric is fully functional without it (invite codes carry
  the handle); local claims are `provisional` until confirmed, `conflicted`
  handles re-pick without touching any pinned peer.
- No heartbeat: a handle→key binding is not a claim about mutable reality;
  the writes that re-assert it are real work (rotation, succession).

## Cut / deferred, with reasons

- **User root key signing per-device keys** (stable person-fp across devices,
  lost-laptop revocation): pays off only when peers talk to multiple of your
  devices; shipping now means either credential replication (a stolen laptop
  holds the root — voids the revocation story it exists for) or designing
  rotation ceremonies with zero users. Card `v` makes it additive later; the
  root key, when it lands, NEVER leaves the claim device (certs travel, keys
  don't).
- Directory deployment (above). npm publish (operator: build/test first).
- Envelope user-stamping (the card is the vehicle that does real work with
  the handle; wire bytes with no consumer otherwise).
- `pm` front door / roster envelope (v1 jobs covered by ping + printed
  counter-code + the granted-name list the human already typed; a grant-gated
  roster is real enumeration-posture design with no v1 consumer).
- Petname override on connect (an override re-opens the symmetry bug; two
  contacts both claiming @peer is the directory-tier problem, refused
  honestly for now).
- Multi-handle-per-person; openwebui per-user identities; ask-identity GC.

## Related records

- `docs/studies/2026-08-17-fabric-observations.md` — the full-fabric problem
  inventory this design was cut against.
- `docs/studies/2026-08-16-crossfleet-onboarding.md` §6 — the naming law this
  layer completes.
- Commitment trailers on the three phase commits carry the per-phase
  decisions.
