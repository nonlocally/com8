# Session view — the talk page's read-only observatory

**Date:** 2026-08-21 · **Status:** shipped (worktree-sessionview), then
**superseded the same day** by the merged timeline (worktree-timeline):
the chat/session TAB split mirrored the system's planes, not the user's
needs — the operator's immediate confusion ("shouldn't these two just be
merged?") was the evidence. The v2 rule: one timeline; mail is
authoritative for correspondence and the transcript's copies of it are
dropped unconditionally; the session is the spine for everything else.
Everything below about resolution, parsing, caching, and the trust
envelope carries forward unchanged.

## What this is

The talk page's second tab. Tapping an agent on the board and opening
`session` shows the agent's actual live Claude session — the operator's
words and the agent's prose as chat bubbles, tool calls as one-line
receipts (`$ restart the daemon…`, `✎ com8.py`), compactions as honest
dividers — rendered read-only from the transcript on disk, in the same
skin, with the mail composer persistent underneath. It answers the thing
the mail thread deliberately does not: *what has this agent been doing
and saying?* The chat tab stays the interaction plane; the session tab is
the observatory. Together they replace the old terminal-mirror wall with
the two honest halves it conflated.

## Decisions that bind

1. **Files-as-API, read-only.** The transcript JSONL is the source; the
   daemon is not involved and nothing ever writes. No new daemon ops.
2. **Resolution mirrors delivery.** name → live session via the session
   registry exactly as `_deliver_pending` finds it — name match, pid
   alive, socket present, and on collision the daemon's own chooser
   semantics (probe-live wins, then interactive, then newest; the parity
   test uses `Com8._choose_session` as its oracle). The tab must show
   the same session mail reaches, or it lies.
3. **Transcript by session-id glob, never cwd munging.** A session's cwd
   drifts (worktree switches); its id doesn't. The id is validated and
   the glob result containment-checked under the projects dir.
4. **Filter the plumbing, keep the conversation.** Kept: user text,
   assistant prose, tool receipts, compaction marks. Dropped: thinking,
   tool results, system reminders (prefix AND embedded spans),
   command/notification records. Cross-session deliveries are
   correspondence: their inner text is kept and their `from-name` is
   carried as `who` — another agent's words never wear the operator's
   "you" byline.
5. **Bounded string scans, no regex over transcript text.** Fleet mail
   lands in transcripts verbatim; a backtracking regex over
   megabyte lines measurably froze the cache lock (reviewed: 512 KB →
   79 s). Every extraction is a `str.find` with bounds.
6. **Measured cache validity.** Transcripts only grow — so parse once,
   append incrementally, and treat inode + first-256-bytes fingerprint +
   shrink as the replacement signal (a longer replacement is otherwise
   indistinguishable from an append; `beam` copies transcripts onto
   these paths). One malformed record skips that line, never poisons the
   cache. At most 1000 renderable turns held; windows below the cap say
   `truncated`.
7. **The client trusts indices, not continuity.** The API carries `sid`;
   the pane resets on a changed sid ("· session restarted ·"), marks
   index jumps ("· gap — turns evicted ·"), and says "session ended"
   when a live session goes away — silence is never the signal.
8. **Same trust envelope as mail, stated boundary.** `/api/session` is
   token-gated like `/api/conv`; local sessions only (`name@device` is a
   400 — the transcript lives on the agent's device). Transcripts are
   the most sensitive read on the board: the single-operator-tailnet
   assumption is documented in the README with the precondition
   (identity pinning or port ACL) before this device is ever shared
   into a foreign tailnet.

## Cut, deliberately

- **Remote session view** (`name@device`): would need a fabric op or
  file fetch over links; refused honestly in v1.
- **Markdown rendering** of assistant prose beyond fenced code: the chat
  helper's fence-to-mono is reused; full markdown is skin risk for
  little gain on a phone.
- **cc_peer/codex-backed agents**: their sidecars are com8 plants, not
  Claude sessions — `live:false` is the honest answer today.
- **Streaming/SSE**: same verdict as talk v1 (iOS kills EventSource);
  the 2.5 s poll with an `after` cursor is the proven shape.

## Verification

`scripts/test-com8-transcript.sh` — 25 checks: resolution (collision,
dead pid, probe parity against the daemon's chooser with an accepting
listener), parsing (exact turn sequence, attribution, embedded-reminder
stripping), pathological inputs under time bounds, malformed-record
immunity, longer-replacement detection, explicit window-arithmetic index
sets, concurrency, three distribution lists carrying every board module,
token gate, honest 400s, and the page's `[hidden]` guard / sid tracking /
who bylines. Adversarial review: 0 Critical, 9 Important — all fixed with
failing-first regressions; scoped re-review on the fix diff.
