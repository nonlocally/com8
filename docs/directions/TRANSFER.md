# Direction: transfer

**Status: a proposal.** Nothing in this document is implemented except the
existing pieces named under [What exists today](#what-exists-today). It describes
where the work is going and in what order, so it can be argued with before code
is written.

## The problem

Agent sessions pile up on the device where they were started, which is usually
a laptop. The laptop runs out of memory, sleeps when its lid closes, and takes
every session with it when it leaves the network. Meanwhile other devices on the
same mesh sit idle with far more capacity and never sleep.

Moving a set of sessions to another device by hand works. It is also re-derived
from scratch each time: which transcripts, which directories, which windows,
which launch flags. What gets forgotten is lost without a word, and nothing
records what was moved or how to bring it back.

Three things are wanted:

1. **Transfer.** Move a running agent session, or a whole tmux session of them,
   to another device and carry on there, with a written account of what moved,
   what was set up again, and what could not survive.
2. **A device chooser.** For every session, a ranked list of the devices on the
   mesh that could serve it, with reasons. The same idea as
   [model-switcher](https://github.com/aadarwal/model-switcher)'s account
   chooser, applied to devices instead of subscription accounts.
3. **Parity.** A running account of what each device has (client versions,
   agent configuration, knowledge, tools) compared with a reference device,
   and a way to bring a device up to it. A device that has drifted is a worse
   place to send a session, and nobody notices until a session arrives.

**Who decides.** The ranking and the comparison are computed automatically. A
transfer or a change to a device happens only when a person runs it. That is a design rule for this direction, not a missing
feature: see [Who decides](#who-decides).

## The principle

An agent's address must not say where it runs. Which device serves a session is
a placement decision that can be made again later. Everything below follows
from treating the device as a property of the session rather than as part of
its identity.

## What a session is made of

A transfer has to account for six things. COM8 already names the first three
(the comment above `move` in `lib/com8.py` calls the transcript the agent's
mind, and its workspace "the material the agent works on").

| Part | What it is | On transfer |
| --- | --- | --- |
| **Mind** | The provider's conversation state: the transcript, its side directory (subagent and tool-result files), file history, and the project's memory directory | Copied and checksummed |
| **World** | Every directory the session works in: repositories and their worktrees, ignored data, directories that are not repositories, loose files | Copied to the same absolute paths |
| **Address** | Its durable identity and mailbox, and its bus registrations | Identity and mailbox move; registrations are made again by the resumed session |
| **Seat** | Its tmux position (session, window, pane geometry, names) and its launch command (model, effort, permission flags) | Rebuilt on the target |
| **Reach** | What it talks to from where it sits: SSH hosts, credentials, MCP servers, installed tools | Checked against the target; anything missing is reported before the move |
| **Volatile state** | Background shells, monitors, subagents, scheduled wake-ups, open connections, scratch files | Lost. Listed in the plan; the agent is told on arrival |

The world is the part that tools usually get wrong, because it is tempting to
equate it with the session's working directory or with one Git repository. In
the first real workspace examined for this direction (seven sessions in four
windows):

- five of the seven sessions had a parent directory or the home directory as
  their working directory, and did their work in repositories beneath it;
- the worlds came to about 8 GB across four roots. Two roots were not Git
  repositories. One repository had 25 uncommitted paths on a branch with no
  upstream. Another had seven worktrees and thousands of ignored paths;
- the minds came to about 1 GB. One session's transcript was 220 MB with a
  387 MB side directory;
- one session had made over a thousand SSH calls to a host the target device
  had no way to reach;
- the client and tmux versions differed between the two devices, and the two
  tmux versions print a window's layout in different formats.

So a transfer copies files, never Git objects alone; it derives the world from
what the session actually touched; and it checks reach before it moves anything.

## What exists today

| Piece | Where | What it does | What it leaves open |
| --- | --- | --- | --- |
| `com8 move NAME DEVICE` | `lib/com8.py` | Moves a durable identity: transcript by rsync, mailbox by a staged merge, and the address by `depart` (the name becomes a proxy at the origin) and `arrive` (claimed at the target). Refuses when the workspace is absent on the target | Requires the session to be stopped already. Does not copy the workspace, rebuild the seat, or handle a session that has no durable identity |
| Snapshots (`tss`, `tsr`) | `profiles/runtime/fns/snapshots` | Saves and rebuilds a whole tmux workspace with a resume command per agent pane, on one device | Same device only. Finds a Claude session by guessing the newest unclaimed transcript for a directory |
| Mesh probe | `profiles/runtime/fns/mesh` | Reads a device's cores, load, memory, disk, uptime, tmux sessions and installed clients over SSH | Collects facts but ranks nothing, and knows nothing about what a session needs |
| Remote seats | `com8 seat spawn --device` | Starts a command in a seat on a linked device | |
| Device links | `com8 daemon pair USER@HOST` | Enrolls another of your devices | |

Transfer is mostly composition. It adds the world, the quiesce-and-resume step
for a live session, the seat across devices, the plan, and the chooser. It
reuses `depart` and `arrive` for the address and invents no transport: bytes
travel over the SSH connection a device link already has.

## Approaches considered

1. **Place sessions well at launch and never move them.** Cheap and safe, since
   nothing exists yet to move. It does nothing for the sessions already running,
   which is the case that prompted this. Kept as a later use of the same chooser.
2. **Keep the world on a shared or continuously synced filesystem.** Moving
   would then be "stop here, resume there". Two-way sync of many gigabytes of
   working trees with writers on both sides produces conflicts that are hard to
   see and harder to undo. Rejected. Transfer keeps the bounded form of it: a
   one-way copy for one move.
3. **Run every session in a container and move the container.** The cleanest
   form: the world is one volume, and paths, user and tools are identical on
   any device. COM8 has a [contained execution](../CONTAINED-EXECUTION.md)
   adapter, but most sessions today run on the host. A later tier: contained
   sessions should be the easiest kind to transfer.
4. **Checkpoint and restore the process.** Not available on macOS, and not
   needed: both clients can resume a conversation from its transcript.
5. **Resume-based transfer.** Stop the client at the end of a turn, copy mind
   and world, resume the same conversation on the target. Works for host
   sessions as they are, from pieces COM8 already has.

**Chosen: 5**, with 1 and 3 as later uses of the same plan and chooser.

## The transfer

### Units

The unit is one agent session in one pane. A selection can be a pane, a window,
a tmux session, or a list of them. When the selection is a window or a session,
its names and pane geometry travel with it.

A session is identified by the exact session id the client itself recorded for
that process, never by guessing the newest transcript in a directory.

A selection can also name conversations that are not running, by id. There is
no client to stop, so they skip the quiesce step and lose nothing volatile.
Their seats come from a saved layout when there is one, and otherwise each
gets a window of its own.

### The plan comes first, and is read-only

`com8 transfer plan` writes a manifest and changes nothing. It contains:

- **the sessions**: exact ids, launch commands, pane positions, whether each is
  idle and whether it has work running in the background;
- **the minds**: files and sizes;
- **the worlds**: the roots derived from each session's own tool calls (the
  paths it read, wrote and ran commands in), each with its size, whether it is
  a repository, and its uncommitted and ignored content. A person can add or
  remove roots;
- **co-tenants**: live sessions outside the selection that work in the same
  roots. Moving a root out from under them forks it. The plan names them, and
  the person chooses to include them, to copy the root and accept two diverging
  copies, or to stop;
- **the target preflight**: reachable and linked; same home path; client
  installed, signed in and not older than the source's; tmux present; disk room
  for what will be copied; nothing different already at the destination paths;
  and, for each host or tool the session has used, whether the target has it;
- **the contract**: three lists. What moves. What will be set up again. What
  will be lost.

The contract is the formal part. A person approves a transfer knowing exactly
what will not survive it.

### Phases

Each phase is recorded in the manifest and can be repeated safely. A run that
stops can be continued or undone from the manifest.

1. **Pre-copy.** While the sessions keep working, copy minds and worlds to the
   target. This is the long part, and it costs no downtime.
2. **Quiesce.** Wait for each session to reach the end of a turn with nothing
   in the background. A session with background work is asked, by message, to
   end its turn with a short note of what it will need to start again
   (watchers, deadlines). Then the client is asked to exit its own way. A
   session that does not become idle is skipped and reported, unless the person
   forces it after seeing what would die.
3. **Final sync.** Copy what changed since the pre-copy, now that nothing is
   writing. Verify minds by checksum and worlds by a comparison that must find
   no difference.
4. **Cutover.** `depart` at the origin and `arrive` at the target, as `move`
   does today. Mail sent during the move is delivered at the target.
5. **Rebuild and resume.** Recreate the tmux session, windows and panes from
   recorded geometry (not from a layout string, whose format differs between
   tmux versions). Start each client with its recorded launch command and
   `--resume` for the same conversation.
6. **Arrival.** Each session is told that it moved: from where, to where, what
   it can no longer reach, and what was lost. By default this arrives as
   session-start context, so no turn is taken and nothing is typed into a quiet
   pane. A session that left a note of things to start again is resumed with
   that note as its prompt.
7. **Verify.** Each target session reports the same session id and a live
   route. A message to its address gets a correlated reply.
8. **Tombstone.** The origin's panes say where the session went and how to
   attach to it or bring it back.

### What stays at the origin

The clients have exited, so their memory is free. The files stay. Each moved
root gets a marker naming where it went, and a ledger records every departed
session id, so resuming a departed session at the origin can be caught rather
than silently forking it. Transfer never deletes anything at the origin.
Cleaning up is a separate decision made by a person.

### Failure and return

- Before cutover, the origin is authoritative. Undo means resuming the exited
  clients in their own panes from the manifest. Whatever reached the target is
  left there, marked incomplete.
- After cutover, the target is authoritative. Going back is a transfer in the
  other direction. The markers make it a copy of differences only.
- Nothing is overwritten. If a destination path holds something this transfer
  did not put there, that root is refused until a person decides.

### A session that moves itself

An agent may be in the selection it is helping to move. The run therefore
executes in a detached worker outside every client's process tree, the way
model-switcher's recovery worker does. An agent can produce a plan. It cannot
start a run.

### Paths, users and versions

The first version preserves absolute paths, which is what lets a conversation
resume without rewriting anything it remembers. A device with a different home
path is not eligible. A path map (and with it Linux targets for macOS sessions)
is later work; the arrival notice is where the agent would learn the mapping.

Permission flags on the launch command are carried exactly as the plan shows
them. Approving the plan is approving those flags on the new device.

### Privacy and consent

Transcripts can hold secrets, and a target may be a machine other people
administer. The plan states whether the target is shared. Everything a transfer
creates is private to the user, and bytes travel only over SSH between the two
devices, never through a hub.

Being linked to a device does not mean it has agreed to host sessions. Hosting
is a third permission, separate from message permission and seat-control
permission, and a device's owner sets it.

## The device chooser

model-switcher separates four things: readings, a chooser that turns readings
into a ranking, triggers, and a move. The same separation applies here.

| | model-switcher | transfer |
| --- | --- | --- |
| Resource | A subscription account's usage room | A device's capacity and reach |
| Unit moved | One session in one pane | The same; sets travel with their layout |
| Reading | The provider's usage windows | Device facts, and what the session needs |
| Chooser | Eligibility, then a fixed order | The same shape |
| Forced trigger | A usage wall | None yet |
| Elective trigger | Rebalance at the end of a turn, automatic | Advice only; a person runs every transfer |
| Cost of a move | Close to nothing: same files, same machine | Real: gigabytes copied, volatile state lost |
| Safe moment | The end of a turn | The end of a turn with nothing in the background |
| Pin | A hand pick holds the session on its account | The same, for a device |
| Reserve | Weekly capacity an account's owner keeps | Capacity a device's owner keeps |

The difference in the cost of a move is why the trigger differs. Switching
accounts relaunches a client over the same files. Switching devices copies the
world and drops everything volatile. A better device has to be better by a
wide margin before a move pays for itself.

### Readings

**Device facts**, extending what the mesh probe already collects:

- whether it stays up: mains or battery, and whether it sleeps;
- headroom: memory pressure and swap, load per core, free disk;
- how many agent sessions it already serves;
- which clients are installed, their versions, and whether they are signed in
  with usage room;
- whether it is the user's own or shared with other people (declared, not
  detected);
- its owner's policy: whether it accepts sessions, and how much it reserves;
- its reach: which hosts it can open a connection to.

**Session needs**, read from the session itself:

- the hosts, tools and MCP servers it has actually used, weighted to recent use;
- its measured memory;
- the size of its mind and world, and how much of that the candidate device
  already holds.

### Eligibility

A device is out, with the reason shown, if any of these fails: it is reachable
and linked; it has the same home path; the client is installed, signed in and
new enough; there is disk room for the copy and memory room for the session;
its owner accepts sessions and the reserve is not reached.

A person can still choose an ineligible device by hand where the failure is a
policy rather than an impossibility. The tool says once why it would not have.

### Ranking

Among eligible devices, in this order. Each step only breaks ties left by the
one before it:

1. **Fewest unmet needs.** A device that cannot reach something the session
   uses ranks below one that can.
2. **Stays up.** A device on mains that does not sleep ranks above one that
   runs on a battery or sleeps.
3. **Headroom.** A device under no memory or load pressure ranks above one
   that is. Compared as bands, not raw numbers, so the order does not flip on
   noise.
4. **Less to move.** The current device has nothing to move, so a session
   stays put unless another device wins an earlier step.
5. **Own before shared.** Someone else's machine loses ties.
6. **Closer to the person**, for panes someone types into.

The current device is ranked with the rest. A session shows a better device
only when one wins on steps 1 to 3. Steps 4 to 6 never suggest a move on their
own.

This order is a first proposal. It is meant to be tuned against the record
described next, not defended.

### Where it shows

- `com8 transfer status`: a devices table, and a sessions table whose last
  column is the device the chooser would pick, with the reason in a few words.
- A picker for choosing a target by hand, ranked, with a mark on the chooser's
  pick and the reason beside every device it ruled out.
- A pin: a session placed by hand stays where it was put.

## Parity: keeping devices able to serve

A device serves a session well only if it has what the session's own device
had: the client at a compatible version, the same agent configuration, the
same knowledge, the tools the session runs. Today that drifts unobserved. In
the workspace examined, the two devices differed in client version, tmux
version, installed plugins and where checkouts live.

Parity is the chooser's idea pointed at the device instead of the session.

**A bill of materials per device**, read automatically, in layers:

| Layer | Examples | Compared by |
| --- | --- | --- |
| Clients | Claude Code and Codex: version, and how each was installed | Version |
| Agent configuration | User instructions, skills, plugins and their marketplaces, MCP servers, hooks, permission settings | Item by item, by version or content |
| Knowledge | Per-project memory directories | File by file |
| Tools | tmux, Git, rsync, language runtimes, the commands sessions have actually run | Version |
| Shell and Git | The workstation profile, Git identity and hooks, SSH host aliases | Item by item |
| Credentials | Which sign-ins and keys exist | Presence only, never content |

**A reference device.** One device is named the reference, usually the one
where work happens first. Every other device is compared with it. Each item is
the same, behind, ahead, missing, extra or different. A device that is ahead on
an item is never downgraded.

**A plan and an apply**, under the same rule as transfer. The comparison is
computed automatically, and can run on a schedule. Applying it is run by a
person. Each kind of item has one way of being applied:

- **Copy**: files that are safe to place and easy to remove again. Skills,
  memory files that are absent on the device, user instructions (with a backup
  of what was there).
- **Run the owner's installer**: a client update or a plugin install. Parity
  runs the tool's own command and never copies its files.
- **Report only**: anything system-wide on a machine other people use, and
  anything that needs a credential.
- **Never**: credentials themselves.

Whatever parity applies is recorded in an ownership ledger with a backup of
what it replaced, so it can be undone. This is the profile installer's rule.

Configuration files are not copied whole. A settings file holds paths and
hooks that are right on one device and wrong on another, so parity compares
and applies the items inside it.

**How it meets transfer.** The target preflight is a parity comparison scoped
to what the selected sessions need. An unmet need in the chooser is a parity
gap. A transfer plan lists the gaps and says which of them `parity apply` can
close.

## Where state lives

Everything on a device that shapes an agent session is one of four kinds, and
each kind has one rule. Transfer and parity between them cover all four.

| Kind | Examples | Rule |
| --- | --- | --- |
| Belongs to one session | The transcript and its side directory, file checkpoints, the session's saved environment and tasks | Moves with the session, by transfer |
| Shared by a person's sessions | User instructions, skills, plugins, MCP server definitions, hooks, settings; per-project memory; which directories were trusted | Converges through parity |
| Belongs to the device | Provider sign-ins and credential stores, account pools, the device's identity on a bus, SSH keys, scheduled jobs | Never copied. Each device signs in for itself. Tracked only as present or absent |
| Regenerated | Process records, sockets, caches, telemetry, shell snapshots, interface state | Never moved and not tracked |

Three things follow.

**One file can hold several kinds.** A client's global state file can hold the
account's identity, a trust decision per directory, MCP server definitions and
a great many caches, all in one JSON object. That is why parity works on the
items inside a file and never copies the file.

**The shared kind does not all flow one way.** Configuration flows from the
reference device outward. Memory is written by sessions wherever they run, so
it merges in both directions: files that exist on one side are added to the
other, and a file that differs on the two sides is reported, not overwritten.
A trust decision is carried only for a directory that a transfer brings, and
only when the approved plan says so.

**Clients differ.** One client keeps a conversation as a file and a directory
beside it. Another also indexes its conversations in its own databases, and
with several accounts keeps a home per account that share one conversation
store. Moving a conversation of the second kind means moving whatever that
client needs to find it again, which is why each client has its own adapter
and its own step in the order of work.

### What is tracked

Five records, each kept on the device it describes and gathered over SSH when
asked for. None needs a server.

1. **Transfer manifests**, with their event logs: what moved, when, verified
   how, and what was lost.
2. **Bills of materials**: what each device has, layer by layer.
3. **Reference snapshots**: the reference device's bill of materials and its
   copyable content, published as a versioned artifact. Other devices converge
   on a known version, the history of what the reference was is kept, and a
   device can be brought up to date while the reference is offline.
4. **A session index**: which device holds which conversation. Past
   conversations stay where they are and move on demand; with the index,
   "continue that one over there" is a transfer with no client to stop.
5. **A world index**: which device holds which root, copied from where, and
   how fresh.

### A store: a device that holds but does not run

A device in the list can be marked as a store. It keeps copies of
conversations and roots and never runs a session. An always-on machine with a
large disk is the natural choice.

- **Each device pushes its conversations to the store on a schedule.** A
  transcript only grows, so a push sends what was appended since the last one.
- **One live holder.** The session index records, for each conversation, the
  one device that holds the live copy, and which stores hold a copy and how
  fresh it is. Starting a conversation on another device is a transfer whose
  sender is the store: pull, then resume. A store is never mounted as a shared
  working directory, and two devices never write the same transcript.
- **A conversation outlives its device.** A device that restarts takes its
  running sessions with it. With a store, those conversations can be started
  on another device without waiting for the first one to come back.
- **What may go where.** Conversations and knowledge go only to stores their
  owner controls, because a transcript can contain anything a tool ever
  printed. Configuration is small and holds no conversation content, so it may
  also be published to a hosted private repository, where it gains history and
  can be reached from anywhere.
- **Roots.** A repository's committed history is already held by its remote.
  What a store adds is what the remote lacks: uncommitted and ignored files,
  and directories that are not repositories. Archiving those on a schedule is
  a choice made per root, since large trees are costly to keep copying.
- **The same care as the snapshot archive.** The store's volume must be
  mounted right now and be exactly a mount point, or nothing is written. A
  store that is not encrypted is reported as such.

### Ready to start, not only ready to receive

The same records answer a second question: where can a new session for this
project start right now? A device is ready for a project when the client is
signed in, its configuration is at parity, the project's root is present and
fresh, the tools that project's sessions have used are installed, and the
hosts they have used can be reached. That is placement at launch, and it
needs nothing beyond the world index, parity and the needs already derived
for transfer.

## Who decides

For now a person runs every transfer. The chooser computes and shows; it does
not act.

- `plan`, `status` and the ranking are read-only and open to agents.
- `run` requires a person: a confirmation typed at a terminal, or a manifest
  approved by one. There is no agent-callable way to start a run.
- Parity follows the same split: the comparison is open to agents, and
  `parity apply` requires a person.
- Every ranking shown and every transfer a person runs or declines is appended
  to an advice record. That record is how the rule earns trust. It shows
  whether the chooser's picks match what people chose, and whether transfers
  lost anything their plans did not predict.

Automatic transfer is out of scope until that record supports it. It would
need, at the least: a history of transfers in which nothing was lost that the
plan had not listed; advice that agrees with the choices people made; a return
trip proven on real work; and triggers limited to events that leave no better
option, such as a device about to shut down.

## Command surface (sketch)

```sh
com8 transfer status                       # devices, sessions, and the better device for each
com8 transfer plan SELECTION --to DEVICE   # write a manifest; changes nothing
com8 transfer plan SELECTION               # the same, against the chooser's pick
com8 transfer run MANIFEST                 # a person runs this
com8 transfer status MANIFEST              # phase by phase, what became of each session
com8 transfer undo MANIFEST                # before cutover: resume at the origin
com8 transfer back MANIFEST                # after cutover: the return trip

com8 parity status [DEVICE]                # each device against the reference
com8 parity plan DEVICE                    # what would change; changes nothing
com8 parity apply PLAN                     # a person runs this
```

`SELECTION` names a pane, a window, a tmux session, or several. Names are
provisional.

## Order of work

By dependency, not by date. Everything read-only comes before anything that
moves a session, so the plan and the ranking are exercised on real workspaces
before a transfer is trusted with one.

1. **Plan.** Exact session resolution, minds, derived worlds, co-tenants,
   target preflight, the contract. Useful on its own: it answers "what would it
   take to move this?"
2. **Device facts, chooser, status and picker**, with the advice record.
   **Parity** alongside it: the bill of materials, the comparison with the
   reference device, and the copy kind of apply.
3. **Transfer of one pane**: pre-copy, quiesce, final sync, resume, arrival,
   verify, tombstone, undo and the return trip. Claude Code first.
4. **Sets**: windows and sessions with their geometry, and a session that
   moves itself.
5. **Address continuity**: compose with `depart` and `arrive`, and make bus
   registrations again from the resumed session.
6. **Codex**, through the same provider interface: exact session id, idle and
   background-work checks, graceful exit, resume command, the files of a mind.

Later, each building on the above: placement at launch; origin panes that
become live views of the moved session; contained sessions; path maps and
cross-platform targets; automatic triggers.

Steps 1 to 4 need only SSH, rsync, tmux and the client on both devices. They
must not require the origin to run a particular COM8 daemon, so a device still
on an older install can be the one that sessions leave.

## Qualification

Isolated, as the repository requires: two temporary homes standing in for two
devices, a fake SSH that maps a device name to the other home, dedicated tmux
sockets, and fake clients. Covered there: a plan that changes nothing; a
derived world with a non-repository root, uncommitted and ignored files and
worktrees; a co-tenant; every preflight refusal; a run stopped at each phase
and then continued or undone; an occupied destination refused; a round trip
that copies only differences; geometry rebuilt across two layout formats.

Then on real devices, each rung only after the one before it:

1. a plan for a real multi-window session, read by a person;
2. a throwaway session, there and back;
3. one real session with no world to speak of;
4. a whole real session with several roots;
5. a session that moves itself.

A rung passes when every target session resumes the same conversation, answers
a message at its address, and the plan's lost list matches what was in fact
lost. Real-device results are environment-dependent acceptance checks. Passing
the isolated suite does not establish them.

## Open questions

- **Project memory.** A project's memory directory is shared by every session
  of that project, including ones that stay behind. The proposal is to copy
  files that are absent on the target, never overwrite a file that differs, and
  report the differences. Is a merge wanted instead?
- **Departed minds at the origin.** A ledger and a warning on resume, or
  something firmer?
- **Accounts.** Provider credentials are per device and are never copied. When
  both devices use model-switcher, should a transferred session be handed to
  the target's `ms` so that the device choice and the account choice compose?
- **Reach.** Is an unmet need ever a hard refusal, or always a ranked cost that
  a person can accept?
- **The arrival notice.** Session-start context by default and a prompt only
  when there is something to start again: is that the right default?
