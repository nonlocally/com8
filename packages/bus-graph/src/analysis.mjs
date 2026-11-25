import {Matrix, EigenvalueDecomposition, CholeskyDecomposition} from 'ml-matrix';
import Network from 'networkanalysis-ts/network.js';
import LeidenAlgorithm from 'networkanalysis-ts/leidenAlgorithm.js';
import Clustering from 'networkanalysis-ts/clustering.js';
import Random from 'java-random';

// Dense eigendecomposition is deliberately bounded. Nothing is dropped when the
// limit is exceeded: the caller gets a labelled, readable fallback for all IDs.
export const ANALYSIS_NODE_LIMIT = 256;
export const CARD_WIDTH = 250;
export const CARD_HEIGHT = 120;
const GAP = 34;
const WIDTH = CARD_WIDTH + GAP;
const HEIGHT = CARD_HEIGHT + GAP;
const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
const dot = (a, b) => a.reduce((sum, value, i) => sum + value * b[i], 0);

// All analysis starts from the permitted snapshot. Self-messages have no
// between-agent relationship; direction is combined only for this analysis.
// Sum the two directions and any bus contributions BEFORE compressing volume.
export function weightedGraph(model = {}) {
  // A service's published agents are its claim, not participants: they never enter the analysis.
  const ids = [...new Set((model.agents || []).filter(agent => agent && !agent.via).map(agent => agent.id).filter(id => typeof id === 'string' && id))].sort(compare);
  const allowed = new Set(ids);
  const pairs = new Map();
  for (const edge of model.connections || []) {
    if (!edge || !allowed.has(edge.source) || !allowed.has(edge.target) || edge.source === edge.target) continue;
    if (!Number.isFinite(edge.message_count) || edge.message_count < 1) continue;
    const [source, target] = [edge.source, edge.target].sort(compare);
    const key = JSON.stringify([source, target]);
    const old = pairs.get(key);
    const count = Math.min(Number.MAX_SAFE_INTEGER, (old?.count || 0) + Math.floor(edge.message_count));
    pairs.set(key, {source, target, count});
  }
  const edges = [...pairs.values()].sort((a, b) => compare(a.source, b.source) || compare(a.target, b.target))
    .map(edge => ({...edge, weight: Math.log1p(edge.count)}));
  return {ids, edges};
}

function signatureFor(graph) {
  // A lossless canonical signature avoids hash collisions and never includes
  // names, credentials, message content, or unpermitted endpoints.
  return JSON.stringify([graph.ids, graph.edges.map(edge => [edge.source, edge.target, edge.count])]);
}

export const graphSignature = model => signatureFor(weightedGraph(model));

function connectedComponents(ids, edges) {
  const neighbors = new Map(ids.map(id => [id, []]));
  for (const edge of edges) {
    neighbors.get(edge.source).push(edge.target);
    neighbors.get(edge.target).push(edge.source);
  }
  const remaining = new Set(ids);
  const components = [];
  for (const first of ids) {
    if (!remaining.delete(first)) continue;
    const members = [first];
    for (let i = 0; i < members.length; i++) {
      for (const neighbor of neighbors.get(members[i])) if (remaining.delete(neighbor)) members.push(neighbor);
    }
    components.push(members.sort(compare));
  }
  return components.sort((a, b) => b.length - a.length || compare(a[0], b[0]));
}

function orient(vector) {
  let anchor = 0;
  for (let i = 1; i < vector.length; i++) if (Math.abs(vector[i]) > Math.abs(vector[anchor]) + 1e-12) anchor = i;
  return vector[anchor] < 0 ? vector.map(value => -value) : vector;
}

// For a repeated eigenvalue, individual solver eigenvectors are arbitrary.
// Project canonical ID-ordered unit vectors into the WHOLE eigenspace, then
// Gram–Schmidt. The resulting basis is independent of the solver's rotation.
function canonicalModes(values, vectors, n) {
  const modes = [];
  for (let start = 1; start < n && modes.length < 2;) {
    let end = start + 1;
    while (end < n && Math.abs(values[end] - values[start]) <= 1e-10) end++;
    const basis = [];
    for (let anchor = 0; anchor < n && basis.length < end - start && modes.length < 2; anchor++) {
      const vector = Array.from({length: n}, (_, row) => {
        let value = 0;
        for (let column = start; column < end; column++) value += vectors.get(row, column) * vectors.get(anchor, column);
        return value;
      });
      // Two passes reduce cancellation for nearly dependent projections.
      for (let pass = 0; pass < 2; pass++) for (const previous of basis) {
        const projection = dot(vector, previous);
        for (let row = 0; row < n; row++) vector[row] -= projection * previous[row];
      }
      const norm = Math.sqrt(dot(vector, vector));
      if (norm <= 1e-9) continue;
      const normalized = orient(vector.map(value => value / norm));
      basis.push(normalized);
      modes.push({eigenvalue: values[start], vector: normalized});
    }
    start = end;
  }
  return modes;
}

function spectralComponent(ids, edges) {
  const n = ids.length;
  if (n === 1) return {eigenvalues: [], modes: [], points: [{x: 0, y: 0}], layout: 'isolate'};
  const index = new Map(ids.map((id, i) => [id, i]));
  const degrees = Array(n).fill(0);
  for (const edge of edges) {
    degrees[index.get(edge.source)] += edge.weight;
    degrees[index.get(edge.target)] += edge.weight;
  }
  const laplacian = Matrix.eye(n);
  for (const edge of edges) {
    const source = index.get(edge.source), target = index.get(edge.target);
    const value = -edge.weight / Math.sqrt(degrees[source] * degrees[target]);
    laplacian.set(source, target, value);
    laplacian.set(target, source, value);
  }
  const decomposition = new EigenvalueDecomposition(laplacian, {assumeSymmetric: true});
  const values = decomposition.realEigenvalues;
  if (!values.every(Number.isFinite)) throw new Error('Nonfinite eigendecomposition');
  const modes = canonicalModes(values, decomposition.eigenvectorMatrix, n);
  if (modes.length !== Math.min(2, n - 1)) throw new Error('Incomplete spectral basis');
  // A connected component has exactly one zero mode. Never threshold away a
  // small positive Fiedler value: it may represent a weak but real bridge.
  const points = ids.map((_, i) => ({x: modes[0].vector[i], y: modes[1]?.vector[i] || 0}));
  const rangeX = Math.max(...points.map(p => p.x)) - Math.min(...points.map(p => p.x));
  const rangeY = Math.max(...points.map(p => p.y)) - Math.min(...points.map(p => p.y));
  const scale = Math.max(580, Math.sqrt(n) * 300) / Math.max(rangeX, rangeY, 1e-9);
  return {eigenvalues: values.slice(1), modes, layout: 'spectral', points: points.map(point => ({x: point.x * scale, y: point.y * scale}))};
}

function gridPoints(n) {
  const columns = Math.max(1, Math.ceil(Math.sqrt(n)));
  return Array.from({length: n}, (_, i) => ({x: (i % columns) * WIDTH, y: Math.floor(i / columns) * HEIGHT}));
}

// Projection onto readable card positions. The bounded overlap relaxation and
// final interval placement together guarantee non-overlapping rectangles.
function separateCards(raw, passes = 80) {
  const points = raw.map(point => ({...point}));
  for (let iteration = 0; iteration < passes; iteration++) {
    let collisions = 0;
    for (let i = 0; i < points.length; i++) for (let j = i + 1; j < points.length; j++) {
      const a = points[i], b = points[j];
      const dx = b.x - a.x, dy = b.y - a.y;
      const overlapX = WIDTH - Math.abs(dx), overlapY = HEIGHT - Math.abs(dy);
      if (overlapX <= 0 || overlapY <= 0) continue;
      collisions++;
      if (overlapX / WIDTH < overlapY / HEIGHT) {
        const displacement = (overlapX / 2 + .01) * (dx < 0 ? -1 : 1);
        a.x -= displacement; b.x += displacement;
      } else {
        const displacement = (overlapY / 2 + .01) * (dy < 0 ? -1 : 1);
        a.y -= displacement; b.y += displacement;
      }
    }
    if (!collisions) return points;
  }
  // A dense tied eigenspace can retain tiny overlaps after bounded relaxation.
  // Find the nearest legal vertical gap for each residual card, guaranteeing
  // readability without an unbounded simulation or a global force layout.
  const placed = [];
  for (const point of points) {
    const intervals = placed.filter(other => Math.abs(other.x - point.x) < WIDTH)
      .map(other => [other.y - HEIGHT, other.y + HEIGHT]).sort((a, b) => a[0] - b[0]);
    const merged = [];
    for (const interval of intervals) {
      const last = merged.at(-1);
      if (last && interval[0] <= last[1]) last[1] = Math.max(last[1], interval[1]);
      else merged.push([...interval]);
    }
    const blocked = merged.find(([lower, upper]) => point.y > lower && point.y < upper);
    if (blocked) point.y = point.y - blocked[0] <= blocked[1] - point.y ? blocked[0] - .01 : blocked[1] + .01;
    placed.push(point);
  }
  return points;
}

// Display refinement constants are independent of agent names, kinds and roles.
// At n<=256 the dense path matrix and one Cholesky factorization are bounded;
// all iterations reuse the same factorization and never run a live simulation.
export const SPACING_ITERATIONS = 36;
const SPECTRAL_ANCHOR_STRENGTH = .06;
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

function seedTiedPoints(raw) {
  const seeded = raw.map(point => ({...point}));
  const visited = new Set();
  for (let i = 0; i < raw.length; i++) {
    if (visited.has(i)) continue;
    const tied = [];
    for (let j = i; j < raw.length; j++) if (Math.hypot(raw[i].x - raw[j].x, raw[i].y - raw[j].y) < 1e-6) {
      tied.push(j); visited.add(j);
    }
    if (tied.length < 2) continue;
    // Equal spectral coordinates cannot choose a direction by themselves.
    // A canonical-ID-ordered, centered sunflower breaks that display tie in
    // two dimensions. It does not change the raw coordinates or eigenmodes.
    const offsets = tied.map((_, k) => ({x: Math.sqrt(k + .5) * WIDTH * .62 * Math.cos(k * GOLDEN_ANGLE), y: Math.sqrt(k + .5) * HEIGHT * .8 * Math.sin(k * GOLDEN_ANGLE)}));
    const center = offsets.reduce((sum, p) => ({x: sum.x + p.x / offsets.length, y: sum.y + p.y / offsets.length}), {x: 0, y: 0});
    tied.forEach((j, k) => {seeded[j].x += offsets[k].x - center.x; seeded[j].y += offsets[k].y - center.y;});
  }
  return seeded;
}

/**
 * Refine one connected component while retaining its spectral anchors.
 *
 * The objective is a constrained weighted stress, not a role-based force rule:
 *   E(X) = sum_{i<j} (||Xi-Xj|| - dij)^2 / dij^2
 *          + lambda * sum_i ||Xi-Si||^2,
 * with non-overlapping card rectangles. Si are the original spectral points.
 * dij are weighted shortest-path lengths, scaled uniformly for card density.
 * Their edge costs are clamp(sqrt(median(w)/w), .4, 2.5), where w is log1p
 * retained traffic. lambda=.06 times mean row degree of the stress Laplacian.
 *
 * Anchored SMACOF majorization is followed by readable-card projection each
 * iteration. Projection is approximate, so monotonic descent/global optimality
 * is NOT claimed. We retain the best feasible objective encountered, starting
 * with a feasible spectral layout. The displayed diagnostics report both
 * stress and displacement honestly; spectra/Leiden are never modified.
 *
 * ids must be canonical and points aligned to ids. Inputs are never mutated.
 */
export function refineSpectralSpacing(ids, edges, raw) {
  const n = ids.length;
  if (n < 2 || n > ANALYSIS_NODE_LIMIT || !edges.length) return {points: separateCards(raw), diagnostics: null};
  const index = new Map(ids.map((id, i) => [id, i]));
  const localEdges = edges.filter(edge => index.has(edge.source) && index.has(edge.target) && edge.source !== edge.target && Number.isFinite(edge.weight) && edge.weight > 0);
  if (!localEdges.length) return {points: separateCards(raw), diagnostics: null};
  const sortedWeights = localEdges.map(edge => edge.weight).sort((a, b) => a - b);
  const middle = Math.floor(sortedWeights.length / 2);
  const medianWeight = sortedWeights.length % 2 ? sortedWeights[middle] : (sortedWeights[middle - 1] + sortedWeights[middle]) / 2;
  const paths = Array.from({length: n}, (_, i) => Float64Array.from({length: n}, (_, j) => i === j ? 0 : Infinity));
  for (const edge of localEdges) {
    const i = index.get(edge.source), j = index.get(edge.target);
    const length = Math.max(.4, Math.min(2.5, Math.sqrt(medianWeight / edge.weight)));
    paths[i][j] = Math.min(paths[i][j], length);
    paths[j][i] = paths[i][j];
  }
  for (let k = 0; k < n; k++) for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
    const through = paths[i][k] + paths[k][j];
    if (through < paths[i][j]) {paths[i][j] = through; paths[j][i] = through;}
  }
  const pairs = [];
  let squaredDistance = 0;
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
    if (!Number.isFinite(paths[i][j])) throw new Error('Spacing requires one connected component');
    pairs.push({i, j, distance: paths[i][j]});
    squaredDistance += paths[i][j] ** 2;
  }
  const desiredRmsDistance = Math.max(430, 210 * Math.sqrt(n));
  const scale = desiredRmsDistance / Math.sqrt(squaredDistance / pairs.length);
  const matrix = Matrix.zeros(n, n);
  let degreeSum = 0;
  for (const pair of pairs) {
    pair.distance *= scale;
    pair.weight = 1 / pair.distance ** 2;
    matrix.set(pair.i, pair.j, -pair.weight);
    matrix.set(pair.j, pair.i, -pair.weight);
    matrix.set(pair.i, pair.i, matrix.get(pair.i, pair.i) + pair.weight);
    matrix.set(pair.j, pair.j, matrix.get(pair.j, pair.j) + pair.weight);
    degreeSum += 2 * pair.weight;
  }
  const anchorWeight = SPECTRAL_ANCHOR_STRENGTH * degreeSum / n;
  for (let i = 0; i < n; i++) matrix.set(i, i, matrix.get(i, i) + anchorWeight);
  const factor = new CholeskyDecomposition(matrix);
  const score = points => {
    let stress = 0, displacement = 0;
    for (const {i, j, distance, weight} of pairs) stress += weight * (Math.hypot(points[i].x - points[j].x, points[i].y - points[j].y) - distance) ** 2;
    for (let i = 0; i < n; i++) displacement += (points[i].x - raw[i].x) ** 2 + (points[i].y - raw[i].y) ** 2;
    return {stress: stress / pairs.length, objective: (stress + anchorWeight * displacement) / pairs.length, rmsDisplacement: Math.sqrt(displacement / n)};
  };
  // Keep the prior collision-only solution as a feasible candidate too: the
  // refinement may change it only when the declared objective improves.
  const baseline = separateCards(raw);
  let points = separateCards(seedTiedPoints(raw), 6);
  const before = score(baseline), seeded = score(points);
  let best = seeded.objective < before.objective ? points : baseline;
  let bestScore = seeded.objective < before.objective ? seeded : before;
  let iterations = 0;
  for (; iterations < SPACING_ITERATIONS; iterations++) {
    const rhs = Matrix.zeros(n, 2);
    for (let i = 0; i < n; i++) {rhs.set(i, 0, anchorWeight * raw[i].x); rhs.set(i, 1, anchorWeight * raw[i].y);}
    for (const {i, j, distance, weight} of pairs) {
      const dx = points[i].x - points[j].x, dy = points[i].y - points[j].y;
      const length = Math.max(1e-9, Math.hypot(dx, dy));
      const coefficient = weight * distance / length;
      rhs.set(i, 0, rhs.get(i, 0) + coefficient * dx); rhs.set(j, 0, rhs.get(j, 0) - coefficient * dx);
      rhs.set(i, 1, rhs.get(i, 1) + coefficient * dy); rhs.set(j, 1, rhs.get(j, 1) - coefficient * dy);
    }
    const solved = factor.solve(rhs);
    const next = separateCards(points.map((point, i) => ({x: .25 * point.x + .75 * solved.get(i, 0), y: .25 * point.y + .75 * solved.get(i, 1)})), 6);
    const nextScore = score(next);
    if (!Number.isFinite(nextScore.objective)) throw new Error('Nonfinite spacing refinement');
    if (nextScore.objective < bestScore.objective) {best = next; bestScore = nextScore;}
    const movement = Math.max(...next.map((point, i) => Math.hypot(point.x - points[i].x, point.y - points[i].y)));
    points = next;
    if (movement < .1) {iterations++; break;}
  }
  return {points: best.map(point => ({...point})), diagnostics: {iterations, pairCount: pairs.length, desiredRmsDistance,
    anchorStrength: SPECTRAL_ANCHOR_STRENGTH, stressBefore: before.stress, stressAfter: bestScore.stress,
    objectiveBefore: before.objective, objectiveAfter: bestScore.objective, rmsDisplacement: bestScore.rmsDisplacement}};
}

function makeCommunities(groups) {
  return groups.map(members => [...members].sort(compare))
    .sort((a, b) => b.length - a.length || compare(a[0], b[0]))
    .map((members, i) => ({id: `community:${JSON.stringify(members)}`, label: `Group ${i + 1}`, members, colorIndex: i % 8}));
}

function leidenCommunities(graph) {
  if (!graph.edges.length) return {communities: makeCommunities(graph.ids.map(id => [id])), modularity: null};
  // The core API avoids the run helper's console logging and node mutation.
  // Degree node weights + resolution 1/(2m) makes its CPM quality expression
  // exactly standard weighted Newman–Girvan modularity at gamma = 1.
  const index = new Map(graph.ids.map((id, i) => [id, i]));
  const network = new Network({nNodes: graph.ids.length, setNodeWeightsToTotalEdgeWeights: true,
    edges: [graph.edges.map(edge => index.get(edge.source)), graph.edges.map(edge => index.get(edge.target))],
    edgeWeights: graph.edges.map(edge => edge.weight), sortedEdges: false, checkIntegrity: true});
  let best = null, quality = -Infinity, bestKey = '';
  for (const seed of [17, 29, 43]) {
    const algorithm = new LeidenAlgorithm();
    algorithm.initializeBasedOnResolutionAndNIterationsAndRandomnessAndRandom(1 / (2 * network.getTotalEdgeWeight()), 10, .01, new Random(seed));
    const clustering = new Clustering({nNodes: graph.ids.length});
    algorithm.improveClustering(network, clustering);
    const groups = new Map();
    clustering.getClusters().forEach((cluster, i) => {
      if (!groups.has(cluster)) groups.set(cluster, []);
      groups.get(cluster).push(graph.ids[i]);
    });
    const communities = makeCommunities([...groups.values()]);
    const candidateQuality = algorithm.calcQuality(network, clustering);
    const key = JSON.stringify(communities.map(community => community.members));
    if (candidateQuality > quality + 1e-12 || (Math.abs(candidateQuality - quality) <= 1e-12 && compare(key, bestKey) < 0)) {
      best = communities; quality = candidateQuality; bestKey = key;
    }
  }
  if (!best || !Number.isFinite(quality)) throw new Error('Invalid Leiden partition');
  return {communities: best, modularity: quality};
}

export function analyzeGraph(model = {}) {
  const graph = weightedGraph(model);
  const groups = connectedComponents(graph.ids, graph.edges);
  const limited = graph.ids.length > ANALYSIS_NODE_LIMIT;
  const warnings = limited ? [`Spectral and Leiden analysis is limited to ${ANALYSIS_NODE_LIMIT} agents. Showing all ${graph.ids.length} agents in a grid with connected-component groups.`] : [];
  const positions = new Map(), rawPositions = new Map(), components = [];
  const componentById = new Map();
  groups.forEach((ids, i) => ids.forEach(id => componentById.set(id, i)));
  const componentEdges = groups.map(() => []);
  graph.edges.forEach(edge => componentEdges[componentById.get(edge.source)].push(edge));
  const layouts = groups.map((ids, i) => {
    let result;
    try {
      result = limited ? {eigenvalues: [], modes: [], layout: ids.length > 1 ? 'grid' : 'isolate', points: gridPoints(ids.length)} : spectralComponent(ids, componentEdges[i]);
    } catch {
      warnings.push(`Spectral analysis could not resolve a ${ids.length}-agent component; that component uses a grid.`);
      result = {eigenvalues: [], modes: [], layout: 'grid', points: gridPoints(ids.length)};
    }
    let refinement;
    try {
      refinement = result.layout === 'spectral' ? refineSpectralSpacing(ids, componentEdges[i], result.points) : {points: result.points.map(point => ({...point})), diagnostics: null};
    } catch {
      warnings.push(`Relationship spacing could not resolve a ${ids.length}-agent component; retaining spectral card spacing.`);
      refinement = {points: separateCards(result.points), diagnostics: null};
    }
    const readable = refinement.points;
    // Pack the displayed geometry. The raw spectral geometry receives the same
    // translation, but need not reserve blank space outside readable cards.
    const minX = Math.min(...readable.map(point => point.x));
    const minY = Math.min(...readable.map(point => point.y));
    const width = Math.max(...readable.map(point => point.x)) - minX + CARD_WIDTH;
    const height = Math.max(...readable.map(point => point.y)) - minY + CARD_HEIGHT;
    components.push({id: `component:${JSON.stringify(ids)}`, ids, eigenvalues: result.eigenvalues, modes: result.modes, layout: result.layout, spacing: refinement.diagnostics});
    return {ids, readable, raw: result.points, minX, minY, width, height};
  });
  // Components share no spectral coordinate system, so pack their own canvases.
  const targetWidth = Math.max(1000, Math.sqrt(layouts.reduce((area, item) => area + (item.width + 140) * (item.height + 140), 0)) * 1.3);
  let x = 0, y = 0, rowHeight = 0;
  for (const layout of layouts) {
    if (x && x + layout.width > targetWidth) {x = 0; y += rowHeight + 140; rowHeight = 0;}
    layout.ids.forEach((id, i) => {
      positions.set(id, {x: layout.readable[i].x - layout.minX + x, y: layout.readable[i].y - layout.minY + y});
      rawPositions.set(id, {x: layout.raw[i].x - layout.minX + x, y: layout.raw[i].y - layout.minY + y});
    });
    x += layout.width + 140;
    rowHeight = Math.max(rowHeight, layout.height);
  }
  let communityResult;
  try {
    communityResult = limited ? {communities: makeCommunities(groups), modularity: null} : leidenCommunities(graph);
  } catch {
    warnings.push('Leiden analysis could not be completed; showing connected-component groups.');
    communityResult = {communities: makeCommunities(groups), modularity: null};
  }
  const {communities, modularity} = communityResult;
  const communityByAgent = new Map();
  for (const community of communities) for (const id of community.members) communityByAgent.set(id, community.id);
  return {
    signature: signatureFor(graph), positions, rawPositions, communities, communityByAgent, components,
    stats: {agents: graph.ids.length, relationships: graph.edges.length, components: groups.length, isolates: groups.filter(ids => ids.length === 1).length, communities: communities.length, modularity},
    method: {
      layout: components.some(component => component.layout === 'grid') ? 'Grid fallback (see analysis warning)' : warnings.some(warning => warning.startsWith('Relationship spacing')) ? 'Normalized Laplacian · spectral card spacing fallback' : 'Normalized Laplacian · anchored graph-distance stress · card spacing',
      community: limited || warnings.some(warning => warning.startsWith('Leiden')) ? 'Connected components (Leiden unavailable)' : 'Leiden · weighted modularity · resolution 1 · three seeded starts',
      weighting: 'Undirected weight = log(1 + retained messages in both directions); self-messages excluded',
    },
    warning: warnings.length ? warnings.join(' ') : null,
  };
}
