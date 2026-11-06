import test from 'node:test';
import assert from 'node:assert/strict';
import {snapshotModel, layoutNodes, canDeferRefresh, decorate, agentDetails, agentNodeId, spectralNodes, flowNodes, placeConductor, graphPresentation, communityNodeId, deviceKey} from './model.mjs';

const agent = (id, extra = {}) => ({id, name: id, user: 'aadarwal', kind: 'codex', device: 'mini', device_id: 'device-one', status: 'queueable', buses: ['qit-wilde'], ...extra});
const message = (source, target, extra = {}) => ({source, target, message_count: 1, last_sent_at: 100, status_counts: {delivered: 1}, ...extra});
const snapshot = (agents, edges, extra = {}) => ({agents, buses: [{name: 'qit-wilde', agents, graph: {basis: 'retained_bus_messages', retention_seconds: 604800, edges}}], ...extra});

test('bus membership never fabricates message links', () => {
  const result = snapshotModel(snapshot([agent('a'), agent('b')], []));
  assert.equal(result.agents.length, 2);
  assert.deepEqual(result.connections, []);
  assert.equal(result.observed, true);
  assert.equal(snapshotModel({agents: result.agents, buses: [{name: 'qit-wilde', agents: result.agents}]}).observed, false);
});

test('edges require both visible agents and membership on their source bus', () => {
  const a = agent('a'), b = agent('b'), privateAgent = agent('private');
  const result = snapshotModel({agents: [a, b], buses: [
    {name: 'qit-wilde', agents: [a, b], graph: {basis: 'retained_bus_messages', edges: [message('a', 'b'), message('a', 'private')]}},
    {name: 'other-bus', agents: [privateAgent], graph: {basis: 'retained_bus_messages', edges: [message('b', 'a'), message('private', 'a')]}},
  ]});
  assert.deepEqual(result.connections.map(edge => [edge.source, edge.target]), [['a', 'b']]);
});

test('parallel buses aggregate retained directed counts and actual receipt states', () => {
  const agents = [agent('a'), agent('b')];
  const base = snapshot(agents, [message('a', 'b', {message_count: 3, status_counts: {accepted: 1, leased: 1, cancelled: 1, unused: 100}}), message('b', 'a')]);
  base.buses.push({name: 'general', agents, graph: {basis: 'retained_bus_messages', edges: [message('a', 'b', {message_count: 2, last_sent_at: 200, status_counts: {queued: 2}})]}});
  const result = snapshotModel(base);
  assert.equal(result.connections.length, 2);
  assert.equal(result.connections[0].message_count, 5);
  assert.equal(result.connections[0].last_sent_at, 200);
  assert.deepEqual(result.connections[0].status_counts, {accepted: 1, leased: 1, cancelled: 1, queued: 2});
  assert.deepEqual(result.connections[0].buses, ['qit-wilde', 'general']);
});

test('21 agents on one device get a balanced three-column grid', () => {
  const model = snapshotModel(snapshot(Array.from({length: 21}, (_, index) => agent(`a${index}`)), []));
  const nodes = layoutNodes(model).filter(node => node.type === 'agent');
  assert.equal(new Set(nodes.map(node => node.position.x)).size, 3);
  assert.equal(new Set(nodes.map(node => node.position.y)).size, 7);
  assert.equal(new Set(nodes.map(node => JSON.stringify(node.position))).size, 21);
  assert.ok(Math.max(...nodes.map(node => node.position.y)) < 1200);
});

test('refresh retains moved nodes; removed or reassigned nodes lose old positions', () => {
  const model = snapshotModel(snapshot([agent('a'), agent('b')], [message('a', 'b')]));
  const nodes = layoutNodes(model);
  nodes.find(node => node.id === agentNodeId('a')).position = {x: 1234, y: 5678};
  const updated = snapshotModel(snapshot([agent('a', {status: 'offline', device: 'renamed'}), agent('c')], []));
  const next = layoutNodes(updated, nodes);
  assert.deepEqual(next.find(node => node.id === agentNodeId('a')).position, {x: 1234, y: 5678});
  assert.ok(!next.some(node => node.id === agentNodeId('b')));
  const reassigned = snapshotModel(snapshot([agent('a', {device_id: 'different-device'})], []));
  assert.notDeepEqual(layoutNodes(reassigned, next).find(node => node.id === agentNodeId('a')).position, {x: 1234, y: 5678});
});

test('selection highlights only actual neighbors and prunes departed details', () => {
  const model = snapshotModel(snapshot([agent('a'), agent('b'), agent('c'), agent('d')], [message('a', 'b'), message('c', 'a'), message('c', 'd')]));
  const selected = decorate(model, layoutNodes(model), agentNodeId('a'));
  assert.equal(selected.nodes.find(node => node.id === agentNodeId('b')).data.neighbor, true);
  assert.equal(selected.nodes.find(node => node.id === agentNodeId('c')).data.neighbor, true);
  assert.equal(selected.nodes.find(node => node.id === agentNodeId('d')).data.dimmed, true);
  assert.equal(selected.edges.find(edge => edge.source === agentNodeId('c') && edge.target === agentNodeId('d')).label, undefined);
  const details = agentDetails(model, selected.selectedId);
  assert.equal(details.sent, 1); assert.equal(details.received, 1); assert.equal(details.peerCount, 2);
  const reduced = snapshotModel(snapshot([agent('d')], []));
  assert.equal(decorate(reduced, layoutNodes(reduced), selected.selectedId).selectedId, null);
  assert.equal(agentDetails(reduced, selected.selectedId), null);
});

test('all-data clear has no hidden node, link, or inspector record', () => {
  const model = snapshotModel();
  const decorated = decorate(model, layoutNodes(model), agentNodeId('private'));
  assert.deepEqual(model.agents, []); assert.deepEqual(model.groups, []);
  assert.deepEqual(model.connections, []); assert.deepEqual(decorated.nodes, []);
  assert.deepEqual(decorated.edges, []); assert.equal(decorated.selectedId, null);
});

test('only display fields enter the model; message bodies and credentials are ignored', () => {
  const input = agent('a', {token: 'private-token', device_metadata: {hostname: 'mini', token: 'private-meta'}});
  const result = snapshotModel(snapshot([input, agent('b')], [message('a', 'b', {body: 'private-message', token: 'private-edge'})]));
  const serialized = JSON.stringify(result);
  assert.ok(!serialized.includes('private-'));
  assert.equal(result.agents[0].device_metadata.hostname, 'mini');
});

test('agent-controlled ids cannot collide with structural device nodes', () => {
  const result = snapshotModel(snapshot([agent('device:one'), agent('__proto__'), agent('agent:one')], [message('__proto__', 'device:one')]));
  const nodes = layoutNodes(result);
  assert.equal(new Set(nodes.map(node => node.id)).size, nodes.length);
  assert.equal(result.connections.length, 1);
});

test('drag refresh may defer added agents and updated traffic but never removals or revoked bus scope', () => {
  const agents = [agent('a'), agent('b')];
  const current = snapshotModel(snapshot(agents, [message('a', 'b')]));
  assert.equal(canDeferRefresh(current, snapshotModel(snapshot([
    agent('a', {status: 'offline'}), agent('b'), agent('c'),
  ], [message('a', 'b', {message_count: 2})]))), true);
  assert.equal(canDeferRefresh(current, snapshotModel(snapshot([agent('a')], []))), false);
  assert.equal(canDeferRefresh(current, snapshotModel(snapshot(agents, []))), false);
  assert.equal(canDeferRefresh(current, snapshotModel(snapshot([
    agent('a', {device_id: 'different-device'}), agent('b'),
  ], [message('a', 'b')]))), false);
  assert.equal(canDeferRefresh(current, snapshotModel(snapshot([
    agent('a', {buses: []}), agent('b'),
  ], [message('a', 'b')]))), false);
});

test('drag refresh cannot retain a removed source bus even when the same agents and link remain elsewhere', () => {
  const agents = [agent('a'), agent('b')];
  const shared = snapshot(agents, [message('a', 'b')]);
  shared.buses.push({name: 'general', agents, graph: {basis: 'retained_bus_messages', edges: [message('a', 'b')]}});
  const current = snapshotModel(shared);
  const narrowed = snapshotModel({...shared, buses: shared.buses.slice(1)});
  assert.equal(current.connections.length, narrowed.connections.length);
  assert.equal(canDeferRefresh(current, narrowed), false);
});

const analysisFixture = (groups) => ({
  positions: new Map(groups.flat().map((id, index) => [id, {x: index * 300, y: index % 2 * 160}])),
  communities: groups.map((members, index) => ({id: `g${index}`, label: `Group ${index + 1}`, members, colorIndex: index})),
});

test('spectral positions use analysis coordinates, preserving manual positions only for the same device identity', () => {
  const model = snapshotModel(snapshot([agent('a'), agent('b')], [message('a', 'b')]));
  const analysis = analysisFixture([['a', 'b']]);
  const nodes = spectralNodes(model, analysis);
  assert.deepEqual(nodes.map(node => node.position), [...analysis.positions.values()]);
  assert.ok(nodes.every(node => node.type === 'agent'));
  nodes[0].position = {x: 3333, y: 4444};
  assert.deepEqual(spectralNodes(model, analysis, nodes)[0].position, nodes[0].position);
  const reassigned = snapshotModel(snapshot([agent('a', {device_id: 'changed'})], []));
  assert.deepEqual(spectralNodes(reassigned, analysis, nodes)[0].position, analysis.positions.get('a'));
});

test('collapsed communities aggregate each directed message once and count internal messages separately', () => {
  const model = snapshotModel(snapshot([agent('a'), agent('b'), agent('c'), agent('d')], [
    message('a', 'b', {message_count: 3}), message('b', 'a', {message_count: 4}),
    message('a', 'c', {message_count: 5}), message('b', 'd', {message_count: 6}),
    message('d', 'b', {message_count: 7}), message('c', 'c', {message_count: 8}),
  ]));
  const analysis = analysisFixture([['a', 'b'], ['c', 'd']]);
  const result = graphPresentation(model, spectralNodes(model, analysis), analysis, {collapsed: new Set(['g0', 'g1'])});
  assert.equal(result.nodes.length, 2);
  assert.equal(result.nodes.find(node => node.id === communityNodeId('g0')).data.internalMessages, 7);
  assert.equal(result.nodes.find(node => node.id === communityNodeId('g1')).data.internalMessages, 8);
  assert.deepEqual(result.edges.map(edge => [edge.source, edge.target, edge.data.messageCount]), [
    [communityNodeId('g0'), communityNodeId('g1'), 11], [communityNodeId('g1'), communityNodeId('g0'), 7],
  ]);
  assert.equal(result.nodes.reduce((sum, node) => sum + node.data.internalMessages, 0) + result.edges.reduce((sum, edge) => sum + edge.data.messageCount, 0), model.connections.reduce((sum, edge) => sum + edge.message_count, 0));
});

test('expanded community layer preserves member positions and directed detail; disabling the layer expands groups', () => {
  const model = snapshotModel(snapshot([agent('a'), agent('b'), agent('c')], [message('a', 'b'), message('b', 'c')]));
  const analysis = analysisFixture([['a', 'b'], ['c']]);
  const nodes = spectralNodes(model, analysis);
  nodes[1].position = {x: 1000, y: 2000};
  const options = {collapsed: new Set(['g0']), showCommunities: false, pinned: new Set(['b']), selectedId: agentNodeId('a')};
  const result = graphPresentation(model, nodes, analysis, options);
  assert.equal(result.nodes.length, 3);
  assert.equal(result.edges.length, 2);
  assert.deepEqual(result.nodes.find(node => node.id === agentNodeId('b')).position, {x: 1000, y: 2000});
  assert.equal(result.nodes.find(node => node.id === agentNodeId('b')).data.pinned, true);
  assert.ok(result.nodes.every(node => !node.data.community));
  assert.equal(result.nodes.find(node => node.id === agentNodeId('c')).data.dimmed, true);
});

test('presentation removes revoked identities and aggregate totals even if caller supplied old analysis and positions', () => {
  const all = snapshotModel(snapshot([agent('a'), agent('private'), agent('c')], [message('a', 'private', {message_count: 20}), message('a', 'c', {message_count: 2})]));
  const analysis = analysisFixture([['a', 'private'], ['c']]);
  analysis.communities[0].id = 'community:["a","private"]';
  const now = snapshotModel(snapshot([agent('a'), agent('c')], [message('a', 'c', {message_count: 2})]));
  const view = graphPresentation(now, spectralNodes(all, analysis), analysis, {collapsed: new Set(['community:["a","private"]']), selectedId: agentNodeId('private')});
  assert.equal(view.selectedId, null);
  assert.equal(view.nodes.filter(node => node.type === 'community').length, 0);
  assert.deepEqual(view.nodes.map(node => node.data.agent.id), ['a', 'c']);
  assert.equal(view.edges[0].data.messageCount, 2);
  assert.ok(!JSON.stringify(view).includes('private'));
  const clear = graphPresentation(snapshotModel(), spectralNodes(all, analysis), analysis, {collapsed: new Set(['g0', 'g1'])});
  assert.deepEqual(clear.nodes, []);
  assert.deepEqual(clear.edges, []);
});

test('community focus highlights its actual neighbors without inventing extra links', () => {
  const model = snapshotModel(snapshot(['a', 'b', 'c', 'd'].map(id => agent(id)), [message('a', 'b'), message('b', 'c')]));
  const analysis = analysisFixture([['a', 'b'], ['c'], ['d']]);
  const view = graphPresentation(model, spectralNodes(model, analysis), analysis, {focusedCommunity: 'g0'});
  assert.equal(view.edges.length, 2);
  assert.equal(view.nodes.find(node => node.id === agentNodeId('a')).data.focused, true);
  assert.equal(view.nodes.find(node => node.id === agentNodeId('c')).data.neighbor, true);
  assert.equal(view.nodes.find(node => node.id === agentNodeId('d')).data.dimmed, true);
});

test('reciprocal message labels occupy opposite rows without changing counts or offsetting one-way links', () => {
  const model = snapshotModel(snapshot(['a', 'b', 'c'].map(id => agent(id)), [
    message('a', 'b', {message_count: 59}), message('b', 'a', {message_count: 50}), message('a', 'c', {message_count: 3}),
  ]));
  const analysis = analysisFixture([['a'], ['b'], ['c']]);
  const edges = graphPresentation(model, spectralNodes(model, analysis), analysis).edges;
  const outgoing = edges.find(edge => edge.source === agentNodeId('a') && edge.target === agentNodeId('b'));
  const incoming = edges.find(edge => edge.source === agentNodeId('b') && edge.target === agentNodeId('a'));
  const oneWay = edges.find(edge => edge.target === agentNodeId('c'));
  assert.equal(outgoing.label, '59');
  assert.equal(incoming.label, '50');
  assert.match(outgoing.labelStyle, /translate:0 -12px/);
  assert.match(incoming.labelStyle, /translate:0 12px/);
  assert.doesNotMatch(oneWay.labelStyle, /translate/);
  assert.match(outgoing.labelStyle, /^color:/);
  assert.deepEqual(edges.map(edge => edge.data.messageCount), [59, 50, 3]);
});

test('explicit conductor sits above all cards centered on actual peers while the default layout stays unchanged', () => {
  const model = snapshotModel(snapshot(['a', 'b', 'c', 'unrelated'].map(id => agent(id)), [message('a', 'b'), message('c', 'a')]));
  const analysis = analysisFixture([['a', 'b', 'c', 'unrelated']]);
  const nodes = spectralNodes(model, analysis);
  nodes.find(node => node.data.agent.id === 'unrelated').position = {x: 9000, y: -50};
  const original = structuredClone(nodes);
  assert.equal(placeConductor(nodes, null), nodes);
  const chosen = placeConductor(nodes, 'a', new Set(), model.connections);
  const leader = chosen.find(node => node.data.agent.id === 'a');
  assert.equal(leader.position.x, 450); // peers b and c span x=300..850
  assert.equal(leader.position.y + 120, -50 - 220); // above even the unrelated card
  assert.deepEqual(nodes, original);
  assert.deepEqual(chosen.filter(node => node.data.agent.id !== 'a'), original.filter(node => node.data.agent.id !== 'a'));
  assert.equal(placeConductor(nodes, 'a', new Set(['a']), model.connections), nodes);
  assert.equal(placeConductor(nodes, 'missing'), nodes);
  const solo = nodes.slice(0, 1);
  assert.equal(placeConductor(solo, 'a'), solo);
});

test('conductor remains separate from a collapsed community without changing math membership or double-counting traffic', () => {
  const model = snapshotModel(snapshot(['a', 'b', 'c', 'd'].map(id => agent(id)), [
    message('a', 'b', {message_count: 7}), message('c', 'a', {message_count: 5}),
    message('b', 'c', {message_count: 3}), message('a', 'd', {message_count: 2}),
    message('d', 'b', {message_count: 11}), message('a', 'a', {message_count: 13}),
  ]));
  const analysis = analysisFixture([['a', 'b', 'c'], ['d']]);
  const originalCommunities = structuredClone(analysis.communities);
  const nodes = spectralNodes(model, analysis);
  const view = graphPresentation(model, nodes, analysis, {conductorId: 'a', collapsed: new Set(['g0', 'g1'])});
  const conductor = view.nodes.find(node => node.id === agentNodeId('a'));
  assert.equal(conductor.data.conductor, true);
  assert.equal(conductor.data.community.members.length, 3);
  const group = view.nodes.find(node => node.id === communityNodeId('g0'));
  assert.deepEqual(group.data.community.members, ['b', 'c']);
  assert.equal(group.data.totalMembers, 3);
  assert.equal(group.data.internalMessages, 3);
  assert.deepEqual(analysis.communities, originalCommunities);
  const outward = view.edges.find(edge => edge.source === conductor.id && edge.target === group.id);
  const inward = view.edges.find(edge => edge.source === group.id && edge.target === conductor.id);
  assert.equal(outward.data.messageCount, 7);
  assert.equal(inward.data.messageCount, 5);
  assert.equal(outward.sourceHandle, 'out-bottom');
  assert.equal(outward.targetHandle, 'in-top');
  assert.equal(inward.sourceHandle, 'out-top');
  assert.equal(inward.targetHandle, 'in-bottom');
  const total = view.edges.reduce((sum, edge) => sum + edge.data.messageCount, 0) + view.nodes.reduce((sum, node) => sum + (node.data.internalMessages || 0), 0);
  assert.equal(total, 41);
  const reset = graphPresentation(model, nodes, analysis, {collapsed: new Set(['g0', 'g1'])});
  assert.ok(!reset.nodes.some(node => node.data.conductor));
  assert.equal(reset.nodes.find(node => node.id === communityNodeId('g0')).data.internalMessages, 28);
});

test('choosing a singleton conductor never creates an empty aggregate or resurrects a removed choice', () => {
  const model = snapshotModel(snapshot([agent('a'), agent('b')], [message('a', 'b')]));
  const analysis = analysisFixture([['a'], ['b']]);
  const nodes = spectralNodes(model, analysis);
  const view = graphPresentation(model, nodes, analysis, {conductorId: 'a', collapsed: new Set(['g0', 'g1'])});
  assert.equal(view.nodes.length, 2);
  assert.ok(!view.nodes.some(node => node.id === communityNodeId('g0')));
  assert.ok(view.edges.every(edge => view.nodes.some(node => node.id === edge.source) && view.nodes.some(node => node.id === edge.target)));
  const removed = graphPresentation(snapshotModel(snapshot([agent('b')], [])), nodes, analysis, {conductorId: 'a'});
  assert.ok(!removed.nodes.some(node => node.data.conductor));
  assert.deepEqual(removed.edges, []);
});

test('an agent literally named null does not receive conductor handles without an explicit choice', () => {
  const model = snapshotModel(snapshot([agent('null'), agent('b')], [message('null', 'b'), message('b', 'null')]));
  const analysis = analysisFixture([['null', 'b']]);
  const nodes = spectralNodes(model, analysis);
  for (const conductorId of [null, 'missing']) {
    const view = graphPresentation(model, nodes, analysis, {conductorId});
    assert.ok(view.nodes.every(node => !node.data.conductor));
    assert.ok(view.edges.every(edge => edge.sourceHandle === 'out' && edge.targetHandle === 'in'));
  }
});


test('flow coordinates preserve moved cards and pins only for the same device identity', () => {
  const original = snapshotModel(snapshot([agent('a'), agent('b')], [message('a', 'b')]));
  const flow = {positions: new Map([['a', {x: 0, y: 50}], ['b', {x: 400, y: 50}]])};
  const nodes = flowNodes(original, flow);
  assert.deepEqual(nodes.map(node => node.position), [{x: 0, y: 50}, {x: 400, y: 50}]);
  nodes[0].position = {x: 111, y: 222};
  assert.deepEqual(flowNodes(original, flow, nodes)[0].position, {x: 111, y: 222});
  const reassigned = snapshotModel(snapshot([agent('a', {device_id: 'different'}), agent('b')], [message('a', 'b')]));
  assert.deepEqual(flowNodes(reassigned, flow, nodes).find(node => node.data.agent.id === 'a').position, {x: 0, y: 50});
  const removed = snapshotModel(snapshot([agent('b')], []));
  assert.deepEqual(flowNodes(removed, flow, nodes).map(node => node.data.agent.id), ['b']);
});

test('suggested flow root survives collapse without claiming conductor authority or losing directions', () => {
  const model = snapshotModel(snapshot(['root', 'a', 'b'].map(id => agent(id)), [message('root', 'a', {message_count: 7}), message('a', 'root', {message_count: 3}), message('a', 'b', {message_count: 2})]));
  const analysis = analysisFixture([['root', 'a', 'b']]);
  const nodes = flowNodes(model, analysis);
  const view = graphPresentation(model, nodes, analysis, {flowRootId: 'root', collapsed: new Set(['g0'])});
  const root = view.nodes.find(node => node.id === agentNodeId('root'));
  assert.equal(root.data.flowRoot, true);
  assert.equal(root.data.conductor, false);
  const group = view.nodes.find(node => node.type === 'community');
  assert.deepEqual(group.data.community.members, ['a', 'b']);
  assert.equal(group.data.internalMessages, 2);
  assert.deepEqual(view.edges.map(edge => edge.data.messageCount), [7, 3]);
  assert.ok(view.edges.every(edge => edge.sourceHandle === 'out' && edge.targetHandle === 'in'));
  const removed = graphPresentation(snapshotModel(snapshot([agent('a'), agent('b')], [message('a', 'b')])), nodes, analysis, {flowRootId: 'root'});
  assert.ok(removed.nodes.every(node => !node.data.flowRoot));
});

test('changing the flow root across collapsed groups keeps all traffic and only the current root outside', () => {
  const model = snapshotModel(snapshot(['a', 'b', 'c', 'd'].map(id => agent(id)), [
    message('a', 'b', {message_count: 5}), message('b', 'a', {message_count: 3}),
    message('b', 'c', {message_count: 7}), message('c', 'b', {message_count: 11}),
    message('c', 'd', {message_count: 13}), message('c', 'c', {message_count: 17}),
  ]));
  const analysis = analysisFixture([['a', 'b'], ['c', 'd']]);
  const nodes = flowNodes(model, analysis);
  const collapsed = new Set(['g0', 'g1']);
  for (const rootId of ['a', 'c']) {
    const view = graphPresentation(model, nodes, analysis, {flowRootId: rootId, collapsed});
    assert.deepEqual(view.nodes.filter(node => node.type === 'agent').map(node => node.data.agent.id), [rootId]);
    const aggregates = view.nodes.filter(node => node.type === 'community');
    assert.equal(aggregates.reduce((sum, node) => sum + node.data.community.members.length, 0), 3);
    assert.ok(aggregates.every(node => !node.data.community.members.includes(rootId)));
    const total = aggregates.reduce((sum, node) => sum + node.data.internalMessages, 0) + view.edges.reduce((sum, edge) => sum + edge.data.messageCount, 0);
    assert.equal(total, 56);
    assert.ok(view.edges.every(edge => view.nodes.some(node => node.id === edge.source) && view.nodes.some(node => node.id === edge.target)));
    const single = aggregates.find(node => node.data.community.members.length === 1);
    assert.deepEqual(single.position, nodes.find(node => node.data.agent.id === single.data.community.members[0]).position);
  }
});

test('new Flow aggregates clear a visible root and one another along the cross-flow axis', () => {
  const model = snapshotModel(snapshot(['root', 'a', 'b', 'c', 'd'].map(id => agent(id)), [message('root', 'a'), message('b', 'c')]));
  const analysis = analysisFixture([['root'], ['a', 'b'], ['c', 'd']]);
  const collapsed = new Set(['g1', 'g2']);
  for (const flowDirection of ['RIGHT', 'DOWN']) {
    const point = (flow, across) => flowDirection === 'RIGHT' ? {x: flow, y: across} : {x: across, y: flow};
    const gap = flowDirection === 'RIGHT' ? 154 : 284;
    const positions = new Map([
      ['root', point(400, gap)], ['a', point(400, 0)], ['b', point(400, gap * 2)],
      ['c', point(400, 0)], ['d', point(400, gap * 2)],
    ]);
    const base = flowNodes(model, {positions});
    const original = structuredClone(base);
    const options = {flowRootId: 'root', flowDirection, collapsed, pinned: new Set(['root'])};
    const view = graphPresentation(model, base, analysis, options);
    const axis = flowDirection === 'RIGHT' ? 'y' : 'x';
    const along = flowDirection === 'RIGHT' ? 'x' : 'y';
    assert.equal(view.nodes.length, 3);
    assert.deepEqual(view.nodes.find(node => node.data.agent?.id === 'root').position, positions.get('root'));
    assert.ok(view.nodes.every(node => node.position[along] === 400));
    const offsets = view.nodes.map(node => node.position[axis]).sort((a, b) => a - b);
    assert.ok(offsets.slice(1).every((value, i) => value - offsets[i] >= gap));
    assert.deepEqual(base, original, 'automatic spacing does not mutate stored member or pin positions');
    assert.deepEqual(graphPresentation(model, base, analysis, options).nodes.map(node => node.position), view.nodes.map(node => node.position));
    const manualId = communityNodeId('g2');
    const manual = point(400, gap); // Deliberate overlap with the chosen root is retained.
    const moved = graphPresentation(model, base, analysis, {...options, aggregatePositions: new Map([[manualId, manual]])});
    assert.deepEqual(moved.nodes.find(node => node.id === manualId).position, manual);
    assert.notDeepEqual(moved.nodes.find(node => node.id === communityNodeId('g1')).position, manual);
  }
});

test('a service\'s published agents hang under it without creating message links', () => {
  const service = agent('svc', {kind: 'service', agents: [
    {id: 'prover-1', name: 'Prover 1', status: 'live', role: 'prover'},
    {id: 'lemma-7', name: 'Lemma 7', status: 'queueable', role: 'worker', parent: 'prover-1'},
    {id: 'prover-1', name: 'duplicate'}, {name: 'no id'}, {id: 'orphan', parent: 'absent'}, 'text',
  ]});
  const model = snapshotModel(snapshot([service, agent('b')], [message('b', 'svc')]));
  const children = model.agents.filter(entry => entry.via);
  assert.deepEqual(children.map(entry => [entry.id, entry.name, entry.kind, entry.status, entry.via, entry.parent]), [
    ['svc/prover-1', 'Prover 1', 'prover', 'live', 'svc', null],
    ['svc/lemma-7', 'Lemma 7', 'worker', 'queueable', 'svc', 'svc/prover-1'],
    ['svc/orphan', 'orphan', 'agent', 'queueable', 'svc', null],
  ]);
  const parent = model.agents.find(entry => entry.id === 'svc');
  assert.ok(children.every(entry => deviceKey(entry) === deviceKey(parent) && entry.buses.join() === parent.buses.join()));
  assert.deepEqual(model.connections.map(edge => [edge.source, edge.target]), [['b', 'svc']]);
  assert.deepEqual(model.containment, [
    {source: 'svc', target: 'svc/prover-1'}, {source: 'svc/prover-1', target: 'svc/lemma-7'}, {source: 'svc', target: 'svc/orphan'},
  ]);
  // Children follow their service in the device grid and in every presentation.
  const nodes = layoutNodes(model);
  assert.deepEqual(nodes.filter(node => node.type === 'agent').map(node => node.data.agent.id), ['b', 'svc', 'svc/prover-1', 'svc/lemma-7', 'svc/orphan']);
  const view = graphPresentation(model, nodes, null, {selectedId: agentNodeId('svc')});
  const containment = view.edges.filter(edge => edge.data?.containment);
  assert.equal(containment.length, 3);
  assert.ok(containment.every(edge => edge.markerEnd === undefined && edge.label === undefined && edge.style.includes('stroke-dasharray')));
  assert.equal(view.edges.filter(edge => !edge.data?.containment).length, 1);
  assert.equal(view.nodes.find(node => node.id === agentNodeId('svc/prover-1')).data.neighbor, true);
  assert.equal(decorate(model, nodes, null).edges.filter(edge => edge.data?.containment).length, 3);
  // Published agents are not traffic: details count nothing and the inspector names the service.
  const details = agentDetails(model, agentNodeId('svc/lemma-7'));
  assert.equal(details.sent + details.received + details.peerCount, 0);
  assert.equal(details.agent.viaName, 'svc');
});

test('published agents are ignored on non-services, bounded, and keep their own identity space', () => {
  const codex = agent('c', {agents: [{id: 'x'}]});
  assert.equal(snapshotModel(snapshot([codex], [])).agents.length, 1);
  const many = agent('svc', {kind: 'service', agents: Array.from({length: 300}, (_, index) => ({id: 'a' + index}))});
  const bounded = snapshotModel(snapshot([many], []));
  assert.equal(bounded.agents.length, 1 + 256);
  // A child named like a real agent never replaces it, and a child never carries message text.
  const clash = agent('svc', {kind: 'service', agents: [{id: 'b', name: 'Impostor', message: 'secret', description: 'secret'}]});
  const model = snapshotModel(snapshot([clash, agent('b')], [message('b', 'svc')]));
  assert.deepEqual(model.agents.map(entry => entry.id).sort(), ['b', 'svc', 'svc/b']);
  assert.equal(JSON.stringify(model).includes('secret'), false);
  assert.deepEqual(model.connections.map(edge => [edge.source, edge.target]), [['b', 'svc']]);
});
