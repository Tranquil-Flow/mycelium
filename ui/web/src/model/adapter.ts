import {
  EVIDENCE_SNAPSHOT_PROTOCOL,
  type EvidenceLink,
  type EvidenceNode,
  type EvidenceRoute,
  type EvidenceRouteStage,
  type EvidenceSnapshot,
  type EvidenceValue,
  type KnownNodeLocation,
  type NodeLocation,
  type StageMemory,
} from './types';
import {
  EvidenceParseError,
  array,
  boolean,
  dateTimeString,
  deepFreeze,
  finiteNumber,
  nonNegativeInteger,
  nonNegativeNumber,
  offlineClaimBoundary,
  oneOf,
  positiveInteger,
  record,
  sameStringArrays,
  string,
  stringArray,
  unique,
} from './runtime';

const SIMULATOR_PROTOCOL = 'mycelium.planner_simulation.v1';
const GEO_PROTOCOL = 'mycelium.ui_geo_fixture.v1';
const FIXTURE_MANIFEST_PROTOCOL = 'mycelium.ui_fixture_manifest.v1';
const FIXTURE_FILES = [
  'hypothetical-six-node.json',
  'planner-simulation.json',
  'synthetic-geo.json',
] as const;

interface ParsedModelDimensions {
  readonly id: string;
  readonly numLayers: number;
  readonly hiddenSize: number;
  readonly layerWeightGb: number;
  readonly decodeGflopsPerLayer: number;
  readonly prefillGflopsPerLayerPerToken: number;
  readonly activationBytes: number;
  readonly kvHeads: number;
  readonly headDim: number;
  readonly kvBytes: number;
  readonly tokenEnvelopeBytes: number;
}

interface ParsedWorkload {
  readonly contextWindow: number;
  readonly concurrentRequests: number;
  readonly outputTokens: number;
  readonly contextFractionPerRequest: number;
  readonly kvSafetyMultiplier: number;
}

function parseModelDimensions(value: unknown, path: string): ParsedModelDimensions {
  const model = record(value, path);
  return {
    id: string(model.model_id, `${path}.model_id`),
    numLayers: positiveInteger(model.num_layers, `${path}.num_layers`),
    hiddenSize: positiveInteger(model.hidden_size, `${path}.hidden_size`),
    layerWeightGb: nonNegativeNumber(model.layer_weight_gb, `${path}.layer_weight_gb`),
    decodeGflopsPerLayer: nonNegativeNumber(
      model.decode_gflops_per_layer,
      `${path}.decode_gflops_per_layer`,
    ),
    prefillGflopsPerLayerPerToken: nonNegativeNumber(
      model.prefill_gflops_per_layer_per_token,
      `${path}.prefill_gflops_per_layer_per_token`,
    ),
    activationBytes: positiveInteger(model.activation_bytes, `${path}.activation_bytes`),
    kvHeads: positiveInteger(model.kv_heads, `${path}.kv_heads`),
    headDim: positiveInteger(model.head_dim, `${path}.head_dim`),
    kvBytes: positiveInteger(model.kv_bytes, `${path}.kv_bytes`),
    tokenEnvelopeBytes: positiveInteger(
      model.token_envelope_bytes,
      `${path}.token_envelope_bytes`,
    ),
  };
}

function parseWorkload(value: unknown, path: string): ParsedWorkload {
  const workload = record(value, path);
  const contextFractionPerRequest = nonNegativeNumber(
    workload.context_fraction_per_request,
    `${path}.context_fraction_per_request`,
  );
  if (contextFractionPerRequest > 1) {
    throw new EvidenceParseError(
      `${path}.context_fraction_per_request`,
      'a ratio from 0 through 1',
    );
  }
  const kvSafetyMultiplier = nonNegativeNumber(
    workload.kv_safety_multiplier,
    `${path}.kv_safety_multiplier`,
  );
  if (kvSafetyMultiplier === 0) {
    throw new EvidenceParseError(`${path}.kv_safety_multiplier`, 'a positive finite number');
  }

  return {
    contextWindow: positiveInteger(workload.context_window, `${path}.context_window`),
    concurrentRequests: positiveInteger(
      workload.concurrent_requests,
      `${path}.concurrent_requests`,
    ),
    outputTokens: positiveInteger(workload.output_tokens, `${path}.output_tokens`),
    contextFractionPerRequest,
    kvSafetyMultiplier,
  };
}

function assertSame<T>(actual: T, expected: T, path: string): void {
  if (!Object.is(actual, expected)) {
    throw new EvidenceParseError(path, `the scenario value ${JSON.stringify(expected)}`);
  }
}

function assertMatchingModel(
  scenarioModel: ParsedModelDimensions,
  reportModel: ParsedModelDimensions,
): void {
  assertSame(reportModel.id, scenarioModel.id, 'simulation.model.model_id');
  assertSame(reportModel.numLayers, scenarioModel.numLayers, 'simulation.model.num_layers');
  assertSame(reportModel.hiddenSize, scenarioModel.hiddenSize, 'simulation.model.hidden_size');
  assertSame(
    reportModel.layerWeightGb,
    scenarioModel.layerWeightGb,
    'simulation.model.layer_weight_gb',
  );
  assertSame(
    reportModel.decodeGflopsPerLayer,
    scenarioModel.decodeGflopsPerLayer,
    'simulation.model.decode_gflops_per_layer',
  );
  assertSame(
    reportModel.prefillGflopsPerLayerPerToken,
    scenarioModel.prefillGflopsPerLayerPerToken,
    'simulation.model.prefill_gflops_per_layer_per_token',
  );
  assertSame(
    reportModel.activationBytes,
    scenarioModel.activationBytes,
    'simulation.model.activation_bytes',
  );
  assertSame(reportModel.kvHeads, scenarioModel.kvHeads, 'simulation.model.kv_heads');
  assertSame(reportModel.headDim, scenarioModel.headDim, 'simulation.model.head_dim');
  assertSame(reportModel.kvBytes, scenarioModel.kvBytes, 'simulation.model.kv_bytes');
  assertSame(
    reportModel.tokenEnvelopeBytes,
    scenarioModel.tokenEnvelopeBytes,
    'simulation.model.token_envelope_bytes',
  );
}

function assertMatchingWorkload(
  scenarioWorkload: ParsedWorkload,
  reportWorkload: ParsedWorkload,
): void {
  assertSame(
    reportWorkload.contextWindow,
    scenarioWorkload.contextWindow,
    'simulation.workload.context_window',
  );
  assertSame(
    reportWorkload.concurrentRequests,
    scenarioWorkload.concurrentRequests,
    'simulation.workload.concurrent_requests',
  );
  assertSame(
    reportWorkload.outputTokens,
    scenarioWorkload.outputTokens,
    'simulation.workload.output_tokens',
  );
  assertSame(
    reportWorkload.contextFractionPerRequest,
    scenarioWorkload.contextFractionPerRequest,
    'simulation.workload.context_fraction_per_request',
  );
  assertSame(
    reportWorkload.kvSafetyMultiplier,
    scenarioWorkload.kvSafetyMultiplier,
    'simulation.workload.kv_safety_multiplier',
  );
}

function synthetic(value: number): EvidenceValue<number> {
  return { value, provenance: 'synthetic' };
}

function parseLocation(value: unknown, path: string): KnownNodeLocation {
  const location = record(value, path);
  const latitude = finiteNumber(location.lat, `${path}.lat`);
  const longitude = finiteNumber(location.lon, `${path}.lon`);
  if (latitude < -90 || latitude > 90) {
    throw new EvidenceParseError(`${path}.lat`, 'a latitude from -90 through 90');
  }
  if (longitude < -180 || longitude > 180) {
    throw new EvidenceParseError(`${path}.lon`, 'a longitude from -180 through 180');
  }

  return {
    state: 'known',
    provenance: 'synthetic',
    city: string(location.city, `${path}.city`),
    country: string(location.country, `${path}.country`),
    latitude,
    longitude,
    precision: string(location.precision, `${path}.precision`),
  };
}

function parseNode(
  value: unknown,
  index: number,
  geoNodes: Record<string, unknown>,
): EvidenceNode {
  const path = `scenario.nodes[${index}]`;
  const node = record(value, path);
  const id = string(node.node_id, `${path}.node_id`);
  const locationSource = geoNodes[id];
  let location: NodeLocation;
  if (locationSource === null) {
    location = {
      state: 'unknown',
      provenance: 'unknown',
      reason: 'source_explicitly_unknown',
    };
  } else if (locationSource === undefined) {
    location = { state: 'unknown', provenance: 'unknown', reason: 'not_provided' };
  } else {
    location = parseLocation(locationSource, `geography.nodes.${id}`);
  }

  return {
    id,
    resources: {
      gpuTeraflops: nonNegativeNumber(node.gpu_tflops, `${path}.gpu_tflops`),
      cpuTeraflops: nonNegativeNumber(node.cpu_tflops, `${path}.cpu_tflops`),
      vramAvailableGb: nonNegativeNumber(node.vram_available_gb, `${path}.vram_available_gb`),
      ramAvailableGb: nonNegativeNumber(node.ram_available_gb, `${path}.ram_available_gb`),
      gpuMemoryBandwidthGbps: nonNegativeNumber(
        node.gpu_memory_bandwidth_gbps,
        `${path}.gpu_memory_bandwidth_gbps`,
      ),
      ramBandwidthGbps: nonNegativeNumber(
        node.ram_bandwidth_gbps,
        `${path}.ram_bandwidth_gbps`,
      ),
      vramRamBandwidthGbps: nonNegativeNumber(
        node.vram_ram_bandwidth_gbps,
        `${path}.vram_ram_bandwidth_gbps`,
      ),
      unifiedMemory: boolean(node.unified_memory, `${path}.unified_memory`),
      workspaceGb: nonNegativeNumber(node.workspace_gb, `${path}.workspace_gb`),
    },
    location,
    provenance: 'synthetic',
  };
}

function parseLink(
  value: unknown,
  index: number,
  knownNodeIds: ReadonlySet<string>,
): EvidenceLink {
  const path = `scenario.links[${index}]`;
  const link = record(value, path);
  const source = string(link.src, `${path}.src`);
  const target = string(link.dst, `${path}.dst`);
  if (!knownNodeIds.has(source)) {
    throw new EvidenceParseError(`${path}.src`, 'a node present in scenario.nodes');
  }
  if (!knownNodeIds.has(target)) {
    throw new EvidenceParseError(`${path}.dst`, 'a node present in scenario.nodes');
  }
  if (source === target) {
    throw new EvidenceParseError(path, 'a link between two distinct nodes');
  }

  const lossRatio = nonNegativeNumber(link.loss_ratio, `${path}.loss_ratio`);
  if (lossRatio > 1) {
    throw new EvidenceParseError(`${path}.loss_ratio`, 'a ratio from 0 through 1');
  }

  return {
    id: `${source}->${target}`,
    source,
    target,
    bidirectional: boolean(link.bidirectional, `${path}.bidirectional`),
    metrics: {
      roundTripTimeMs: synthetic(nonNegativeNumber(link.rtt_ms, `${path}.rtt_ms`)),
      jitterMs: synthetic(nonNegativeNumber(link.jitter_ms, `${path}.jitter_ms`)),
      bandwidthMbps: synthetic(nonNegativeNumber(link.bandwidth_mbps, `${path}.bandwidth_mbps`)),
      lossRatio: synthetic(lossRatio),
    },
    provenance: 'synthetic',
  };
}

function nullableString(value: unknown, path: string): string | null {
  return value === null ? null : string(value, path);
}

function parseMemory(value: unknown, path: string): StageMemory {
  const memory = record(value, path);
  return {
    weightsGb: nonNegativeNumber(memory.weights_gb, `${path}.weights_gb`),
    kvCacheGb: nonNegativeNumber(memory.kv_cache_gb, `${path}.kv_cache_gb`),
    kvCacheInRamGb: nonNegativeNumber(memory.kv_cache_in_ram_gb, `${path}.kv_cache_in_ram_gb`),
    ramUsedGb: nonNegativeNumber(memory.ram_used_gb, `${path}.ram_used_gb`),
    vramUsedGb: nonNegativeNumber(memory.vram_used_gb, `${path}.vram_used_gb`),
    residentLayerCount: nonNegativeInteger(
      memory.resident_layer_count,
      `${path}.resident_layer_count`,
    ),
    ramLayerCount: nonNegativeInteger(memory.ram_layer_count, `${path}.ram_layer_count`),
    ramExecution: nullableString(memory.ram_execution, `${path}.ram_execution`),
  };
}

function parseStage(
  value: unknown,
  path: string,
  strategyRingId: string,
  numLayers: number,
  knownNodeIds: ReadonlySet<string>,
): EvidenceRouteStage {
  const stage = record(value, path);
  const nodeId = string(stage.node_id, `${path}.node_id`);
  if (!knownNodeIds.has(nodeId)) {
    throw new EvidenceParseError(`${path}.node_id`, 'a node present in scenario.nodes');
  }

  const layers = array(stage.layers, `${path}.layers`);
  if (layers.length !== 2) {
    throw new EvidenceParseError(`${path}.layers`, 'an inclusive [start, end] pair');
  }
  const startLayer = nonNegativeInteger(layers[0], `${path}.layers[0]`);
  const endLayerInclusive = nonNegativeInteger(layers[1], `${path}.layers[1]`);
  if (endLayerInclusive < startLayer) {
    throw new EvidenceParseError(`${path}.layers`, 'an ordered inclusive layer range');
  }
  if (endLayerInclusive >= numLayers) {
    throw new EvidenceParseError(`${path}.layers[1]`, `an index below model.num_layers (${numLayers})`);
  }
  const layerCount = positiveInteger(stage.layer_count, `${path}.layer_count`);
  if (layerCount !== endLayerInclusive - startLayer + 1) {
    throw new EvidenceParseError(
      `${path}.layer_count`,
      'the size of the inclusive simulator layer range',
    );
  }

  const stageRingId = string(stage.ring_id, `${path}.ring_id`);
  if (stageRingId !== strategyRingId) {
    throw new EvidenceParseError(`${path}.ring_id`, `the strategy ring id ${strategyRingId}`);
  }

  return {
    id: string(stage.stage_signature, `${path}.stage_signature`),
    nodeId,
    startLayer,
    endLayerExclusive: endLayerInclusive + 1,
    layerCount,
    pathClass: string(stage.path_class, `${path}.path_class`),
    pathPriority: nonNegativeInteger(stage.path_priority, `${path}.path_priority`),
    memory: parseMemory(stage.memory, `${path}.memory`),
    metrics: {
      decodeComputeMs: synthetic(
        nonNegativeNumber(stage.decode_compute_ms, `${path}.decode_compute_ms`),
      ),
      decodeOutgoingMs: synthetic(
        nonNegativeNumber(stage.decode_outgoing_ms, `${path}.decode_outgoing_ms`),
      ),
      prefillComputeMs: synthetic(
        nonNegativeNumber(stage.prefill_compute_ms, `${path}.prefill_compute_ms`),
      ),
      prefillOutgoingMs: synthetic(
        nonNegativeNumber(stage.prefill_outgoing_ms, `${path}.prefill_outgoing_ms`),
      ),
    },
    provenance: 'synthetic',
  };
}

function parseRoute(
  id: string,
  value: unknown,
  numLayers: number,
  knownNodeIds: ReadonlySet<string>,
): EvidenceRoute | null {
  const path = `simulation.strategies.${id}`;
  const strategy = record(value, path);
  if (!boolean(strategy.ok, `${path}.ok`)) {
    return null;
  }

  const ringId = string(strategy.ring_id, `${path}.ring_id`);
  const nodeOrder = stringArray(strategy.node_order, `${path}.node_order`);
  unique(nodeOrder, `${path}.node_order`);
  for (let index = 0; index < nodeOrder.length; index += 1) {
    if (!knownNodeIds.has(nodeOrder[index])) {
      throw new EvidenceParseError(
        `${path}.node_order[${index}]`,
        'a node present in scenario.nodes',
      );
    }
  }

  const stages = array(strategy.route, `${path}.route`).map((stage, index) =>
    parseStage(stage, `${path}.route[${index}]`, ringId, numLayers, knownNodeIds),
  );
  if (stages.length === 0) {
    throw new EvidenceParseError(`${path}.route`, 'at least one successful route stage');
  }
  sameStringArrays(
    stages.map((stage) => stage.nodeId),
    nodeOrder,
    `${path}.node_order`,
  );

  let nextStart = 0;
  for (let index = 0; index < stages.length; index += 1) {
    const stage = stages[index];
    if (stage.startLayer !== nextStart) {
      throw new EvidenceParseError(
        `${path}.route[${index}].layers`,
        `a contiguous range starting at layer ${nextStart}`,
      );
    }
    nextStart = stage.endLayerExclusive;
  }
  if (nextStart !== numLayers) {
    throw new EvidenceParseError(`${path}.route`, `complete coverage of all ${numLayers} layers`);
  }

  return {
    id,
    simulatorStrategy: string(strategy.strategy, `${path}.strategy`),
    ringId,
    pathClass: string(strategy.path_class, `${path}.path_class`),
    pathPriority: nonNegativeInteger(strategy.path_priority, `${path}.path_priority`),
    nodeOrder,
    stages,
    metrics: {
      combinedTokensPerSecond: synthetic(
        nonNegativeNumber(
          strategy.estimated_combined_tokens_s,
          `${path}.estimated_combined_tokens_s`,
        ),
      ),
      decodeTokensPerSecond: synthetic(
        nonNegativeNumber(strategy.estimated_decode_tokens_s, `${path}.estimated_decode_tokens_s`),
      ),
      prefillTokensPerSecond: synthetic(
        nonNegativeNumber(
          strategy.estimated_prefill_tokens_s,
          `${path}.estimated_prefill_tokens_s`,
        ),
      ),
      singleRequestTokensPerSecond: synthetic(
        nonNegativeNumber(
          strategy.estimated_single_request_tokens_s,
          `${path}.estimated_single_request_tokens_s`,
        ),
      ),
      decodeLatencyMsPerToken: synthetic(
        nonNegativeNumber(
          strategy.estimated_decode_latency_ms_per_token,
          `${path}.estimated_decode_latency_ms_per_token`,
        ),
      ),
      prefillLatencyMs: synthetic(
        nonNegativeNumber(
          strategy.estimated_prefill_latency_ms,
          `${path}.estimated_prefill_latency_ms`,
        ),
      ),
      networkWorkloadCostMs: synthetic(
        nonNegativeNumber(
          strategy.network_workload_cost_ms,
          `${path}.network_workload_cost_ms`,
        ),
      ),
    },
    provenance: 'synthetic',
  };
}

/**
 * Converts bundled simulator JSON into the browser-owned evidence contract.
 *
 * Inputs are deliberately `unknown`: JSON imports and future fixture loaders must
 * cross this runtime validation boundary before the UI can render their claims.
 * The adapter is pure and uses no filesystem, network, or Node-only APIs.
 */
export function adaptSimulator(
  scenarioInput: unknown,
  simulationInput: unknown,
  geographyInput: unknown,
  manifestInput: unknown,
): EvidenceSnapshot {
  const scenario = record(scenarioInput, 'scenario');
  const simulation = record(simulationInput, 'simulation');
  const geography = record(geographyInput, 'geography');
  const manifest = record(manifestInput, 'manifest');

  const reportProtocol = oneOf(
    simulation.protocol,
    [SIMULATOR_PROTOCOL] as const,
    'simulation.protocol',
  );
  const geographyProtocol = oneOf(
    geography.protocol,
    [GEO_PROTOCOL] as const,
    'geography.protocol',
  );
  oneOf(geography.provenance, ['synthetic'] as const, 'geography.provenance');
  const geographyClaimBoundary = string(
    geography.claim_boundary,
    'geography.claim_boundary',
  );
  const geoNodes = record(geography.nodes, 'geography.nodes');

  const manifestProtocol = oneOf(
    manifest.protocol,
    [FIXTURE_MANIFEST_PROTOCOL] as const,
    'manifest.protocol',
  );
  oneOf(manifest.evidence_state, ['offline'] as const, 'manifest.evidence_state');
  oneOf(manifest.provenance, ['synthetic'] as const, 'manifest.provenance');
  const sourceClaimBoundary = string(manifest.claim_boundary, 'manifest.claim_boundary');
  const manifestSimulator = record(manifest.simulator, 'manifest.simulator');

  const scenarioName = string(scenario.name, 'scenario.name');
  if (string(simulation.scenario, 'simulation.scenario') !== scenarioName) {
    throw new EvidenceParseError('simulation.scenario', `the scenario name ${scenarioName}`);
  }
  if (
    string(manifestSimulator.scenario_name, 'manifest.simulator.scenario_name') !== scenarioName
  ) {
    throw new EvidenceParseError(
      'manifest.simulator.scenario_name',
      `the scenario name ${scenarioName}`,
    );
  }
  if (
    string(manifestSimulator.report_protocol, 'manifest.simulator.report_protocol') !==
    reportProtocol
  ) {
    throw new EvidenceParseError(
      'manifest.simulator.report_protocol',
      `the report protocol ${reportProtocol}`,
    );
  }

  const fixtureFiles = stringArray(manifestSimulator.files, 'manifest.simulator.files');
  unique(fixtureFiles, 'manifest.simulator.files');
  if (
    fixtureFiles.length !== FIXTURE_FILES.length ||
    FIXTURE_FILES.some((fileName) => !fixtureFiles.includes(fileName))
  ) {
    throw new EvidenceParseError(
      'manifest.simulator.files',
      `exactly the fixture files ${JSON.stringify(FIXTURE_FILES)}`,
    );
  }

  const scenarioModel = parseModelDimensions(scenario.model, 'scenario.model');
  const simulationModel = parseModelDimensions(simulation.model, 'simulation.model');
  assertMatchingModel(scenarioModel, simulationModel);
  const scenarioWorkload = parseWorkload(scenario.workload, 'scenario.workload');
  const simulationWorkload = parseWorkload(simulation.workload, 'simulation.workload');
  assertMatchingWorkload(scenarioWorkload, simulationWorkload);

  const nodes = array(scenario.nodes, 'scenario.nodes').map((node, index) =>
    parseNode(node, index, geoNodes),
  );
  if (nodes.length === 0) {
    throw new EvidenceParseError('scenario.nodes', 'at least one node');
  }
  const nodeIds = nodes.map((node) => node.id);
  unique(nodeIds, 'scenario.nodes[].node_id');
  const knownNodeIds = new Set(nodeIds);

  const links = array(scenario.links, 'scenario.links').map((link, index) =>
    parseLink(link, index, knownNodeIds),
  );
  unique(
    links.map((link) => link.id),
    'scenario.links',
  );

  const strategies = record(simulation.strategies, 'simulation.strategies');
  const routes = Object.entries(strategies)
    .map(([id, strategy]) =>
      parseRoute(id, strategy, scenarioModel.numLayers, knownNodeIds),
    )
    .filter((route): route is EvidenceRoute => route !== null);
  if (routes.length === 0) {
    throw new EvidenceParseError('simulation.strategies', 'at least one successful strategy');
  }

  return deepFreeze({
    protocol: EVIDENCE_SNAPSHOT_PROTOCOL,
    evidenceState: 'offline',
    provenance: 'synthetic',
    claimBoundary: offlineClaimBoundary(sourceClaimBoundary),
    sourceClaimBoundary,
    geographyClaimBoundary,
    source: {
      kind: 'simulator_fixture',
      manifestProtocol,
      reportProtocol,
      geographyProtocol,
      scenarioName,
      fixtureFiles,
      generatedAt: dateTimeString(simulation.timestamp, 'simulation.timestamp'),
    },
    model: scenarioModel,
    workload: scenarioWorkload,
    nodes,
    links,
    routes,
  });
}

export type {
  EvidenceLink,
  EvidenceNode,
  EvidenceRoute,
  EvidenceRouteStage,
  EvidenceSnapshot,
} from './types';
