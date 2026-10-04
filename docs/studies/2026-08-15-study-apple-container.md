# Study: Apple `container` + anu `box` — sandbox/permission primitives (evidence base for the agent-fabric design)

Date: 2026-08-15 · Method: subagent study of the local CLI + /Users/aadarwal/COM8 box wrapper
+ upstream apple/container + apple/containerization docs.
Host verified: macOS 26.5.1 (Darwin 25.5.0), arm64, `container` 1.0.0
(`/opt/homebrew/bin/container`); guest kernel kata `vmlinux-6.18.15-186`; init
`ghcr.io/apple/containerization/vminit:0.33.3`.

## 1. Isolation model

One lightweight VM per container, not a namespace. Upstream technical-overview: "it runs a
lightweight VM for each container that you create"; "Each container has the isolation
properties of a full VM." Backing: Virtualization.framework (VMs) + vmnet (network).
containerization README: vminitd "provides a GRPC API over vsock"; "Block storage uses
virtio-blk"; "shared directories use virtio-fs (one virtiofsd per share)"; sub-second start
claims — empirically a `box` start is a few seconds (the Node/Claude entrypoint dominates).

**Shared with host by default: nothing but what you mount.** No host PID/proc, no host FS
root, no Keychain, no TCC grants; `--virtualization` off (verified false in inspect).
Host-side architecture is XPC-brokered (`container-apiserver` + `container-core-images` +
`container-network-vmnet`). Guest→host attack surface = VM boundary + virtiofsd + vsock, not
a shared kernel.

## 2. Network model

Every container gets a **real IP on a vmnet L2 network** (live inspect:
`192.168.64.5/24`, gw `.1`, ipv6 fd30:…, MAC, MTU 1280; `container network ls` → `default
192.168.64.0/24`). Upstream: "Every container gets an IP address on its network, always
reachable by that IP from the host and from other containers on the same network." **Host →
container works directly, no port publishing.** Custom networks are mutually isolated and
**macOS 26+ only**; on macOS 15 "Container-to-container communication over the virtual
network is not possible."

DNS: `container system dns create <domain>` (admin) + `[dns] domain` in config.toml →
`<name>.<domain>` per container; bare-hostname lookup on custom networks currently doesn't
work. Container → host services: `sudo container system dns create host.container.internal
--localhost 203.0.113.113` (documented costs: disables Private Relay; the packet-filter rule
is removed on restart).

**Inbound SSH into a container is feasible** (routable IP + sshd in image + authorized key).
`--ssh` is the opposite direction (mounts `$SSH_AUTH_SOCK` into the guest). Port publishing:
`-p host:container[/proto]`; **`--publish-socket host_path:container_path`** publishes a
container unix socket to the host.

**Tailnet reachability from inside a box: untested and mechanism-dependent** (vmnet NAT vs
the tailscale utun routes). Must-measure, not assumed.

**Egress restriction is coarse — two options, both blunt:** `--network none` (a true air gap
— verified empirically in PixCell:
"A raw IP connect from inside fails with ENETUNREACH. `--no-dns` alone is *not* sufficient —
it drops resolver config but leaves routes",
`PixCell-running/docs/reports/2026-07-27-contained-frontdoor-validation.md:82`) and
`container network create --internal` ("Restrict to host-only network"; behavior unverified
locally). **No per-destination allowlist, no proxy hook, no L7 filter.**

## 3. Filesystem

Bind mounts are virtiofs (every inspect mount shows `"type":{"virtiofs":{}}`). Alternatives:
named volumes (ext4 block devices, better I/O), `--tmpfs` (guest-memory-only). `--read-only`
root; `ro` per-mount.

**What `box` mounts** (`COM8/config/bash/fns/box:45-58`): the git toplevel at its own real
path; and if cwd is a *linked worktree*, the main repo too (via `--git-common-dir`) — which
is what makes the worktree's absolute-path `.git` pointer resolve inside the VM, and makes
agent-emitted paths valid on both sides.

**Ownership risk is real:** the container init runs uid 0/gid 0 (inspect), `box` never passes
`--user`; virtiofs writes land as the host uid owning the mount — so **container root can
create/modify/chmod/delete anything under the mounted worktree and main repo**, `.git`,
hooks, config included. The Containerfile compensates only for git's safety check
(`git config --system safe.directory '*'`, `config/box/Containerfile:39`). Also: a
single-file bind mount cannot be renamed over — mount directories, not files.

## 4. Identity / credential hooks

`container run` primitives: `--name`, `-l/--label k=v`; `-e/--env`, `--env-file`;
`-u/--user`, `--uid/--gid`; `--cap-add/--cap-drop ALL`; `--ulimit`; `--read-only`;
`--shm-size`; `--tmpfs`; `--ssh` (agent socket into guest at
`/var/host-services/ssh-auth.sock`); `--publish-socket`. **Unix sockets cross the VM boundary
in both directions** — so a per-agent host-side broker socket (mediating pushes, secrets,
approvals) is a supported pattern. Per-container SSH keypairs are trivially injectable
(mount `/root/.ssh` or env); each container's distinct IP/MAC/hostname/name makes
`agent-N ↔ 192.168.64.N ↔ key N ↔ label N` straightforward.

**What `box` injects today — deliberately almost nothing** (box:76-77: "Nothing else crosses
from the host — no ssh keys, no gh auth"): `~/.local/share/anu/box/claude` → `/root/.claude`
(persistent login, shared across boxes); `TERM`; `ANTHROPIC_API_KEY` if set; git identity as
env only, signing disabled; the live `pane` bin at `/opt/anu/bin`; the pane reply dir at its
real path **RW + shared across all workers** (flagged in-source as trusted-worker model).
Empirical credential caveat: "Two copies of one OAuth credential cannot coexist — refreshing
rotates the refresh token" (validation report:88) → per-agent Claude auth means per-agent
accounts/keys, not copies.

## 5. Lifecycle / resources

`--cpus N`, `--memory <size>`; box defaults 2 CPU / 4G (`ANU_BOX_CPUS/MEMORY`). System
defaults in `container system property ls` ([container] 4/1gb, [machine] 5/8gb, [build]
2/2048mb rosetta=true). Images are OCI (build/pull/push/tag/save/load; registry login;
default docker.io); `--arch/--os/--platform`; `--rosetta` for x86_64. `container exec` drives
a running container — PixCell's warm-container pattern: detached `sleep infinity` +
`container exec --interactive <name> /opt/venv/bin/python -I -c …` (`verifier.py:672`). Also
logs, stats, cp, export, inspect, kill, stop, prune, `--cidfile`, `--init`, `-d`. `box` runs
`--rm`, one VM per invocation; `_box_ensure_system` starts services; `_box_build` builds
`anu-agent` from `config/box/Containerfile`; `_box_doctor` checks CLI/services/image/creds.

## 6. Honest security boundaries

**What it buys:** a hypervisor boundary against arbitrary code execution (rm -rf /, poisoned
postinstall, runaway build → confined to a throwaway VM); filesystem blast radius = exactly
the mount set; no shared kernel (runc-genre escapes don't apply); no host credentials in the
box → a prompt-injected agent can't exfiltrate what was never mounted; resource caps;
`--network none` is a genuine, measured air gap.

**What it does not protect against:** (a) network wide open by default — box passes no
`--network`; exfiltration via HTTP unimpeded; (b) the mounted worktree fully writable by
container root, including `.git` and (linked worktree) the entire main repo; (c) the shared
pane reply dir is RW across all boxes — one worker can forge another's result; (d)
`ANTHROPIC_API_KEY` and the shared persistent `/root/.claude` cross the boundary when set;
(e) on macOS 26 every box on `default` can reach every other box's IP; (f) no seccomp/LSM
surface at the CLI — caps and ulimits only; (g) no disk quota on a bind mount.

**Versus Docker Desktop / Lima:** those run one shared Linux VM for all containers, so
inter-container isolation reduces to namespaces+cgroups inside it, and an escape lands in a
VM with `$HOME` mounted and an effectively-root docker socket. Apple inverts: each container
its own VM/kernel/IP — strictly stronger for N mutually-untrusted agents. The price: far
thinner policy surface (Docker has real firewalling, seccomp profiles, userns-remap, quotas;
`container` 1.0.0 has `--network none`, `--internal`, caps, ulimits).

## 7. Box's current contract

`box` = shell function (`config/bash/fns/box:191-198`) + standalone shim
(`config/bash/bin/box:5-6`). Dispatch: build/doctor/help/else → `_box_run`, default `bash`.
**`cxc` = `box claude --dangerously-skip-permissions`** (`config/bash/aliases:34`;
agentlaunch:10 "Claude (full auto, contained in a VM)").

Inside: root in a Linux VM; 2 CPU/4 GB; `/opt/anu/bin` first on PATH (live pane kernel);
`IS_SANDBOX=1` (Containerfile:52 — "Claude Code refuses --dangerously-skip-permissions as
root unless it knows it is sandboxed"); node 22, git, curl/wget, rg, fd, jq, python3+pip,
build-essential, JupyterLab, marimo, numpy/matplotlib, @anthropic-ai/claude-code; workdir =
host cwd at the identical path.

**Git commit yes, push no** (Containerfile:7-8: "Contained agents commit locally; the host
pushes") — push fails by credential absence in both transports (no key/agent socket; no gh
token/helper).

**Escape hatches, by leverage:** (1) full internet egress; (2) write access to worktree +
main repo incl. `.git`; (3) the shared reply channel; (4) `ANTHROPIC_API_KEY` if exported;
(5) the shared `/root/.claude` credential dir; (6) `ANU_BOX_IMAGE/CPUS/MEMORY` + `ANU_PATH`
env overrides (redirect image, caps, and which pane binary gets mounted). Non-capabilities:
"a box still can't run host-only orchestration (swarm, tmux directly)."

## Primitives to build per-agent sandbox-identity on

| Primitive | Mechanism | Identity use |
|---|---|---|
| name + labels | `--name`, `-l` | stable agent handle; filterable roster |
| dedicated IP/MAC/hostname | vmnet auto | network-level identity (`agent-3 ↔ 192.168.64.8`) |
| per-network cohorts (macOS 26) | `network create` | agents that may talk only to each other |
| air gap | `--network none` | verification agents that must not phone home |
| host-only | `network create --internal` | host broker without internet (**verify**) |
| guest uid/gid | `--user` | stop container-root owning host files |
| caps | `--cap-drop ALL` | remove raw sockets/mount/ptrace |
| ro root + scoped writes | `--read-only` + `:ro` + `--tmpfs` | writable surface as explicit whitelist |
| resource envelope | `--cpus/--memory/--ulimit` | bound a runaway agent |
| env injection | `-e/--env-file` | per-agent tokens, git identity, agent id |
| socket bridging | `--publish-socket`, `--ssh`, virtiofs UDS | **host-side broker**; boxed agent as socket peer |
| per-container ssh keypair | mount/env | inbound ssh; per-agent upstream authorization |
| warm container + exec | `-d` + `exec` | long-lived agent, host-driven injection |
| DNS domain | `system dns create` | `agent-3.anu` naming; `--localhost` broker endpoint |
| content-addressed handoff | ro snapshot in, one writable out dir | the proven PixCell pattern (`verifier.py:169-174`) |

## Hard limitations to design around

1. No fine-grained egress — an allowlist must be a host-side proxy, enforceable only when
   paired with `--network none` + a socket broker as the *only* channel out.
2. `box` runs guest-root, no caps/ulimits/read-only — adding `--user`, `--cap-drop ALL`,
   `--read-only` is the single highest-value change (PixCell has the working recipe,
   `verifier.py:126-144`).
3. The linked-worktree double mount widens blast radius to the whole main repo; `:ro` is
   impossible as-is (git writes refs/objects) — isolation needs per-agent clones or a commit
   broker.
4. Shared credential dir + shared reply dir defeat per-agent identity; OAuth rotation means
   per-agent *accounts*, not copies.
5. On macOS 26 all boxes on `default` reach each other; per-agent networks are the only
   separation and also cut wanted coordination.
6. No disk quota on bind mounts; no seccomp/LSM; no userns-remap.
7. Tailnet-from-box unverified — measure first.
8. macOS 26 required for networks/DNS/container↔container.
9. `--dns-*` is not a containment control.
10. DNS domain creation needs sudo; the `--localhost` rule doesn't survive restart.

**Side note:** `~/src/QPG-MIT/PixCell-running/containers` is not a clone of apple/container —
it is PixCell's own image build context (verifier/session Containerfiles), the most advanced
local usage of the runtime and the best source of empirical security facts on this machine.
