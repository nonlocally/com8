# Explicit communicate buses

## Problem and contract

The existing router discovers native Claude sidecars and SSH adapters. Discovery
is implicit and does not establish a membership or authorization boundary.
Cross-operator access exists in the separate com8 plane, which the communicate
package intentionally does not ship. A plugin user needs one explicit
registration command, an honest roster, and scoped access without a shell login.

`communicate bus register` attaches the current session to `general` on the
configured hub. `--bus photonics` joins only that bus. Repeating registration
updates the same session. No latest-session guessing and no substitute Codex
headless thread. General is the default shared bus on a hub, not a worldwide
public directory. Independent installations connect to the same hub by invitation.

## Implementation

* A stdlib Python broker owns a SQLite registry, bus membership, scoped bearer
  credentials, one-use expiring invitations, bounded mail queues and receipts.
* The local owner can create buses and admit/revoke devices. Each invitation
  grants enrollment in one bus. Both sender and recipient must belong to the
  addressed bus; membership is rechecked when queued mail is collected.
* Each installation has one outbound polling worker. Its adapter maps a
  registered session to an existing Claude socket or an exact Codex thread.
  Socket probes mean live; a Codex queue target means queueable, not live.
  Expired worker heartbeats mean offline. Accepted/queued/delivered are distinct
  from an agent having read or answered a message.
* An embedded dashboard displays authorized buses and agents, search, measured
  status, registration instructions, and owner controls. HTML contains no token
  or roster before authentication. Browser auth starts in the URL fragment and
  stays in session storage; API calls require an Authorization header.
* CLI and MCP share the same implementation. Plugin instructions make the
  natural-language registration request actionable. Source checkout and npm
  installation must run the same new tooling without com8 dependencies.

## Network and security boundary

The broker binds only loopback HTTP. Other machines use HTTPS through a TLS
reverse proxy: Tailscale Serve for a tailnet, or an explicitly enabled public
HTTPS endpoint (for example Tailscale Funnel) for collaborators outside it.
Participants make outbound HTTPS calls; they expose no inbound listener, native
agent socket, filesystem, terminal or shell login. No network exposure or live
SSH/Tailscale configuration is changed during development.

Credentials are capabilities. Invitation holders can enroll local sessions in
the named bus; a revoked credential cannot read, send, poll, or enroll. The hub
operator can see routed message content and administer memberships. Same-UID
local processes are trusted; private buses do not sandbox them or remove the
older native socket/SSH paths. A session registered in both general and a private
bus remains reachable through general. Received content does not attest sender
permission mode and remains subject to the receiver's native approval gate.

## Verification

Test broker authorization, spoof rejection, private roster hiding, one-time
invites/expiry, revocation of queued mail, concurrent registration, bounded
payloads, restart persistence and lease expiry. Exercise the real CLI/HTTP/worker
round trip with temporary homes and real Unix sockets plus an exact-argv Codex
queue fixture. Test multiple isolated installations, HTTPS enforcement, and
browser injection/auth controls. Run the packed npm MCP/setup/distribution
tests and inspect the dashboard in a real browser. State separately what was
tested locally and what needs a real operator-approved network deployment.
