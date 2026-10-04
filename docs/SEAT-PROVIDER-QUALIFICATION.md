# Installed durable seat acceptance

`scripts/qualify-seat-provider.py` is an opt-in real Claude terminal check. It
requires Python 3, Node, Bash, tmux, an installed Claude CLI, and an existing
authorized `CLAUDE_CODE_OAUTH_TOKEN` supplied privately in its environment. It
makes two model requests. It does not fetch credentials, register a hosted
account, or replace any active client configuration.

```sh
python3 -B scripts/qualify-seat-provider.py /path/to/com8-0.3.0 \
  --evidence /private/new-seat-evidence --run-live

python3 -B scripts/qualify-seat-provider.py /path/to/com8-0.3.0 \
  --evidence /private/new-cli-seat-evidence --launch cli --run-live
```

The script verifies the extracted archive, runs `setup --no-clients
--no-service` in a fresh temporary home, and checks the running daemon's source
path and revision. It calls the installed `com8 spawn` with an explicit real
Claude command and records the durable identity's exact pane and workspace on
a dedicated tmux server. The default exercises generic command spawning.
`--launch cli` uses the advertised `--cli claude` selector and the supported
per-launch `COM8_CLAUDE_CMD` to apply the same restricted client configuration.
CLI mode also requires native adoption: a uniquely probed live route whose
process matches the provider pane and whose socket is inside the fixture.
Successful spawning alone does not qualify that additional check.

The first challenge goes through the installed `com8 seat send` and `seat read`.
Passing requires the exact literal input and a byte-exact copy of the payload
in an assistant text block in the intended Claude session transcript. The report
records whether the assistant added commentary. Seeing an echoed prompt on the
terminal alone is insufficient. The seat driver's public contract normalizes newlines, carriage
returns and tabs to spaces and strips control characters; this check uses one
line containing quotes, shell metacharacters, a backslash and Unicode.

For the second challenge, the controller starts a durable `ask`. In command mode
it reads the saved request and forwards it through `seat send`. CLI mode instead
requires the request to arrive natively as a cross-session message in the exact
provider transcript, with no manual forwarding fallback. The model calls the installed MCP
`com8_reply` with the exact supplied token, identity and payload. Passing requires
that recorded tool call, matching reply bytes and the resolved correlation ID.
The command mode's explicit controller handoff does not qualify autonomous
mailbox delivery or native session adoption. Neither mode qualifies desktop
wake, cross-device communication or resume.
The MCP executable comes from the installed release, supplied explicitly for
this test; installed plugin discovery has its own acceptance harness.

Built-in provider tools are disabled. Only the fixture's `com8_reply` MCP tool is
allowed; the client denies other approval requests. The new empty workspace is
trusted only in the fixture's own configuration. The harness does not bypass all
permissions or answer unexpected onboarding dialogs. Missing prerequisites leave
the result unqualified; submission uncertainty or mismatched evidence fails it.

Cleanup terminates only the fixture's pane, identities, daemon process group and
named tmux server, then uninstalls its temporary release. It rechecks both the
input artifact and installed payload for changes. Private transcripts and logs
remain for review, with directories mode 0700 and evidence files mode 0600; no
credential file is deliberately written. Publish only reviewed status summaries.
