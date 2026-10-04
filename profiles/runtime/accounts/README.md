# Optional managed provider accounts

This is the working account launch, rotation, rebalance, and pane observer code
from Anu, with packaged paths and explicitly configured service endpoints. It
preserves the `@anu_*` state protocol, generation and launch nonce checks, per-pane
locks, rate-limit backoff, and exact saved session IDs. These compatibility names
are intentional. This module is independent of agent bus identity and messaging.

Enable it explicitly with `com8 profile install --accounts` (add `--terminal`
for shell aliases). This installs `com8-account`, `com8-account-pane`, and
`com8-account-secrets`. Installation does not authenticate, contact a service,
alter provider settings, or start an observer. Generic profiles retain native
provider authentication. Private overrides belong in
`~/.config/com8/profiles/local.sh`, which upgrades preserve.

| Setting | Purpose/default |
| --- | --- |
| `COM8_USAGE_URL` | Existing usage API implementing `/api/usage` and `/api/usage/pick`; no default endpoint |
| `COM8_USAGE_HOST`, `COM8_USAGE_REPO` | Explicit SSH host and repository for optional account registration; no default host/repository |
| `COM8_SECRETS_CONFIG` | Existing secrets client configuration; default `~/.config/com8/secrets.toml` |
| `COM8_SECRETS_STATE` | Existing credential/cache state; default `~/.local/state/com8/secrets` |
| `COM8_SECRETS_BIN` | Optional executable replacing the packaged secrets client |
| `COM8_ACCOUNT_TOKEN_PATH` | Existing store folder; default `/anu/agents/claude/accounts` |
| `ANU_ACCOUNT_CACHE` | Per-device token cache; default `~/.local/state/com8/accounts/tokens.json` |
| `ANU_ACCOUNT_LOCKDIR` | Handoff locks; default `~/.local/state/com8/accounts/locks` |
| `ANU_ACCOUNT_CODEX_DIR` | Per-device Codex account homes; default `~/.local/state/com8/accounts/codex` |
| `ANU_CODEX_BASE_CONFIG` | Optional replacement base configuration; normally `~/.codex/config.toml` |
| `ANU_NOTIFY_DIR` | Optional existing observer state directory; otherwise private COM8 state |
| `ANU_PANE_DIR` | Optional existing file reply directory; default `~/.local/state/com8/pane` |
| `COM8_PROFILE_WATCH=1` | Explicitly allow the terminal profile's per-server observer hook |
| `COM8_BOX_LAUNCHER` | Explicit container executable; `--box` profile supplies `com8-box` |

XDG configuration/state roots are honored where shown by the launchers. Legacy
`ANU_USAGE_*`, `ANU_SECRETS_CONFIG`, `ANU_SECRETS_STATE`, and `ANU_SECRETS_BIN`
remain accepted; the corresponding `COM8_*` setting takes precedence. Existing
secret-store realm and key names remain unchanged (`ANU_SECRETS_REALM`, optional
`ANU_SECRETS_STORE` / `ANU_SECRETS_OP_VAULT`). The account module sets its managed
launcher and observer paths before loading private overrides. Existing paths can
be selected without copying credentials into the package.

`com8-account help` lists account operations. A named Claude launch uses the
local token cache first. An automatic pick consults the configured usage API;
the existing bounded last-pick and native-keychain fallback behavior is retained
when that API is unavailable. An exhausted pool remains distinct from an
unreachable API. Add and remote sync are explicit writes. With no sync host
configured, `com8-account sync` only warms the local cache. Use
`com8-account add --no-register NAME` when no usage registry is configured.
A named remote sync writes that remote host's default COM8 cache path; if its
private launcher selects a custom `ANU_ACCOUNT_CACHE`, run `com8-account sync cache`
on that host to warm the selected path. Codex login is per
device; its account homes share conversations with `~/.codex` while retaining
separate authentication and rendered configuration. Add, launch, and explicit
doctor repair can reconcile those homes; profile installation itself cannot.

Managed host Claude launches merge SessionStart and StopFailure hooks into
per-launch settings without writing global settings. User-supplied JSON or a
settings file is preserved. A user setting that disables hooks remains a user
override; it removes hook-based evidence. Codex's existing account-local hook
rendering and trusted hashes are retained. Installed paths with spaces are
quoted. A contained Claude launch requires an executable box adapter; contained
Codex remains refused because its required quota hook/rollout evidence is not
available through that path. Containers retain the observer's existing screen
confirmation fallback and do not receive invalid host hook paths.

The observer can report state, reliably send pane input, notify, maintain its
needs ledger, and run the existing rotation/rebalance loop. It does not import
swarm construction, browser/display commands, or general orchestration. No
observer starts until the private watch setting is enabled and the configured
tmux server loads its profile. Existing `@anu_autorotate` and `@anu_rebalance`
options continue to control behavior. Run only one observer per server during
migration; installation does not stop a legacy observer.

`com8-account-pane reply ID -` stores a literal stdin reply atomically under
`$ANU_PANE_DIR/replies/ID`, with owner-only permissions and the caller's correlation
ID. `reply ID -f FILE` and inline text are also supported; `reply gc --older DAYS`
explicitly prunes old replies. This verb needs no tmux or provider. For optional
container replies, configure `COM8_BOX_PANE_BIN="$COM8_PROFILE_RUNTIME/accounts"`
(this installed directory contains the executable named `pane`) and set
`COM8_BOX_PANE_DIR` to the same private directory as `ANU_PANE_DIR`. The box mounts
code read only and reply state separately. No legacy source checkout is needed.

The secrets client supports only `get`, `set`, `mkdir`, and `env` against an
already configured store. It includes no hosting, admin enrollment, or root-store
creation/deletion. Root credentials are read from the configured existing
Keychain/libsecret/file/1Password backend; short-lived Infisical tokens are
cached privately. Values sent by `set` use a private dotenv file, retaining the
donor's limitation on embedded newlines. The existing Infisical CLI receives its
short-lived access token as an argument; root credentials do not travel in argv.

The donor fixture suite exercises 1,276 assertions with fake providers, tmux,
network, and stores. Installed boundary tests cover fresh home selection, settings
merging, paths with spaces, Codex hook paths, refusal without containment, and
credential cache/value transport. These are regression checks, not a claim that
a real provider account handoff, quota event, remote registry, or secret service
has been exercised after migration.
