---
name: communicate-fleet
description: Reaching agents on other devices — bus invitations over HTTPS for new participants, plus legacy SSH remote session listing and Claude bridges. Use when connecting another person's device, joining a remote bus, listing sessions, bridging/unbridging, or checking communicate's network plumbing.
---

# communicate-fleet — other devices

For a shared registered bus, first inspect
`communicate bus status --no-start --json` and use the configured hub. If the
user names a different hub, inspect it with global `--hub URL` and keep that
selection. COM8's hosted `https://bus.nonlocally.org` is an available option;
it requires enrollment there. Missing enrollment calls for a scoped invitation,
not a local replacement. Follow `communicate-bus` for connect/use.
The owner issues
a scoped invite with `communicate bus invite BUS --url https://HOST`; the
participant runs `communicate bus connect --invite-stdin < /absolute/private/invitation`
(a user-owned, mode-`0600` file; keep its contents out of arguments and logs), then
`communicate bus register --bus BUS` inside its existing agent session.
Participants only make outbound HTTPS requests. This works across tailnets
when the owner intentionally exposes its authenticated gateway over public
HTTPS, and requires no participant SSH access or inbound agent socket.

The SSH tools below remain useful for trusted devices already under your
administration. They grant the access of the SSH account and do not implement
private bus membership checks.

Devices are addressed as `local`, a Tailscale hostname (e.g.
`lab-mini`), or `user@host`. Transport is plain ssh — your keys are
the trust model.

## Check the substrate first

```sh
communicate link <device>    # tailscale sees it? ssh works? codex there? how many Claude sessions?
communicate ls  [device]     # local: sessions + bridges · remote: its Claude session table
```

## Bridge a remote Claude session in

```sh
communicate claude bridge <device> [name|pid|newest]
communicate claude unbridge <device|all>
```

What it does: picks the remote sidecar, mirrors the two sockets across an
`ssh -L`/`ssh -R` pair INTO IDENTICAL PATHS on each side, and plants each
side's sidecar on the other. Because a forwarded unix socket is an ordinary
local file, every native guard passes — the remote session simply appears in
`ListAgents`/`communicate agents` (type `claude*`) and you message it normally;
replies come back the same way. Works across Anthropic accounts (it is
filesystem + socket, not cloud).

**Constraint:** run `bridge` from INSIDE a Claude Code session — it needs your
`$CLAUDE_CODE_MESSAGING_SOCKET` as the return address. A supervisor re-plants
sidecars every few seconds and survives transient ssh drops.

## Codex on other devices

All three Codex lanes take the device argument directly (`communicate codex
queue <device> …`, `ask`, `peer`) — no bridge needed; they run over ssh
per-command.

## See and tear down plumbing

```sh
communicate status   # active bridges (up/down), codex peers
communicate down     # stop wakes + unbridge all + unpeer all; removes every planted artifact
```

`down` is deliberately complete: sidecars it planted, tunnels, daemons — gone.
It does not stop the bundled durable COM8 daemon. Use `com8 stop` for that
separate service when intended; saved identities and messages remain.

## Safety

Bridging widens "who can message this agent" from you-locally to whoever can
reach that socket over the ssh login. Only bridge to ends you trust; the
receiving side's inbound gate (see the `communicate` skill) still applies.
