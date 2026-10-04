---
name: example-agent
what: one line — what this agent or corpus IS
ask-me-for: what a peer should send it
workspace: ~/src/project @ main
availability: on demand
---

# example-agent

This directory is the *shareable* form of a capability card. Cards are
generated into the identity record by `com8 claim`/`com8 spawn` and live in
the daemon's state; this folder is for cards you deliberately choose to write
by hand and share.

Entries here are gitignored by default: a card describes a real machine, a
real corpus, and sometimes another person's infrastructure. Ship the schema,
never the entries.
