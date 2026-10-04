# How Claude Code peer messaging works (and how `communicate` rides it)

This is the reverse-engineered contract behind `SendMessage` / `ListAgents`,
verified against the Claude Code bundle (v2.1.226) and by live round-trips.
`communicate` is a thin layer on top of it — no private APIs, just the same
files and sockets Claude itself uses.

## 1. Identity: a sidecar file per session

Every Claude Code session writes a JSON **sidecar** at:

```
${CLAUDE_CONFIG_DIR:-~/.claude}/sessions/<pid>.json
```

Example:

```json
{
  "pid": 67034,
  "sessionId": "c87d2c71-…",
  "cwd": "/Users/you/src/app",
  "messagingSocketPath": "/tmp/cc-socks/67034.sock",
  "name": "agent-2",
  "kind": "interactive",
  "status": "idle",
  "peerProtocol": 1
}
```

The two load-bearing fields are **`name`** (the identity) and
**`messagingSocketPath`** (the address). Together, the set of sidecars *is* a
routing table: name → socket.

## 2. Address: a unix domain socket

Each session listens on a unix socket whose path is derived as:

```
${XDG_RUNTIME_DIR || CLAUDE_CODE_TMPDIR || "/tmp"}/cc-socks/<pid>.sock
```

On macOS with `XDG_RUNTIME_DIR` unset that's `/tmp/cc-socks/<pid>.sock`. The
parent dir is created mode `0700`; the socket is chmod `0600`. The live path is
also exported as `$CLAUDE_CODE_MESSAGING_SOCKET`.

## 3. Discovery + the sweep

`ListAgents` reads every `sessions/*.json`, and for each one **connects to its
`messagingSocketPath`** with a ~250 ms timeout to check liveness. Accepting the
connection is enough to count as live.

Discovery doubles as a garbage collector: if a sidecar's socket probe **fails**
*and* its pid is **not a live local process**, the sidecar is unlinked. This is
why naively-planted sidecars vanish — a foreign pid + any transient probe miss =
reaped. `communicate` defeats this two ways:

- **Claude bridge**: a supervisor re-plants the sidecar every few seconds, so a
  sweep during a tunnel hiccup self-heals.
- **Codex peer**: the adapter daemon names its sidecar by *its own live pid* (so
  `b0(pid)` is true and the sweep never reaps it) *and* re-plants every 3 s.

## 4. The wire protocol

Messages are **newline-delimited JSON**, one object per connection, written then
half-closed (`JSON.stringify(obj) + "\n"`). A minimal inbound user message — the
binary logs this exact example as an injection hint:

```
echo '{"type":"user","message":{"role":"user","content":"hello"}}' \
  | socat - UNIX-CONNECT:/tmp/cc-socks/<pid>.sock
```

`socat` is **not** a safe assumption — it is absent on plenty of hosts, and a
runbook that requires it strands whoever follows it literally. Python is always
present where `communicate` runs, so prefer:

```sh
python3 -c 'import socket,json,sys
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect(sys.argv[1])
s.sendall((json.dumps({"type":"user","message":{"role":"user","content":sys.argv[2]},
  "from":"uds:"+sys.argv[3]})+"\n").encode())' <target.sock> "hello" <your-reply.sock>
```

or `lib/cc_peer.py send --to <sock> --from <sock> --text "hello"`.

Key rules the receiver enforces:

- `message.content` **must be a nonempty plain string** (content-block arrays are
  rejected).
- `from` is `uds:<absolute-socket-path>`; it is the reply address.
- `session_id`, if present, must equal the target's own session id — otherwise
  the message is **dropped**. Omit it.
- `priority` defaults to `next`.

To render as a proper **cross-session message** with attribution, the content
string is itself wrapped (this is what Claude's `PCr()` emits):

```
<cross-session-message from="uds:/tmp/cc-socks/99999.sock" from-name="codex-local">
…body…
</cross-session-message>
```

Delivery works with a bare string too; the wrapper only adds the `from-name`
attribution. `communicate`'s adapter always wraps, so a Codex reply shows up
labelled `codex-local`, not anonymous.

## 5. Reply / receipt routing

An automatic delivery **receipt** (`{type:"control",
action:"peer_message_status", …}`) is sent back to the message's `from` address —
but only if `dirname(from) === dirname(receiver's own socket)` and it ends in
`.sock`. So peers whose sockets live in the **same** `cc-socks/` directory get
receipts; peers in different dirs still exchange messages, just without receipts.
`communicate` therefore mirrors sockets to identical paths across the tunnel,
keeping everyone in one namespace.

## 6. The local-only guard

`L4p`/`N4p` refuse to connect unless the path is local: anything not starting
with two slashes passes (`/tmp/cc-socks/x.sock` ✓), and addresses must match
`^(?:uds|bridge|did):[A-Za-z0-9%:_/.\\-]{1,200}$`. A remote `http(s)://host` URL
is rejected outright. This is the crux of why the design is what it is: **you
cannot point Claude at a remote address.** But a unix socket **forwarded over an
SSH tunnel** appears as an ordinary local path — so it passes every guard. That
single fact is what makes cross-device peering possible without touching Claude.

## 7. What `communicate` adds

| Piece | What it does |
|-------|--------------|
| Claude bridge | `ssh -L`/`-R` mirror both sockets to identical paths; plant each sidecar on the other host; supervise + re-plant. A remote session becomes a native peer. |
| Codex peer | A daemon binds a `cc-socks` socket, plants a self-maintained sidecar, and backs the socket with `codex exec`. Codex becomes a native peer. |
| Router | Aggregates all sidecars into one `name → {type, device, socket}` table; `route` resolves a name and writes the socket — same verb for any agent kind. |
| Wake | Periodically routes a message to an agent by name (a message to an idle/finished session resumes it → a wake-up). |

Everything is the same primitives Claude already trusts: sidecar files, unix
sockets, newline-JSON. The only new ingredient is an SSH tunnel that makes a
remote socket look local, and a small adapter that lets a non-Claude agent speak
the protocol.

## 8. Addendum (2026-08-15): discovery lists only pid-shaped sidecar filenames

A planted sidecar named `pm-foo.json` **survives** the sweep (live pid in the
`pid` field, socket answers the probe) but **never appears in `ListAgents`**.
The identical object written as `3999901.json` is listed immediately. So the
filename is part of the contract: it must look like `<pid>.json` (numeric),
while the sweep's is-the-pid-alive test reads the *field*. The two are not
cross-checked — a numeric filename far above the real pid range with a live
pid in the field is both listed and sweep-proof. `com8` therefore plants
deterministic numeric filenames (`3000000 + crc32(name) % 900000`,
linear-probed on collision; macOS `pid_max` is 99998). Verified live on
Claude Code 2.1.x.
