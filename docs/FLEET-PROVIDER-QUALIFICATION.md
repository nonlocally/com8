# Two-device actual-provider qualification

`scripts/qualify-provider-fleet.py` implements the two-device contract in
[provider qualification](PROVIDER-QUALIFICATION.md#two-model-two-device-proof).
It requires an actual Claude Code session on one designated SSH device and an
actual Codex app-server thread on another. Both must use the same reviewed,
extracted release through normal installed-plugin discovery.

A local self-test, a successful SSH connection, enrollment, registration, or a
`queued` receipt does **not** qualify model consumption. A live passing report
requires both models to originate a challenge through `bus_send`, the receiving
model to call `bus_reply`, and complete literal UTF-8 bytes and conversation IDs
to agree with the broker. Each direction uses a fresh 5,478-byte payload with
random nonces, newlines, quotes, backslashes and shell metacharacters.

## Ownership and prerequisites

Coordinate exclusive use of the prepared provider homes with their device
owners before running `run --run-live`. `plan`, `bundle` and `self-test` make no
remote calls or provider requests. The live switch authorizes remote staging,
setup in the disposable homes, model requests, and removal of those homes.

| Owner | Objects it may create or change | Cleanup |
| --- | --- | --- |
| Coordinator | Fresh private temporary broker database; unique private bus; one-day CA and server key; run-specific SSH control sockets and forwards | Revoke test principals and unused invitations; stop its TLS server and SSH processes; delete keys, credentials and temporary state |
| Claude device worker | Fresh temporary script/state/socket workspace; explicitly disposable authenticated HOME; normal `setup --claude --no-service`; one fresh streaming session | Close that provider process group; cooperatively stop its adapter; uninstall its integration; remove the leased HOME including its prepared authentication |
| Codex device worker | Equivalent objects for `setup --codex --no-service`; fresh exact app-server thread and explicit resumes | Equivalent cleanup; no persistent tool approvals |
| Evidence owner | New private coordinator and device evidence directories | Retain for review; never publish raw transcripts or configuration |

Existing Anu configuration, plugins, tmux servers, launchd/systemd jobs, live
client homes, source checkouts, and configured production brokers are outside
this ownership contract. The harness does not install services, copy provider
authentication, change SSH configuration, clone repositories, or transfer the
broker administrator credential to a device. Provider authentication must be
prepared privately on **each respective device** before handoff.

The coordinator needs Python, OpenSSL, and working authenticated SSH aliases
with verified host keys. Devices need Python compatible with the release,
`lsof` access to their SSH forwarding listener, their respective authenticated
provider CLI, the release's normal installation prerequisites, and Python's
normal HTTPS trust roots. SSH forwarding must be permitted by the existing
configuration. Missing prerequisites leave the live proof unqualified; cleanup
failures make its report fail. No missing prerequisite is a passing gate.
The coordinating operator must also arrange a cleanup backstop for both prepared
authentication homes: a failure before the harness contacts a device cannot
claim or remove that device's home. Once a worker validates its disposable HOME,
it claims the run lease before artifact or evidence-directory validation, so
those later preflight failures still remove its prepared authentication.

## Transport and enrollment

The coordinator constructs a broker from the checksum-qualified release, with
fresh explicitly owned state, and serves the release's HTTP handler through a
TLS socket on literal `127.0.0.1`. It uses the release's `BusHTTPServer`, including
its normal startup behavior; no DNS or TLS verification workaround is applied.

Each device gets a separate, short-lived, single-use invitation for the unique
private bus and an owner-assigned recipient account. The worker invokes the
release's existing `bus connect` parser/implementation with that invitation in
memory. Invitation secrets travel over SSH stdin, never in command arguments or
printed reports. Registration must belong to the enrolled device principal,
with no administrator privileges and only the selected bus grant. A revoked
invitation and replay of the redeemed invitation must both be rejected.

For each device the harness creates its own SSH master with configured forwards
cleared, then adds exactly one reverse forward:

```text
remote 127.0.0.1:<allocated port>
  -> authenticated SSH connection
  -> coordinator 127.0.0.1:<TLS broker port>
```

Every owned SSH invocation sets `ForwardAgent=no`. Only the local SSH processes
may use the coordinator's existing authentication agent; workers and providers
discard `SSH_AUTH_SOCK` and `SSH_AGENT_PID`. The control-socket command that adds
the forward uses `-F /dev/null`, preventing configured forwarding directives
from being reapplied after the private master has authenticated the alias.

The remote origin is `https://127.0.0.1:<allocated port>`. Its certificate has the
literal loopback IP as its subject alternative name. The worker validates the
certificate chain, hostname, and exact expected leaf fingerprint. Only the CA
certificate is sent to the devices. It is added to a private bundle containing
that device's normal trust roots, preserving HTTPS trust for provider requests.
`SSL_CERT_FILE` affects only the disposable worker and its children; no system
trust store is changed. `lsof` must confirm that the remote forward listens
exclusively on `127.0.0.1`. A server configuration that overrides the requested
bind, an unavailable listener inspection, certificate errors, or a mismatched
leaf stops qualification. There is no insecure-bind or skip-verification mode.

This measures verified TLS over explicitly configured SSH forwarding. It does
not qualify a public HTTPS deployment, its gateway, or its browser SSO. SSH is
the transport for this **bus** test; this is not native socket/SSH addressing or
durable mailbox/seat link qualification.

## Prepare the private run configuration

Use absolute paths and a new evidence directory for every run. Runtime paths
must refer to extracted artifacts outside source checkouts. Archive SHA-256,
release source, complete runtime file hashes, and the archive's release manifest
are checked on the coordinator and on both devices. Mixed candidates are
rejected.

The following is a configuration shape, not a credential template. Replace the
placeholders with the reviewed candidate and coordinated paths; keep it outside
source. All three archives must have the same reviewed checksum.

```json
{
  "runtime": "/private/release/com8-0.3.0",
  "archive": "/private/release/com8-0.3.0.tar.gz",
  "archive_sha256": "<64 hexadecimal characters>",
  "source": "<full reviewed release source commit>",
  "timeout": 360,
  "devices": {
    "claude": {
      "ssh": "provider-device-a",
      "python": "/opt/homebrew/bin/python3",
      "path": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
      "provider_bin": "/private/provider-bin/claude",
      "runtime": "/private/release/com8-0.3.0",
      "archive": "/private/release/com8-0.3.0.tar.gz",
      "client_home": "/private/fleet-claude-home",
      "disposable_home": true,
      "evidence": "/private/evidence/fleet-claude-run-1",
      "recipient_user": "qualification-claude"
    },
    "codex": {
      "ssh": "provider-device-b",
      "python": "/opt/homebrew/bin/python3",
      "path": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
      "provider_bin": "/private/provider-bin/codex",
      "runtime": "/private/release/com8-0.3.0",
      "archive": "/private/release/com8-0.3.0.tar.gz",
      "client_home": "/private/fleet-codex-home",
      "disposable_home": true,
      "evidence": "/private/evidence/fleet-codex-run-1",
      "recipient_user": "qualification-codex"
    }
  }
}
```

`provider_bin` is optional if that device's noninteractive PATH resolves its
provider. `path` supplies reviewed absolute directories for the disposable
worker and its children; it never edits a shell startup file. Both Node and the
provider executable must resolve and complete `--version` before setup.
`codex_profile` can select an already prepared profile in the isolated
Codex home. The home must be owned by the remote account, mode 0700, and separate
from its live home, runtime, worker workspace and retained evidence.

For a provider that needs an environment credential, the device owner may
prepare a mode-0600 JSON file **inside its disposable home** and set
`auth_env_file` to its absolute path. Supported keys are
`CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` for Claude or `OPENAI_API_KEY` for
Codex. The worker reads it privately, does not return its contents, and deletes
it with the owned home. Credentials are never inherited from the coordinator
or copied between devices. Do not put credential values in the run config.

## Local validation and live invocation

```sh
python3 scripts/qualify-provider-fleet.py self-test /private/release/com8-0.3.0 \
  --archive /private/release/com8-0.3.0.tar.gz \
  --archive-sha256 <reviewed-sha256>
python3 scripts/test-provider-qualification.py

python3 scripts/qualify-provider-fleet.py plan /private/fleet-run.json
```

The self-test verifies the real archive's top-level manifest separately from its
vendored manifest. It exercises policy rejection, exact resume authorization ordering,
verified TLS rejection cases, real local enrollment scope and revocation,
correlated synthetic message bytes, standalone worker EOF/failure cleanup, and
HOME ownership leases, including early validation failures. It uses SSH's local
configuration parser to confirm that configured extra forwards and agent
forwarding are excluded. A child-spawning installer fixture must lose both its
leader and TERM-resistant child on timeout before HOME deletion can pass.
Unconfirmed process cleanup must retain the HOME. Its report explicitly says actual providers are not
tested. Its synthetic API endpoints never appear in live qualification.

After the remote ownership/authentication handoff:

```sh
python3 scripts/qualify-provider-fleet.py run /private/fleet-run.json \
  --evidence /private/evidence/fleet-controller-run-1 --run-live
```

The run streams only `qualify-provider-fleet.py` and the sibling
`qualify-provider.py` helper into a fresh `/tmp/com8-fleet-*` workspace on each
device. It does not need a checkout. To inspect or transfer those two files
separately, create an optional standalone bundle:

```sh
python3 scripts/qualify-provider-fleet.py bundle /private/fleet-worker.tar.gz
```

The implementation reuses the helper's artifact validation, actual tool-event
parsing, provider lifecycle, exact-thread app-server protocol, and one-call
approval checks. Its narrow approval wrapper requires the run's exact private
bus before delegating to the original policy. Claude exposes only the four
required bus tools, with built-in tools disabled. Codex retains its read-only
sandbox and disabled shell, with approvals matched to the exact pending
installed-plugin tool, thread, peer, message and reply ID.

## Proof, bounded failures and evidence

Before either model registers, the worker records `bus status --no-start` and
verifies its enrollment. The broker record must match the model's fresh Claude
session ID or verified Codex `thread/start` ID and its own principal. Distinct
hardware/OS identity hashes establish that two SSH aliases did not resolve to
one device. Provider versions, OS details, release source/checksum, principals,
session/thread IDs and registration IDs are retained in the private report.

The two directions use actual model sends and actual model replies. The
controller cannot call `send`, `reply`, `poll`, `poll_device`, `ack` or model
registration through its owner interface. It reads broker message/receipt rows
using SQLite read-only mode. Even an administrator cannot use the participant
receipt API to read these devices' messages; device credentials stay on their
own devices. Actual tool records, full literal bytes, correct participants,
message IDs, and conversation IDs must all agree.

The Codex registration turn finishes and its process exits **before** the
Claude challenge is enqueued. Exact reply permission and the verified thread ID
are supplied **before** `thread/resume`, which may start queue consumption
immediately. An already started queued turn can finish; it is never killed to
make an explicit-resume claim. If needed, a nudge prompts the same thread to
consume its queue. The report records which turn path occurred. Queueing a raw
answer back to Codex is reported separately from the receiving model's proven
consumption of a challenge; it is not autonomous desktop wake.

Bounded checks run against only these test principals and sessions:

- The coordinator pauses the Codex adapter before the Claude model sends. The
  broker retains the message as `accepted`. After enqueueing, the owned SSH
  forward is stopped; both a failed TLS connection and a failed adapter poll
  are observed. Reconnecting delivers the **same message ID** as `queued`, and
  the resumed model supplies its exact correlated reply. The controller never
  resends or acknowledges that message.
- Codex calls `bus_send` once to a unique nonexistent registration. Its tool
  must return an error and the broker must contain no extra delivery.
- Claude creates a final message while the Codex adapter is stopped. Revoking
  only the recipient test principal before it can fetch changes the message
  from `accepted` to `cancelled`. Subsequent credential use must be denied.
- Revoked and already redeemed invitations cannot enroll another device. At
  cleanup all test principals and unused invitations are revoked.

The final broker inventory must contain exactly the four successful exchange
messages plus the one cancelled revocation message, and exactly the two model
registrations. Unexpected messages, recipients, loops, changed bytes, rejected
tool approvals, or unconfirmed cleanup fail the run.

Raw provider events and setup/uninstall logs remain in the device evidence
directories. The coordinator retains selected actual tool evidence, broker
messages, bounded-failure evidence and `report.json`. Evidence directories are
0700 and files are 0600. Only reviewed, sanitized report fields are suitable for
release notes; the raw logs and host/session metadata remain private.

Cleanup runs on success, failure, SSH EOF and handled interruption. A bounded
worker watchdog prevents indefinite unattended runs. The worker deletes a HOME
only when its run-specific ownership lease still matches and its processes are
confirmed stopped. Installer and provider-version commands own new process
groups; timeout/interruption sends TERM, escalates to KILL if needed, and confirms
the group is gone. The same verified group shutdown runs before closing provider
pipes, including when a provider's leader already exited but a child remains.
Owned SSH groups also get verified TERM/KILL shutdown; an unconfirmed group
preserves its diagnostic workspace. Failed cleanup is reported and diagnostic state is retained
for the owning operator; it is never relabeled a successful qualification.
An unhandled machine loss or forced kill still requires operator inspection of
the recorded evidence and run-specific `/tmp/com8-fleet-*` paths. Do not use
broad process kills, remove unrelated state, or retry with an occupied home.

A passing row covers only Claude Code streaming plus Codex app-server installed
plugins across two enrolled devices over this transport. Same-provider pairs,
desktop discovery/wake, native SSH routing, durable links/seats, public gateway
SSO, upgrades, and service lifecycle remain separate gates.
