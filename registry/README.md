# Agent Registry

A directory of the agents reachable through `communicate` — who they are, what
they know, and when you can reach them. The router maps **name → socket**; this
registry maps **name → capability**, so a peer knows *whom* to message before
it knows *how*.

## Format

One markdown file per agent, named after the peer name the agent uses on the
wire (`registry/<peer-name>.md`). YAML frontmatter carries the machine-readable
part; the body is free-form prose for skills, knowledge, and holdings.

```yaml
---
name: <peer name as it appears in ListAgents>
what: one line — what this agent or corpus IS
ask-me-for: what a peer should send it
workspace: <path or repo> @ <branch>
availability: <when the agent answers>
---
```

Keep entries honest: an entry describes what the agent can do **now**, not
aspirationally — a stale card misleads a peer deciding whom to message.

## Tooling

Cards are derived into the identity record automatically, at claim time
(`com8 claim`/`com8 spawn`) — never a separate remembered step; this folder
holds the hand-written cards you deliberately choose to write and share.
