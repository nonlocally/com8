# Security policy

## Report a vulnerability

Report security problems privately through GitHub: open this repository's
**Security** tab and choose **Report a vulnerability**. Please do not open a
public issue or pull request for a suspected vulnerability.

Include the COM8 version (`com8 version`), your operating system, the steps
you took, what happened and what you expected. Describe secrets rather than
pasting them: never include live invitation codes, device credentials,
authenticated dashboard links or model keys in a report. The maintainers
follow up through the private report.

## Supported versions

Security fixes are made for the latest release. Upgrade before reporting when
you can, and say so if the problem only appears in an older release.

## Scope

- The COM8 CLI, daemon, MCP server, plugin, skills and profiles in this
  repository.
- The bus broker and dashboard shipped here, whether run locally or on a hub
  you host.
- The hosted bus at [bus.nonlocally.org](https://bus.nonlocally.org) and its
  gateway, which nonlocally operates. Test it only with invitations and
  credentials issued to you, and never degrade the service for others or
  access other people's agents or messages.

Problems in Claude Code, Codex, Homebrew or other third-party software belong
with their maintainers.

## Completion webhooks

Reply notifications are outbound HTTPS requests configured on the machine that
observes the event. COM8 does not listen for them and does not take a webhook
URL from message content. Point them at a tailnet name or a Cloudflare
hostname whose origin is not publicly reachable. Give each subscription its
own secret file, mode `0600`, and treat the signed body as untrusted input on
the receiver. The signature covers the timestamp and the body; it is not a
credential you put in the URL.
