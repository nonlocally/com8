// This model only consumes an already-authorized dashboard snapshot. It neither
// guesses links from bus membership nor keeps agents that leave that snapshot.
export const NODE_WIDTH = 250;
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const text = (value, fallback = '') => typeof value === 'string' && value ? value : fallback;
const integer = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
export const timestamp = value => Number.isFinite(value) && value > 0 ? value : 0;
export const owner = agent => text(agent.user, 'Unassigned');
export const deviceName = agent => text(agent.device, text(agent.device_metadata?.hostname, 'Unknown device'));
export const deviceKey = agent => JSON.stringify([owner(agent), text(agent.device_id, text(agent.principal, deviceName(agent)))]);
export const agentNodeId = id => `agent:${id}`;

export const MAX_PUBLISHED_AGENTS = 256;
const PUBLISHED_STATUSES = ['live', 'queueable', 'offline'];

function cleanAgent(input) {
  const metadata = {};
  for (const key of ['hostname', 'platform', 'tailscale_hostname', 'tailscale_name', 'tailscale_dns_name']) {
    if (typeof input.device_metadata?.[key] === 'string') metadata[key] = input.device_metadata[key];
  }
  return {
    id: input.id, name: text(input.name, 'Unnamed agent'), kind: text(input.kind, 'agent'),
    user: text(input.user), device: text(input.device), device_id: text(input.device_id, text(input.principal)),
    device_metadata: metadata, status: text(input.status, 'unknown'),
    last_seen: timestamp(input.last_seen), description: text(input.description),
    buses: Array.isArray(input.buses) ? [...new Set(input.buses.filter(v => typeof v === 'string'))] : [],
  };
}

// A service may publish the agents it runs. They are its claim, shown under it
// in its own identity space (`service id/agent id`), never message traffic:
// only id, name, status, role and parent are read, the rest is dropped.
function publishedAgents(service, input) {
  const children = [];
  if (service.kind !== 'service' || !Array.isArray(input)) return children;
  const ids = new Set();
  for (const entry of input) {
    if (!entry || typeof entry !== 'object' || typeof entry.id !== 'string' || !entry.id || ids.has(entry.id)) continue;
    ids.add(entry.id);
    const parent = typeof entry.parent === 'string' && entry.parent ? entry.parent : null;
    children.push({
      id: `${service.id}/${entry.id}`, name: text(entry.name, entry.id), kind: text(entry.role, 'agent'),
      user: service.user, device: service.device, device_id: service.device_id, device_metadata: service.device_metadata,
      status: PUBLISHED_STATUSES.includes(entry.status) ? entry.status : 'queueable', last_seen: service.last_seen, description: '',
      buses: [...service.buses], via: service.id, viaName: service.name, parent: parent && ids.has(parent) ? `${service.id}/${parent}` : parent,
    });
    if (children.length >= MAX_PUBLISHED_AGENTS) break;
  }
  // A parent must be in the same list; anything else hangs from the service.
  for (const child of children) if (child.parent && !ids.has(child.parent.slice(service.id.length + 1))) child.parent = null;
  return children;
}

export function snapshotModel(snapshot = {}) {
  const agents = new Map();
  const containment = [];
  for (const input of Array.isArray(snapshot.agents) ? snapshot.agents : []) {
    if (input && typeof input.id === 'string' && input.id && !agents.has(input.id)) agents.set(input.id, cleanAgent(input));
  }
  for (const input of Array.isArray(snapshot.agents) ? snapshot.agents : []) {
    const service = input && typeof input.id === 'string' ? agents.get(input.id) : null;
    if (!service || service.id !== input.id) continue;
    for (const child of publishedAgents(service, input.agents)) {
      if (agents.has(child.id)) continue;
      agents.set(child.id, child);
      containment.push({source: child.parent || service.id, target: child.id});
    }
  }
  const connections = new Map();
  let observed = false;
  const retention = [];
  for (const bus of Array.isArray(snapshot.buses) ? snapshot.buses : []) {
    if (!bus || typeof bus.name !== 'string' || !Array.isArray(bus.agents)) continue;
    const members = new Set(bus.agents.filter(a => a && typeof a.id === 'string').map(a => a.id));
    if (bus.graph?.basis !== 'retained_bus_messages' || !Array.isArray(bus.graph.edges)) continue;
    observed = true;
    if (Number.isFinite(bus.graph.retention_seconds) && bus.graph.retention_seconds > 0) retention.push(bus.graph.retention_seconds);
    for (const edge of bus.graph.edges) {
      if (!edge || !agents.has(edge.source) || !agents.has(edge.target) || !members.has(edge.source) || !members.has(edge.target)) continue;
      const count = integer(edge.message_count);
      if (!count) continue;
      const key = JSON.stringify([edge.source, edge.target]);
      const connection = connections.get(key) || {id: key, source: edge.source, target: edge.target, message_count: 0, last_sent_at: 0, buses: [], status_counts: {}};
      connection.message_count += count;
      connection.last_sent_at = Math.max(connection.last_sent_at, timestamp(edge.last_sent_at));
      if (!connection.buses.includes(bus.name)) connection.buses.push(bus.name);
      for (const [status, value] of Object.entries(edge.status_counts || {})) {
        if (['accepted', 'leased', 'delivered', 'queued', 'failed', 'expired', 'cancelled'].includes(status)) {
          connection.status_counts[status] = (own(connection.status_counts, status) ? connection.status_counts[status] : 0) + integer(value);
        }
      }
      connections.set(key, connection);
    }
  }
  const groups = new Map();
  for (const agent of agents.values()) {
    const key = deviceKey(agent);
    if (!groups.has(key)) groups.set(key, {key, owner: owner(agent), name: deviceName(agent), agents: []});
    groups.get(key).agents.push(agent);
  }
  const sortedGroups = [...groups.values()].sort((a, b) => a.owner.localeCompare(b.owner) || a.name.localeCompare(b.name) || a.key.localeCompare(b.key));
  // A service's published agents follow it, in the order the service listed them.
  const anchor = agent => agents.get(agent.via) || agent;
  for (const group of sortedGroups) group.agents.sort((a, b) => {
    const [first, second] = [anchor(a), anchor(b)];
    return first.name.localeCompare(second.name) || first.id.localeCompare(second.id) || Number(Boolean(a.via)) - Number(Boolean(b.via)) || (a.via ? 0 : a.id.localeCompare(b.id));
  });
  return {agents: [...agents.values()], groups: sortedGroups, connections: [...connections.values()], containment, observed, retention_seconds: Math.min(...retention, 604800)};
}

// Dashed, unlabelled, arrowless: a service's claim about its agents, never traffic.
function containmentEdges(model, destination, visible, active = new Set()) {
  return (model.containment || []).filter(edge => visible.has(edge.source) && visible.has(edge.target)).map(edge => {
    const source = destination(edge.source), target = destination(edge.target);
    if (source === target) return null;
    const connected = active.has(source) || active.has(target);
    return {id: `contains:${JSON.stringify([edge.source, edge.target])}`, source, target, type: 'bezier', sourceHandle: 'out-bottom', targetHandle: 'in-top',
      animated: false, selectable: false, focusable: false, deletable: false, reconnectable: false,
      style: `stroke:${connected ? '#aaa89a' : '#55554d'};stroke-width:1;stroke-dasharray:4 4;opacity:${active.size && !connected ? .08 : .7}`,
      data: {containment: true}, ariaLabel: `${edge.target} is run by ${edge.source}`};
  }).filter(Boolean);
}

export function layoutNodes(model, previous = []) {
  const positions = new Map(previous.filter(n => n.type === 'agent').map(n => [n.id, n]));
  const deviceColumns = Math.min(2, Math.max(1, model.groups.length));
  const groupColumns = group => Math.min(3, Math.max(1, Math.ceil(Math.sqrt(group.agents.length))));
  const widths = Array.from({length: deviceColumns}, (_, column) => Math.max(...model.groups.filter((_, index) => index % deviceColumns === column).map(group => groupColumns(group) * 350 - 100)));
  const nodes = [];
  let rowY = 0;
  for (let start = 0; start < model.groups.length; start += deviceColumns) {
    const row = model.groups.slice(start, start + deviceColumns);
    const rowHeight = Math.max(...row.map(group => Math.ceil(group.agents.length / groupColumns(group)))) * 170 + 140;
    let groupX = 0;
    for (let column = 0; column < row.length; column++) {
      const group = row[column];
      const columns = groupColumns(group);
      const x = groupX;
      nodes.push({id: `device:${group.key}`, type: 'device', position: {x, y: rowY}, data: {owner: group.owner, name: group.name, count: group.agents.length}, width: columns * 350 - 100, height: 50, draggable: false, selectable: false, connectable: false, focusable: false, deletable: false});
      group.agents.forEach((agent, index) => {
        const id = agentNodeId(agent.id);
        const old = positions.get(id);
        // A renamed device remains the same group, a moved/reassigned device does not.
        const position = old?.data?.group === group.key ? old.position : {x: x + (index % columns) * 350, y: rowY + 75 + Math.floor(index / columns) * 170};
        nodes.push({id, type: 'agent', position: {...position}, width: NODE_WIDTH, height: 120, data: {agent, group: group.key, dimmed: false, neighbor: false}, selected: Boolean(old?.selected), deletable: false, connectable: false, ariaLabel: `${agent.name}, ${agent.kind}, ${owner(agent)}, ${deviceName(agent)}, ${agent.status}`});
      });
      groupX += widths[column] + 130;
    }
    rowY += rowHeight;
  }
  return nodes;
}

// Polling may wait for pointer release only while every displayed identity and
// permission-bearing association remains present. Revocation/removal must never
// sit behind an active drag, even if the pointer stays down indefinitely.
export function canDeferRefresh(current, next) {
  const nextAgents = new Map(next.agents.map(agent => [agent.id, agent]));
  for (const agent of current.agents) {
    const replacement = nextAgents.get(agent.id);
    if (!replacement || deviceKey(replacement) !== deviceKey(agent) || agent.buses.some(bus => !replacement.buses.includes(bus))) return false;
  }
  const nextConnections = new Map(next.connections.map(edge => [edge.id, edge]));
  return current.connections.every(edge => {
    const replacement = nextConnections.get(edge.id);
    return replacement && edge.buses.every(bus => replacement.buses.includes(bus));
  });
}

export function decorate(model, nodes, selectedId) {
  const selected = model.agents.some(agent => agentNodeId(agent.id) === selectedId) ? selectedId : null;
  const neighbors = new Set();
  for (const edge of [...model.connections, ...(model.containment || [])]) {
    if (agentNodeId(edge.source) === selected) neighbors.add(agentNodeId(edge.target));
    if (agentNodeId(edge.target) === selected) neighbors.add(agentNodeId(edge.source));
  }
  const visible = new Set(model.agents.map(agent => agent.id));
  const active = new Set(selected ? [selected] : []);
  return {
    selectedId: selected,
    nodes: nodes.map(node => node.type === 'agent' ? {...node, selected: node.id === selected, data: {...node.data, dimmed: Boolean(selected && node.id !== selected && !neighbors.has(node.id)), neighbor: neighbors.has(node.id)}} : node),
    edges: model.connections.map(edge => {
      const connected = selected && (agentNodeId(edge.source) === selected || agentNodeId(edge.target) === selected);
      const color = connected ? '#c7c6bc' : '#696962';
      return {
        id: `message:${edge.id}`, source: agentNodeId(edge.source), target: agentNodeId(edge.target), type: 'bezier',
        sourceHandle: 'out', targetHandle: 'in', animated: false, selectable: false, focusable: false, deletable: false, reconnectable: false,
        markerEnd: {type: 'arrowclosed', color, width: 14, height: 14},
        label: selected && !connected ? undefined : String(edge.message_count), labelStyle: `fill:${connected ? '#edede7' : '#a1a199'};font-size:10px;font-family:inherit`,
        labelBgStyle: 'fill:#121211;fill-opacity:0.95', labelBgPadding: [6, 3], labelBgBorderRadius: 3,
        style: `stroke:${color};stroke-width:${connected ? 2 : 1};opacity:${selected && !connected ? .08 : connected ? .95 : .35}`,
        ariaLabel: `${edge.message_count} retained messages from ${edge.source} to ${edge.target}`,
      };
    }).concat(containmentEdges(model, agentNodeId, visible, active)),
  };
}

export function agentDetails(model, nodeId) {
  const agent = model.agents.find(value => agentNodeId(value.id) === nodeId);
  if (!agent) return null;
  const incoming = model.connections.filter(edge => edge.target === agent.id);
  const outgoing = model.connections.filter(edge => edge.source === agent.id);
  const peers = new Set([...incoming.map(edge => edge.source), ...outgoing.map(edge => edge.target)]);
  peers.delete(agent.id);
  return {agent, incoming, outgoing, peerCount: peers.size, received: incoming.reduce((sum, edge) => sum + edge.message_count, 0), sent: outgoing.reduce((sum, edge) => sum + edge.message_count, 0)};
}

export const COMMUNITY_COLORS = ['#b3aece', '#91b8b2', '#c0ad83', '#aab889', '#c29fa7', '#8facc8', '#b8a18b', '#a7acb3'];
export const communityNodeId = id => `community:${id}`;
export const communityColor = index => COMMUNITY_COLORS[(Number.isInteger(index) ? index : 0) % COMMUNITY_COLORS.length];

// Layout is deliberately separate from presentation: collapsing a group never
// deletes its members' visual positions or changes the underlying message graph.
export function spectralNodes(model, analysis, previous = []) {
  const old = new Map(previous.filter(node => node.type === 'agent').map(node => [node.id, node]));
  return layoutNodes(model).filter(node => node.type === 'agent').map(node => {
    const prior = old.get(node.id);
    const position = prior?.data?.group === node.data.group ? prior.position : analysis.positions.get(node.data.agent.id) || node.position;
    return {...node, position: {...position}};
  });
}

export function flowNodes(model, flow, previous = []) {
  return spectralNodes(model, {positions: flow?.positions || new Map()}, previous);
}

// A person may choose one visible card to sit above the others. This is a
// drawing preference only: no role inference, routing, or community changes.
export function placeConductor(nodes, conductorId, pinned = new Set(), connections = []) {
  const conductor = nodes.find(node => node.type === 'agent' && node.data.agent.id === conductorId);
  const peers = nodes.filter(node => node.type === 'agent' && node.data.agent.id !== conductorId);
  if (!conductor || !peers.length || pinned.has(conductorId)) return nodes;
  const peerIds = new Set(connections.flatMap(edge => edge.source === conductorId ? [edge.target] : edge.target === conductorId ? [edge.source] : []));
  const linked = peers.filter(node => peerIds.has(node.data.agent.id));
  const centered = linked.length ? linked : peers;
  const left = Math.min(...centered.map(node => node.position.x));
  const right = Math.max(...centered.map(node => node.position.x + (node.width || NODE_WIDTH)));
  const top = Math.min(...peers.map(node => node.position.y));
  const position = {x: (left + right - (conductor.width || NODE_WIDTH)) / 2, y: top - 220 - (conductor.height || 120)};
  return nodes.map(node => node === conductor ? {...node, position} : node);
}

// A collapsed community starts at its members' centroid. In a layered view that
// centroid can land on another branch or a visible root. Preserve its flow-axis
// coordinate, moving only the automatic aggregate by the nearest legal cross-axis
// gap. Existing aggregate positions are deliberate/stable drawing preferences.
function separateFlowAggregates(nodes, aggregatePositions, direction) {
  const axis = direction === 'DOWN' ? 'x' : 'y';
  const cross = axis === 'x' ? 'y' : 'x';
  const size = (node, key) => key === 'x' ? node.width || NODE_WIDTH : node.height || 120;
  const gap = 34;
  const occupied = nodes.filter(node => node.type === 'agent' || node.type === 'community' && aggregatePositions.has(node.id));
  for (const node of nodes) {
    if (node.type !== 'community' || aggregatePositions.has(node.id)) continue;
    const origin = node.position[axis];
    const blocked = occupied.filter(other => node.position[cross] < other.position[cross] + size(other, cross) + gap && node.position[cross] + size(node, cross) + gap > other.position[cross])
      .map(other => [other.position[axis] - size(node, axis) - gap, other.position[axis] + size(other, axis) + gap]);
    const legal = value => blocked.every(([start, end]) => value <= start || value >= end);
    if (!legal(origin)) {
      const candidates = blocked.flat().filter(legal).sort((a, b) => Math.abs(a - origin) - Math.abs(b - origin) || b - a);
      node.position = {...node.position, [axis]: candidates[0]};
    }
    occupied.push(node);
  }
}

export function graphPresentation(model, baseNodes, analysis, options = {}) {
  const {collapsed = new Set(), aggregatePositions = new Map(), pinned = new Set(), showCommunities = true, selectedId = null, focusedCommunity = null, conductorId = null, flowRootId = null, flowDirection = null} = options;
  const visible = new Set(model.agents.map(agent => agent.id));
  const placementRoot = flowRootId || conductorId;
  const visibleConductor = typeof conductorId === 'string' && visible.has(conductorId);
  // Even a caller holding an old analysis cannot render a removed identity.
  const communities = (analysis?.communities || []).filter(group => group.members.length && group.members.every(id => visible.has(id))); // IDs encode membership: discard a stale group whole.
  const byAgent = new Map(communities.flatMap(group => group.members.map(id => [id, group])));
  const collapsedGroups = new Map(communities.filter(group => showCommunities && collapsed.has(group.id) && group.members.some(id => id !== placementRoot)).map(group => [group.id, group]));
  const destination = id => id !== placementRoot && collapsedGroups.has(byAgent.get(id)?.id) ? communityNodeId(byAgent.get(id).id) : agentNodeId(id);
  const aggregates = new Map();
  const existing = new Map(baseNodes.filter(node => node.type === 'agent').map(node => [node.data.agent.id, node]));
  const rendered = baseNodes.filter(node => node.type === 'device' ? model.groups.some(group => node.id === `device:${group.key}`) : node.type === 'agent' && visible.has(node.data.agent.id) && (node.data.agent.id === placementRoot || !collapsedGroups.has(byAgent.get(node.data.agent.id)?.id))).map(node => {
    if (node.type !== 'agent') return node;
    const community = showCommunities ? byAgent.get(node.data.agent.id) : null;
    return {...node, data: {...node.data, community, pinned: pinned.has(node.data.agent.id), conductor: node.data.agent.id === conductorId, flowRoot: node.data.agent.id === flowRootId}};
  });
  for (const group of collapsedGroups.values()) {
    const members = group.members.filter(id => id !== placementRoot).map(id => existing.get(id)).filter(Boolean);
    if (!members.length) continue;
    const id = communityNodeId(group.id);
    const position = aggregatePositions.get(id) || {x: members.reduce((sum, node) => sum + node.position.x, 0) / members.length, y: members.reduce((sum, node) => sum + node.position.y, 0) / members.length};
    const data = {community: {...group, members: members.map(node => node.data.agent.id)}, totalMembers: group.members.length, internalMessages: 0, sent: 0, received: 0, dimmed: false, neighbor: false};
    aggregates.set(id, data);
    rendered.push({id, type: 'community', position: {...position}, width: NODE_WIDTH, height: 120, data, connectable: false, deletable: false, ariaLabel: `${group.label}, ${members.length} agents, collapsed community`});
  }
  if (flowDirection === 'RIGHT' || flowDirection === 'DOWN') separateFlowAggregates(rendered, aggregatePositions, flowDirection);
  const links = new Map();
  for (const edge of model.connections) {
    if (!visible.has(edge.source) || !visible.has(edge.target)) continue;
    const source = destination(edge.source), target = destination(edge.target);
    if (source === target && aggregates.has(source)) { aggregates.get(source).internalMessages += edge.message_count; continue; }
    if (aggregates.has(source)) aggregates.get(source).sent += edge.message_count;
    if (aggregates.has(target)) aggregates.get(target).received += edge.message_count;
    const key = JSON.stringify([source, target]);
    const link = links.get(key) || {id: key, source, target, message_count: 0};
    link.message_count += edge.message_count;
    links.set(key, link);
  }
  const validSelected = rendered.some(node => node.id === selectedId && node.type !== 'device') ? selectedId : null;
  const active = new Set(focusedCommunity ? rendered.filter(node => node.type === 'community' ? node.data.community.id === focusedCommunity : node.type === 'agent' && byAgent.get(node.data.agent.id)?.id === focusedCommunity).map(node => node.id) : validSelected ? [validSelected] : []);
  const contained = containmentEdges(model, destination, visible, active);
  const neighbors = new Set();
  for (const edge of [...links.values(), ...contained]) {
    if (active.has(edge.source)) neighbors.add(edge.target);
    if (active.has(edge.target)) neighbors.add(edge.source);
  }
  return {
    selectedId: validSelected,
    nodes: rendered.map(node => node.type === 'device' ? node : {...node, selected: node.id === validSelected, data: {...node.data, dimmed: Boolean(active.size && !active.has(node.id) && !neighbors.has(node.id)), neighbor: neighbors.has(node.id), focused: active.has(node.id)}}),
    edges: [...links.values()].map(edge => {
      const connected = active.has(edge.source) || active.has(edge.target);
      const color = connected ? '#c7c6bc' : '#696962';
      const reciprocal = edge.source !== edge.target && links.has(JSON.stringify([edge.target, edge.source]));
      // Reverse-direction curves share their label midpoint. Individual CSS
      // translate preserves Flow's transform while giving each count its own row.
      const labelOffset = reciprocal ? `;translate:0 ${edge.source < edge.target ? -12 : 12}px` : '';
      const fromConductor = visibleConductor && edge.source !== edge.target && edge.source === agentNodeId(conductorId);
      const toConductor = visibleConductor && edge.source !== edge.target && edge.target === agentNodeId(conductorId);
      return {id: `message:${edge.id}`, source: edge.source, target: edge.target, type: 'bezier', sourceHandle: fromConductor ? 'out-bottom' : toConductor ? 'out-top' : 'out', targetHandle: toConductor ? 'in-bottom' : fromConductor ? 'in-top' : 'in', animated: false, selectable: false, focusable: false, deletable: false, reconnectable: false,
        markerEnd: {type: 'arrowclosed', color, width: 14, height: 14}, label: active.size && !connected ? undefined : String(edge.message_count), labelStyle: `color:${connected ? '#edede7' : '#a1a199'};font-size:10px;font-family:inherit${labelOffset}`, labelBgStyle: 'fill:#121211;fill-opacity:0.95', labelBgPadding: [6, 3], labelBgBorderRadius: 3,
        style: `stroke:${color};stroke-width:${connected ? 2 : 1};opacity:${active.size && !connected ? .08 : connected ? .95 : .35}`,
        data: {messageCount: edge.message_count}, ariaLabel: `${edge.message_count} retained messages from ${edge.source} to ${edge.target}`};
    }).concat(contained),
  };
}
