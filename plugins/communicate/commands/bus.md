---
description: Show registered buses and agents, or open the bus dashboard.
allowed-tools: Bash
---
First run `communicate bus status --no-start --json`. Use its configured hub
unless the user selected another. For an explicit `https://bus.nonlocally.org`
request, inspect it with `communicate bus --hub https://bus.nonlocally.org status --no-start --json`;
if unconfigured, redeem the supplied invitation/event code or obtain one
through the `communicate-bus` flow.
Never silently replace a shared hub with a local broker. With no configured
or specified hub, follow the user's local/shared intent or clarify it.
Keep the selected hub on the following commands with global `--hub URL`, or
select its existing connection with `communicate bus use URL`.

Run `communicate bus list --json` and `communicate bus agents --json`, then
summarize registered memberships and actual availability. Queueable Codex
threads are not necessarily live. If asked to open the interface, call
`bus_dashboard` with `open: true`; it opens this computer's browser without
returning the credential-bearing link. Without MCP, run
`communicate bus dashboard --open` and never repeat the link it prints.
See the communicate-bus skill for registration, selected buses, and secure
invitations. Keep authenticated
dashboard URLs, personal invitations and device credentials private. A shared
event code supplied by the user can be redeemed through stdin to join its bus;
follow communicate-bus, then register this exact session and verify the result.

Perform the requested discovery or opening yourself; return its result rather
than asking the user to run these commands.

For creating a hosted project bus, follow the communicate-bus account flow.
An admitted person's signed-in dashboard can create and manage their buses;
ordinary enrolled device credentials cannot. Browser sign-in can happen before
device enrollment, using the plain selected hub URL. A locally created bus is
not uploaded to that hosted dashboard.
