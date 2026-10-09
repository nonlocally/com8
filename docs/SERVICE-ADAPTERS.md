# Fixed service adapters

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
