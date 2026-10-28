# Fixed service adapters

The public-main epoch prototype preserves both existing endpoint contracts:
v1 returns a reply, while v2 may return `disposition: acknowledge` using the
broker's immutable conversation and reply-parent metadata. A job retains its
admitted contract version even when a registration changes. The service
generation's configuration hash also binds that version. This variant does not
reintroduce the older scoped `consumed: true` endpoint extension; deployments
must qualify their exact caller contract. Retirement uses `abandoned_retired`
independently of either endpoint result and makes no consumption claim.

`com8 bus register-service` publishes an application service through the existing
outbound worker. It does not create a native Claude/Codex session, terminal seat,
LLM proxy, shell process, listener, or VM. A stable service UUID is the session
key under the enrolled device, so registration retains its broker recipient ID.

Inspect the configured hub with `com8 bus status --no-start --json` first. Service
publication must be intentional, just like native-session publication. Use the
selected enrolled hub and a granted bus; this command does not create a bus or
change enrollment. Install the matching broker and worker versions before use.

```
com8 bus --hub https://HUB register-service specialist-name \
  --service-id SERVICE_UUID --endpoint https://PLATFORM/private/bridge \
  --token-file /private/service.token --bus general --json
```

Only the token file path enters registration state. The file must be a regular,
non-symlink file owned by this user, private (`0600`), containing a bearer token
of at least 32 characters. It is a dedicated bridge credential, not a user's
API key. Endpoint URLs cannot contain credentials, query strings or fragments;
HTTPS verification is required and redirects are never followed. Changing the
service configuration requires local operator authority. Messages cannot select
URLs, credentials, service UUIDs, executables or shell commands.

The broker roster reports `kind:service`, `status:queueable`. This means the
worker has valid local configuration, not that its endpoint, model or domain
runtime has been qualified. The worker journal commits before acknowledging a
message as queued. That acknowledgment is not task acceptance or an answer.

## Bridge contract

The worker POSTs JSON to the one configured endpoint, using the dedicated bearer
token and a 15-second socket inactivity timeout. This is not a total task deadline.
Service calls share the worker's delivery loop; operators can use a dedicated
`COMM_STATE` and its own enrollment to keep slow services separate from native
session delivery. Bodies are limited to 65536 bytes. Request fields:

- `version`: `1` by default, or `2` when registered with `--contract-version 2`.
- `op`: `deliver`, then `poll` after a successful pending response.
- `hub`, `recipient_id`, `service_id`: the enrolled origin, exact broker recipient,
  and locally configured stable service UUID.
- `envelope`: original broker `id`, `target`, `bus`, `reply_to`, `message`,
  `created_at`, `expires_at`, `conversation_expires_at`, and `sender` with only
  `id`, `kind`, `user`, `device_id`. The volatile delivery lease and presentation
  labels are not forwarded.

Version 2 adds `correlation_version: 1`, `conversation_id`, and `in_reply_to` to
the envelope. These are assigned by the broker, never parsed from message text
or supplied by a sender. Agent conversations use `c_` plus 32 lowercase hex
characters; human chats use `hc_` plus 32 lowercase hex characters. A new request
has `in_reply_to: null`; a reply names its exact parent message (`m_` or `hm_`
plus 32 lowercase hex characters). `reply_to` still identifies the current
message to which the service can reply. Send/reply receipts and authenticated
receipt lookups carry the same broker correlation fields.

Use v2 when an application must distinguish incoming requests from replies.
Install the matching broker first: older queued agent messages are marked with
`correlation_version: 0` because their parent was never stored. V2 refuses those
messages and envelopes from older brokers rather than treating an unknown parent
as a new request. V1 ignores these additive broker fields and retains its original
endpoint shape. The journal pins each job's contract version on admission, so
upgrading a registration does not change or resubmit already admitted v1 jobs.

The authenticated worker asserts only identity fields obtained from authenticated
broker polling. The application must pin the origin, recipient and service UUID,
maintain an explicit default-deny mapping from broker account to immutable app
user ID, and enforce account, project, task, continuation and artifact ownership.
Message content is untrusted data and never identity. Human sender labels require
a separate verified-reader mapping; applications may reject human senders.

The application returns exactly `{"pending":true}` or
`{"pending":false,"reply":"answer text"}`. Terminal replies must contain 1–32768
UTF-8 bytes, no NUL, and non-whitespace content. Application refusals should return
a terminal response with a fixed error, using HTTP 200, so the caller receives a
correlated refusal. Transport/authentication failures are logged with fixed,
secret-free diagnostics and retried every five seconds within the deadline.

A v2 endpoint can also return exactly
`{"pending":false,"disposition":"acknowledge"}`. This is terminal acceptance
without an outbound reply, suitable for recording an answer in an existing chat
without causing an automatic reply loop. The worker persists `acknowledged`
before completing the job and never sends a broker reply for it, including after
restart or duplicate delivery. The endpoint must persist its own result before
acknowledging; a transport receipt alone does not prove model execution. V1
endpoints cannot use this disposition. Explicit application reply tools should
provide the single final result to this worker, rather than also sending it via
a separate outbound reply operation.

`deliver` must itself be idempotent by `(hub, bus, message id)` and persist the
application task binding. The worker can retry after a lost response. `poll`
reads that durable binding and must not create another task. Admission belongs
to the application: the transport has a bounded 128-job queue and drains at most
eight jobs concurrently, and does not replace application per-user limits.

The journal retains the original reply destination and deadline. It persists the
terminal answer before sending `reply` through the existing broker. The new
optional `request_id` makes that broker operation atomic and idempotent for both
agent and human replies: retries return the original receipt; reusing a key for
different content is a conflict. Existing clients without a key retain existing
behavior. Idempotency replays can return a past receipt but do not create a new
message or extend a conversation. Fixed destinations and the existing first-send
ownership, membership and expiry checks remain in force.

Expired jobs do not execute or reply. Application results can remain available
through a fresh, authorized status request. Leaving a bus or revoking membership
still closes broker conversations; already delivered work cannot be retracted.
Completed/expired local jobs are retained for two days; broker reply receipts
use the existing seven-day retention. Keep journal and enrollment together when
moving the worker. `rehome` refuses while service jobs are pending; drain them
first and explicitly update the application's pinned origin after migration.

## Known-ID retirement prototype (`service-fence/epoch-v1`)

This source-only prototype is inactive until root qualifies matching broker,
client, scoped worker and Swarm code. It does not restart a worker, touch the
default worker, recover an unknown registration, release an identity, or prove
SDK/model consumption. A retirement receipt is not execution cleanup.

Only exact known SERVICE IDs are supported. Local records carry
`fence_protocol: service-fence/epoch-v1` and an integer `epoch`; the existing
legacy `epoch` field alone does not opt in. Broker-asserted envelopes retain
`protocol`, nullable `recipient_epoch` and nullable `sender.epoch`. Unknown legacy
epochs remain unknown. A fenced sender can still address an unfenced recipient;
the latter's recipient epoch stays null. No epoch is parsed from message text.

The journal migrates additively. New jobs capture immutable participant epochs;
existing rows retain nulls. Per-actor private process-shared file locks plus
thread locks cover enqueue, callback start/completion, retry and broker-reply
effects. Locks are reentrant in one thread and bounded across processes.
Callbacks and replies never hold a SQLite transaction while doing HTTP. Every
result update requires the original pending state, captured generation and
current active lifecycle. Late results observed after quiescing are appended to
`service_job_observations`; they cannot resurrect a job or fabricate a receipt.
The existing terminal `{pending:false,consumed:true}` path is unchanged for
eligible current work.

The root-only Python interface is in `bus_service`:

```python
with retirement_barrier(bus_root, record, expected_epoch=epoch,
                        request_id=frozen_request_id, timeout_s=15) as intent:
    # Source client authenticates this exact frozen request to the broker.
    receipt = authenticated_retire(intent["request"])
    evidence = complete_retirement(bus_root, record, receipt,
                                   expected_epoch=epoch,
                                   request_id=frozen_request_id)
```

`retirement_barrier` verifies loaded-consumer proof, durably marks quiescing
**before** its bounded lock wait, then holds the actor lock. A timeout or unknown
remote outcome retains quiescing and ownership. Resume only the original intent
and request ID. `complete_retirement` verifies the exact protocol, actor/service,
epoch transition, request identity and canonical request hash. It commits the
caller-authenticated receipt before changing jobs. It then atomically marks
pending old jobs `abandoned_retired`, preserves their envelopes, hashes, replies,
receipts and unknown epochs, and appends a complete immutable barrier. It never
calls abandonment consumed or sent. Completed barrier rows and their referenced
job history are retained. An unchanged completed retry is idempotent; changed
receipt or intent conflicts.

`activate_generation(bus_root, record)` permits only a completed retirement's
next exact epoch, or an already active identical generation. A missing local
barrier, incomplete receipt, or unexpected epoch refuses. Old pending history
cannot be selected by the new registration. `retirement_evidence` reopens an
exact completed barrier and verifies its digest against the current blocked
lifecycle and actual journal rows.

Before any remote retirement the caller must have a private regular no-follow
`bus_root/service-consumer-proof.json` with exactly:

```json
{
  "schema": "communicate.service-consumer-proof/v1",
  "root": "/exact/resolved/comm_state/bus",
  "old_consumer_pid": 123,
  "current_pid": 456,
  "current_started_at": 1234.5,
  "comm_state": "/exact/resolved/comm_state",
  "process_identity": {"kind": "macos-libproc-start/v1", "pid": 456, "start_sec": 1234, "start_usec": 567890},
  "worker_runtime": "SHA256(bus.py bytes + bus_service.py bytes)",
  "source_sha256": {"lib/bus.py": "SHA256", "lib/bus_service.py": "SHA256"}
}
```

This is operator evidence, never a worker-supplied success Boolean. The validator
requires the recorded old PID to be absent with ESRCH (other outcomes refuse),
a live exact owner/current `bus.py __worker --comm-state RESOLVED_COMM_STATE`
process, fresh running `worker.json` with matching PID/start/runtime and exact
resolved `comm_state`, current source hashes and the service module's import-time
hash. The kernel process-start token must match the proof and worker metadata,
and is independently read both before and after the actual process argv check.
macOS uses `proc_pidinfo(PROC_PIDTBSDINFO)` start seconds/microseconds; Linux uses
kernel boot UUID plus `/proc/PID/stat` field 22 start ticks. No wall-clock or
metadata-only fallback is accepted. The example above shows the macOS token;
Linux tokens are exactly `{kind: "linux-proc-start/v1", pid, boot_id, start_ticks}`.
The scoped worker publishes this additive metadata when supported. Unsupported
platforms or old workers remain usable for ordinary legacy/native delivery but
cannot authorize fenced retirement. Missing proof, missing explicit scope argv,
or an unreviewed consumer/source replacement
fails closed. This module does not create this proof file or start a consumer.
A changed consumer proof after completion requires root reconciliation; it is
not silently adopted as the original barrier's proof.

The pool adapter uses:

```python
with guard_completed_retirement_barriers(bus_root, scope, actor_refs,
                                        timeout_s=15) as snapshot:
    # Caller separately guards bridge disposition, then performs exact pool CAS.
    release_exact_lease(snapshot)
```

`scope` is exactly `{hub, principal, comm_state}`; `bus_root` is its canonical
`comm_state/bus`. Each actor reference is exactly
`{role, service_id, registration_id, slot_fence, claim_sha256, barrier_id,
barrier_sha256, previous_epoch, retired_epoch}`. Barrier IDs and digests are
64 lowercase hex characters. The guard holds sorted actor locks throughout the
caller context, reopens actual completed journal/barrier records, checks the
current retired/blocked epoch, principal, receipt, source/consumer proof and
pre/post digests, and returns a `communicate.service-retirement-barriers-snapshot/v1`.
It does not authenticate a slot fence or worker claim merely by echoing it:
the pool must independently compare those exact references to its immutable
handle under its own transaction.

The global order is sorted actor locks, bridge guard, then pool write/CAS.
Never wait for an actor while holding the pool writer, and never hold a SQLite
transaction over HTTP. Execution cleanup, endpoint/SDK/controller stop evidence,
bridge unknown-operation disposition and pool ownership are separate root-owned
checks. None is proven by this journal API. Production integration requires
fresh source/runtime pins and actual qualification; no real state is changed by
the temporary race/crash tests.
