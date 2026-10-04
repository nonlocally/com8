# Provider and client qualification

The ordinary CI gate runs isolated native, bus, durable, seat, package and
extracted-artifact checks. It does not use provider accounts. A green CI run
therefore does not establish actual Claude/Codex messaging, desktop wake, remote
enrollment, or migration from a previous installed release.

## Artifact-to-provider check

Use the reviewed extracted runtime and an already authenticated **isolated**
client home. Prepare authentication privately using the provider's supported
sign-in or an explicitly authorized local credential transfer. Do not copy an
entire live client configuration or print authentication files. This check never
changes the real account home, service configuration, plugin installation, or
hosted-bus credentials. It does make real model requests.

```sh
python3 scripts/qualify-provider.py /path/to/com8-VERSION \
  --provider codex --client-home /private/test-client-home \
  --evidence /private/new-codex-evidence --run-live

python3 scripts/qualify-provider.py /path/to/com8-VERSION \
  --provider claude --client-home /private/test-client-home \
  --evidence /private/new-claude-evidence --run-live
```

An existing Codex provider profile can be selected with `--codex-profile NAME`.
Missing authentication or unsupported client flags leave the check unqualified.
No API key, hosted account, or invitation is fabricated by the harness.

The harness validates artifact hashes, starts a fresh provider conversation,
requires an actual `bus_register` MCP call, and compares the adapter's session
key with the new Claude session ID or Codex thread ID. Its separate API fixture
sends a long payload containing fresh random markers, newlines, quotes, and
shell metacharacters. Passing requires all of the following:

- A delivery receipt for the intended endpoint: `delivered` for Claude or
  `queued` for Codex.
- An actual model `bus_reply` call using the original received message ID.
- A byte-exact reply from that registered identity to the controller fixture.
- The request and reply sharing the same durable broker conversation.
- Cleanup of the isolated provider processes and broker.

The Codex harness first obtains the fresh conversation's ID from the CLI's
`thread.started` event, then resumes that exact conversation with the ID passed
explicitly to its MCP environment. This qualifies the verified-identity path;
it does not establish automatic identity discovery by a newly started MCP child.
Codex is resumed explicitly again after queueing. This proves consumption on resume,
not autonomous wake of an inactive desktop conversation. The controller is an
API fixture, not a second model. Claude loads the artifact with `--plugin-dir`;
Codex receives an explicit artifact MCP command. Neither substitutes for the
installed-plugin discovery checks below. Raw transcripts stay in a new directory
with mode 0700 and files with mode 0600; publish only reviewed status summaries.

## Fresh installed-plugin acceptance

The same harness can exercise normal setup and real client discovery in the
prepared isolated client home:

```sh
python3 scripts/qualify-provider.py /path/to/com8-VERSION \
  --provider codex --client-home /private/fresh-test-client-home \
  --evidence /private/new-installed-evidence --installed --run-live
```

Use `--provider claude` for Claude Code. This mode runs `setup --no-service`,
starts the client without an MCP executable override or `--plugin-dir`, and
requires the same exact-session registration and correlated reply evidence.
It uninstalls its integration on exit. Use a dedicated authenticated client home
because its registry and plugin cache are exercised. For Codex, installed mode uses the real `codex app-server` and its normal plugin
discovery. The verified `thread/start` ID is supplied in the registration prompt;
subsequent phases use `thread/resume` for that exact ID. The harness does not
override the installed MCP executable or environment. This qualifies the Codex
app-server client surface; it does not claim that noninteractive `codex exec` can
answer approval prompts.

For the dormant-session phase, the registration turn finishes and its process
exits **before** the challenge is queued. Codex can automatically consume a
queued message as soon as a running turn ends; shutting it down at that point
would interrupt delivery. The resumed process has the exact reply authorization
and verified session ID ready before `thread/resume`, which may immediately
start processing the queue. A `queued` receipt alone never counts as a model
reply.

Codex 0.156.1 intersects host plugin policies with the plugin's declared approval
policy. An `approve` override cannot relax a required prompt, and `codex exec`
rejects that prompt under its noninteractive policy. Installed qualification
therefore responds through the supported app-server approval protocol. Each
one-call approval must match one pending tool item from the installed
`communicate@communicate` plugin, in the exact fixture thread and turn. Only
`bus_status`, registration of this fixture session/name, and a byte-exact reply
to the current challenge are accepted. Other requests are rejected, shell tools
are disabled, the sandbox stays read-only, and no persistent grant is written.
See the [app-server approval protocol](https://learn.chatgpt.com/docs/app-server#approvals)
and [versioned policy implementation](https://github.com/openai/codex/blob/rust-v0.156.1/codex-rs/core-plugins/src/loader.rs).
This check covers installed messaging. The durable operations, visual directory,
human inbox and desktop acceptance checks below remain separate.

On each supported client, install the same checksum-qualified artifact using
the normal `com8 setup` path, then completely restart the client. Preserve the
original configuration backup and record all commands resolved by the client.
Run from a disposable workspace outside every source checkout. Installation and
legacy disabling on shared machines require their own authorization.

1. Inspect the client's actual loaded plugin and MCP paths. Verify cached plugin
   files against the installed release and resolve its launcher to that release.
   A tool name, plugin manifest, or successful source-checkout invocation alone
   is insufficient evidence.
2. Ask the fresh agent to use its installed COM8/Communicate skill to inspect
   status, claim a uniquely named durable test identity, send to a second test
   identity, and read that mailbox. Require recorded tool calls and matching
   message bytes. Remove only those test identities afterward.
3. Ask it to register **this exact session** on the selected local/private test
   bus. Match the resulting ID to the client's current native session/thread.
   Do not substitute a newest session, a headless peer, or a display-name match.
4. Verify the bus directory, graph and human inbox using only permitted test
   identities. A human inbox reply must remain in its original conversation;
   graph edges and queue receipts are not evidence of model consumption.
5. Test an active session and then the same inactive/resumed session. Record
   native delivery, queueing, automatic wake, manual resume and correlated reply
   separately. Do not mark an unsupported wake path as passed.

Record separate results for Claude Code terminal, Codex CLI, and each advertised
desktop client. Terminal-client success cannot qualify desktop discovery or wake.
The Claude desktop application and Claude Code are different client surfaces;
do not list either as supported merely because the other passed.

## Two-model proof on one device

With both providers privately authenticated in one prepared isolated home, run:

```sh
python3 scripts/qualify-provider-pair.py /path/to/com8-VERSION \
  --client-home /private/fresh-pair-home \
  --evidence /private/new-pair-evidence --run-live
```

This installs the unchanged artifact for both clients, registers the exact Claude
session and Codex thread, and exercises Claude→Codex and Codex→Claude. Each
model originates its challenge with `bus_send`; the receiving model uses
`bus_reply` for the byte-exact response in the same conversation. The controller
supplies prompts and reads receipts and the local database. It never sends,
replies, polls, or acknowledges on either model's behalf.

Claude exposes only the four named bus tools needed for this test and no built-in
shell/file tools. Codex retains the strict one-call approval responder, extended
to allow an outbound send only to the exact fixture peer with the exact challenge
and local broker. Each direction records both delivery receipts separately.
Codex resumes explicitly to consume queued challenges; queued answers are not
reported as automatically consumed. The test stops its provider processes and
broker, uninstalls its owned integrations, and preserves private evidence.
It qualifies two models on one device, not remote transport or desktop wake.

## Two-model, two-device proof

Use two designated devices with the same reviewed installed release. Each needs
its own authenticated provider and a fresh test conversation. The broker owner
issues a short-lived invitation scoped to a private test bus and recipient
account; enrolling one device must not silently enroll another. Never mint a
browser role cookie or copy a broker administrator credential for this proof.

Record both release source hashes, provider versions, device principal IDs,
exact native session/thread IDs, registration IDs and the private test bus name.
Send a long unique payload from model A through its actual plugin to model B's
registration ID. B replies once using the supplied reply ID and the complete
payload. Verify identical UTF-8 bytes, correct participants, and correlated
conversation in the broker's evidence. Repeat in the opposite direction with a
new nonce and clear instructions preventing an automatic reply loop.

Exercise Claude-to-Codex and Codex-to-Claude, then any advertised same-provider
pair. An SSH-invoked API fixture qualifies the network/API path only; it does not
qualify a second model. Preserve separate evidence for native socket/SSH routing
and durable mailbox/seat operations; those addresses are not interchangeable with
bus registration IDs.

Bounded failure checks should include an unavailable destination, revocation
before delivery, a failed connection after enqueueing, and a retry with unchanged
message identity. Verify retained/queued/failed status honestly and ensure no
unrelated session receives the payload. Revoke only the test invitations/device
principals and stop only test processes afterward. Leave existing user state and
active tmux servers untouched.

## Release evidence

For each row, record artifact checksum, source commit, OS/client versions,
entry point, exact identity, message ID, delivery result, reply result and cleanup.
Use `pass`, `fail`, `unqualified`, or `not tested` explicitly. The release support
matrix must be no broader than these measured results. Keep different-release
upgrade/rollback and legacy retirement as separate gates; repeated setup of one
artifact cannot qualify them.

The separate [installed durable seat acceptance](SEAT-PROVIDER-QUALIFICATION.md)
uses a real Claude terminal in an isolated tmux server to qualify identity
creation, generic command spawning, literal input/read and an explicit-token
durable reply. It does not replace the client-discovery or two-device checks.
