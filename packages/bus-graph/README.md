# Bus graph

A locally bundled Svelte Flow view of the existing authorized bus snapshot. The
host dashboard owns authentication, fetching, filters, and permissions; this
widget makes no requests and stores nothing in cookies or browser storage.

The default **Flow** layout organizes the visible communication network into
branches and successive levels, with left-to-right or top-to-bottom direction.
A suggested root and local hubs come from connectivity; viewers can choose a
root and correct a connected agent's layout parent. These are visual choices,
not assigned roles or task dependencies. **Spectral** uses normalized-Laplacian
coordinates with relationship-aware spacing, and **Devices** retains the
owner/device grid. Leiden communities add neutral group labels and colors.
Nodes can be moved freely, and the viewport supports pan, zoom, fit, and a minimap.
The standalone graph page fills its host
without a surrounding box. The embedded view retains Expand; Escape collapses
it and returns focus to its toolbar button. Drag the background to pan. Scroll
with the mouse wheel or two fingers, or pinch, to zoom around the pointer.
Selecting an agent opens an overlay without resizing or moving the canvas. It shows its identity,
device, current status, and incoming/outgoing counts, while highlighting its
neighbors. These are visual operations only; they do not issue agent commands,
change bus membership, or create message edges.

**Communities** supports group focus, member inspection, and collapse/expand.
Collapsed edges preserve directed external message totals; each group shows its
internal count. **Analysis** exposes the weighting, eigenvalues, component
members, and weighted modularity. New traffic updates the displayed arrows;
**Recompute** explicitly updates the analysis and arrangement. Pins retain an
agent's chosen position during recomputation. All state remains in page memory.

In Spectral or Devices, select an agent and choose **Set apart as conductor** to give it a separate
position above the network. The viewer assigns this visual role explicitly;
traffic does not imply authority. The agent remains visible when its community
is collapsed, with all directed message counts preserved. Clear the choice to
restore ordinary placement. Recompute reapplies it unless that agent is pinned.

## Mathematical model

All calculations consume only the sanitized, visible graph. Undirected weights
are `log1p(count(i→j) + count(j→i))`; directions and original counts remain in
the display. Self-messages do not influence analysis. See
[Graph analysis](../../docs/GRAPH-ANALYSIS.md) for equations and interpretation.

- Flow uses root removal to identify branches, internal weighted degree to
  suggest branch hubs, and breadth-first levels within branches. Parent
  corrections must use real visible relationships and preserve an acyclic
  forest. ELK Layered positions that forest; the full directed traffic is drawn
  over it without changing any count or arrow. The automatic root prioritizes
  distinct peer count, then weighted degree, then canonical ID. Names never
  supply role information. Each disconnected component has its own root.
- Spectral coordinates use the two lowest nonzero eigenmodes of each connected
  component's symmetric normalized Laplacian. Pairs have one mode; isolates
  have none. Canonical ID order, eigenspace projection, and sign orientation
  make identical inputs reproducible, including repeated eigenvalues.
- Card spacing refines the spectral starting positions using weighted
  graph-distance stress, a spectral anchor, and rectangle separation. Strong
  connections prefer shorter distances, and densely connected neighborhoods
  influence the spread. The bounded refinement does not change reported
  eigenvectors or eigenvalues. Disconnected components are packed independently.
- Leiden optimizes weighted modularity at resolution one. Three seeded starts
  (`17`, `29`, `43`), each with ten iterations, select the highest objective;
  canonical membership order resolves ties. The implementation is
  `networkanalysis-ts@1.0.0`, the Leiden authors' TypeScript port.
- Dense analysis is limited to **256 visible agents**. Larger views retain every
  node in a grid with connected-component groups and an explicit notice. Filter
  the view to enable spectral/Leiden analysis. Numerical failures similarly
  report their fallback instead of silently claiming successful analysis.

The layered engine is `elkjs@0.11.0`, bundled locally with its EPL-2.0 notices and
source provenance. The eigensolver is `ml-matrix@6.15.0`; the seeded RNG is `java-random@0.4.0`.
The latter declares ISC but omits a standalone license file. A version-specific
notice in `licenses/` records that provenance and the standard license text;
the runtime license generator still fails on any other missing notice.

## Build

From this directory:

```sh
npm ci --ignore-scripts
npm test
npm run build
```

Node 22.12+ (or Node 20.19+) is required by Vite. The exact runtime and build
versions are locked in `package-lock.json`. Build emits checked-in
`lib/assets/bus-graph.js` (IIFE, includes Svelte and Svelte Flow) and
`lib/assets/bus-graph.css` from source. The build also emits
`lib/assets/bus-graph.LICENSES.txt` with license notices for every bundled runtime
dependency; it must be distributed alongside the assets. There are no CDN imports, fonts, or separate
runtime chunks. The dashboard does not require Node to run.

Upstream: [Svelte Flow](https://svelteflow.dev/learn),
[@xyflow/svelte](https://github.com/xyflow/xyflow), licensed MIT. The standard
Svelte Flow attribution is retained.

## Host API

Load the stylesheet and script from the broker's authenticated static routes.
Mount into an empty element inside the dashboard's graph view:

```js
const graph = window.CommunicateGraph.mount(element);
graph.update({scope: `${serverId}:${selectedBus}`, agents, buses, standalone: true});
graph.clear();
graph.destroy();
```

- `agents` is the currently visible, filtered roster. Each agent has `id`, `name`,
  `kind`, `user`, `device`, `device_id`, `device_metadata`, `status`, `last_seen`,
  `description`, and permitted `buses` names.
- An agent of kind `service` may carry `agents`, the list it published: entries
  with `id`, `name`, `status`, `role` and `parent`. They are drawn as nodes
  under the service (`service id/agent id`, the service's device and buses,
  a "via" line instead of a device) joined by dashed, unlabelled lines, in the
  service's listed order, at most 256; a child's `parent` resolves against the
  whole list whatever the order. They take part in no message link, analysis
  weight, community or flow inference, and in the spectral and flow layouts
  they stack under their service; an offline service shows them offline.
  Anything else in an entry is dropped.
- `buses` contains only selected/permitted snapshot buses. Each has `name`,
  `agents`, and optional `graph: {basis: "retained_bus_messages", edges,
  retention_seconds}`. Each directed edge has `source`, `target`,
  `message_count`, `last_sent_at`, and `status_counts`.
- An edge is shown only when both endpoint agents are currently visible and
  occur in that edge's source bus roster. Counts aggregate across selected buses
  for the same directed pair. Membership does not imply traffic.
- Broker receipt retention is based on terminal receipt updates, not a strict
  “messages sent in the last seven days” window. Labels say *retained messages*.
  Nonterminal messages may be older. Message contents are never needed.
- Stable `scope` preserves node positions, selection, and viewport across data
  refresh. Use a different scope for another broker/bus selection. Removed
  agents lose positions and inspector state immediately; no out-of-view cache
  is kept. Device reassignment also resets that agent's position.
- `standalone: true` fills the mount element's explicitly sized height, removes
  the border and Expand control, and uses compact controls. Omit it for the
  embedded dashboard view. The inspector overlays the right edge, or the bottom
  on narrow screens, without changing the canvas dimensions.
- During a node drag, routine refreshes wait for pointer release and only the
  latest sanitized snapshot is kept. Any scope change, removed agent or message
  link, narrower bus membership, device reassignment, or `clear()` applies
  immediately and cancels the drag; a pending snapshot cannot restore it.
- A changed `scope` resets the layout, selection, and viewport. Call `clear()`
  whenever authorization is lost or the session is being replaced; it removes
  nodes, edges, details, and in-memory visual state synchronously.
- `destroy()` is idempotent and removes the component; later update calls are
  ignored. Recreate it with `mount()` when needed.

The pure-model tests cover known spectra, eigenvector residuals, degenerate
eigenspaces, weighted modularity, connected communities, card collisions,
fixed-coordinate relationship changes, deterministic stress refinement,
authorization boundaries, directed aggregation, and state pruning.
`scripts/test-bus-graph-browser.py` covers navigation, dragging through refresh,
zoom, mobile layout, and revocation. `scripts/test-bus-spectral-browser.py` checks
analysis controls, collapsed traffic accounting, stale analysis, pinned positions,
and removal from hidden community/analysis state against a temporary real broker.
`scripts/test-bus-conductor-browser.py` checks explicit conductor placement,
vertical message paths, collapse accounting, pin precedence, restoration, and
clearing scope or identity changes with all network requests blocked.
`scripts/test-bus-flow-browser.py` checks default branches and ranks, orientation,
root/parent correction, full traffic accounting, pins, stable polling, and stale
asynchronous result rejection after permission, scope, clear, and layout changes.
