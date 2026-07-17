export const EVIDENCE_SNAPSHOT_PROTOCOL = 'mycelium.ui_evidence_snapshot.v1' as const;

export type EvidenceProvenance =
  | 'measured'
  | 'declared'
  | 'estimated'
  | 'synthetic'
  | 'derived'
  | 'unknown';

export interface EvidenceValue<T> {
  readonly value: T;
  readonly provenance: EvidenceProvenance;
}

export interface KnownNodeLocation {
  readonly state: 'known';
  readonly provenance: 'synthetic';
  readonly city: string;
  readonly country: string;
  readonly latitude: number;
  readonly longitude: number;
  readonly precision: string;
}

export interface UnknownNodeLocation {
  readonly state: 'unknown';
  readonly provenance: 'unknown';
  readonly reason: 'not_provided' | 'source_explicitly_unknown';
}

export type NodeLocation = KnownNodeLocation | UnknownNodeLocation;

export interface NodeResources {
  readonly gpuTeraflops: number;
  readonly cpuTeraflops: number;
  readonly vramAvailableGb: number;
  readonly ramAvailableGb: number;
  readonly gpuMemoryBandwidthGbps: number;
  readonly ramBandwidthGbps: number;
  readonly vramRamBandwidthGbps: number;
  readonly unifiedMemory: boolean;
  readonly workspaceGb: number;
}

export interface EvidenceNode {
  readonly id: string;
  readonly resources: NodeResources;
  readonly location: NodeLocation;
  readonly provenance: 'synthetic';
}

export interface LinkMetrics {
  readonly roundTripTimeMs: EvidenceValue<number>;
  readonly jitterMs: EvidenceValue<number>;
  readonly bandwidthMbps: EvidenceValue<number>;
  readonly lossRatio: EvidenceValue<number>;
}

export interface EvidenceLink {
  readonly id: string;
  readonly source: string;
  readonly target: string;
  readonly bidirectional: boolean;
  readonly metrics: LinkMetrics;
  readonly provenance: 'synthetic';
}

export interface StageMemory {
  readonly weightsGb: number;
  readonly kvCacheGb: number;
  readonly kvCacheInRamGb: number;
  readonly ramUsedGb: number;
  readonly vramUsedGb: number;
  readonly residentLayerCount: number;
  readonly ramLayerCount: number;
  readonly ramExecution: string | null;
}

export interface StageMetrics {
  readonly decodeComputeMs: EvidenceValue<number>;
  readonly decodeOutgoingMs: EvidenceValue<number>;
  readonly prefillComputeMs: EvidenceValue<number>;
  readonly prefillOutgoingMs: EvidenceValue<number>;
}

export interface EvidenceRouteStage {
  readonly id: string;
  readonly nodeId: string;
  /** Inclusive simulator ranges are normalized to this half-open start. */
  readonly startLayer: number;
  /** Exclusive upper bound; safe for JavaScript slice/range semantics. */
  readonly endLayerExclusive: number;
  readonly layerCount: number;
  readonly pathClass: string;
  readonly pathPriority: number;
  readonly memory: StageMemory;
  readonly metrics: StageMetrics;
  readonly provenance: 'synthetic';
}

export interface RouteMetrics {
  readonly combinedTokensPerSecond: EvidenceValue<number>;
  readonly decodeTokensPerSecond: EvidenceValue<number>;
  readonly prefillTokensPerSecond: EvidenceValue<number>;
  readonly singleRequestTokensPerSecond: EvidenceValue<number>;
  readonly decodeLatencyMsPerToken: EvidenceValue<number>;
  readonly prefillLatencyMs: EvidenceValue<number>;
  readonly networkWorkloadCostMs: EvidenceValue<number>;
}

export interface EvidenceRoute {
  readonly id: string;
  readonly simulatorStrategy: string;
  readonly ringId: string;
  readonly pathClass: string;
  readonly pathPriority: number;
  readonly nodeOrder: readonly string[];
  readonly stages: readonly EvidenceRouteStage[];
  readonly metrics: RouteMetrics;
  readonly provenance: 'synthetic';
}

export interface EvidenceModel {
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

export interface EvidenceWorkload {
  readonly contextWindow: number;
  readonly concurrentRequests: number;
  readonly outputTokens: number;
  readonly contextFractionPerRequest: number;
  readonly kvSafetyMultiplier: number;
}

export interface EvidenceSnapshot {
  readonly protocol: typeof EVIDENCE_SNAPSHOT_PROTOCOL;
  readonly evidenceState: 'offline';
  readonly provenance: 'synthetic';
  readonly claimBoundary: string;
  /** The fixture-manifest boundary governing the complete capture. */
  readonly sourceClaimBoundary: string;
  /** The narrower synthetic-geography boundary, kept separate from the capture boundary. */
  readonly geographyClaimBoundary: string;
  readonly source: {
    readonly kind: 'simulator_fixture';
    readonly manifestProtocol: string;
    readonly reportProtocol: string;
    readonly geographyProtocol: string;
    readonly scenarioName: string;
    readonly fixtureFiles: readonly string[];
    readonly generatedAt: string;
  };
  readonly model: EvidenceModel;
  readonly workload: EvidenceWorkload;
  readonly nodes: readonly EvidenceNode[];
  readonly links: readonly EvidenceLink[];
  readonly routes: readonly EvidenceRoute[];
}

export type FailoverMode = 'stable_drain' | 'active_failover' | 'circuit_break';
export type FailoverStatus = 'resumed' | 'aborted';
export type FailoverRouteState = 'draining' | 'active' | 'failed' | 'aborted';
export type FailoverTransitionState =
  | 'DETECTED'
  | 'QUARANTINED_LOCAL'
  | 'ROUTE_AT_RISK'
  | 'REPLAN_STARTED'
  | 'CANDIDATE_SELECTED'
  | 'REPLACEMENT_LOADING'
  | 'CUTOVER_STARTED'
  | 'RESUMED'
  | 'ABORTED';

export interface FailoverValidationContext {
  readonly knownNodeIds: readonly string[];
  readonly numLayers: number;
}

export interface FailoverRoute {
  readonly id: string;
  readonly generation: number;
  readonly nodeIds: readonly string[];
  readonly state: FailoverRouteState;
}

export interface FailoverTrigger {
  readonly kind: string;
  readonly peerId: string;
  readonly scope: string;
  readonly detectedAt: string;
}

export interface FailoverCutover {
  readonly policy: string;
  readonly lastGoodLayer: number | null;
  readonly lastCommittedToken: number | null;
  readonly checkpointKind: string;
}

export interface FailoverTransition {
  readonly state: FailoverTransitionState;
  readonly atMs: number;
  readonly detail: string;
}

export interface FailoverIncident {
  readonly id: string;
  readonly title: string;
  readonly mode: FailoverMode;
  readonly status: FailoverStatus;
  readonly requestIds: readonly string[];
  readonly deploymentId: string;
  readonly deploymentEpoch: number;
  readonly oldRoute: FailoverRoute;
  readonly newRoute: FailoverRoute | null;
  readonly trigger: FailoverTrigger;
  readonly cutover: FailoverCutover;
  readonly backupReadiness: string;
  readonly compatibility: string;
  readonly transitions: readonly FailoverTransition[];
  readonly evidenceState: 'offline';
  readonly provenance: 'synthetic';
  readonly claimBoundary: string;
  readonly sourceClaimBoundary: string;
}

export interface FailoverOverlayRoute extends FailoverRoute {
  readonly role: 'old' | 'replacement';
  readonly label: string;
}

export interface FailoverOverlay {
  readonly incidentId: string;
  readonly title: string;
  readonly mode: FailoverMode;
  readonly routes: readonly FailoverOverlayRoute[];
  /** The peer named by the trigger; planned drains do not imply peer failure. */
  readonly triggerPeerId: string;
  readonly triggerKind: string;
  readonly triggerScope: string;
  /** @deprecated Use triggerPeerId; retained for existing renderers. */
  readonly failedPeerId: string;
  readonly checkpointLabel: string;
  readonly outcome: string;
  readonly evidenceState: 'offline';
  readonly provenance: 'synthetic';
  readonly claimBoundary: string;
}

export const PROVISIONING_EVIDENCE_PROTOCOL = 'mycelium.ui_provisioning_evidence.v1' as const;

export interface ProvisioningModel {
  readonly id: string;
  readonly numLayers: number;
  readonly manifestDigest: string;
  readonly resolvedCommit: string;
}

export interface ProvisioningAssignment {
  readonly nodeId: string;
  readonly startLayer: number;
  readonly endLayerExclusive: number;
  readonly layerCount: number;
}

export interface ProvisioningEvidence {
  readonly protocol: typeof PROVISIONING_EVIDENCE_PROTOCOL;
  readonly scope: 'artifact_provisioning';
  readonly model: ProvisioningModel;
  readonly nodeIds: readonly string[];
  readonly assignments: readonly ProvisioningAssignment[];
  readonly protocols: {
    readonly routePlan: 'mycelium.route_plan.v2';
    readonly provisioningAudit: 'mycelium.provisioning_audit.v1';
  };
  readonly auditedAt: string;
  readonly allAssignmentsVerified: boolean;
  readonly readyForRuntimeLoad: boolean;
  readonly routeReady: boolean;
  readonly errors: readonly string[];
  readonly evidenceState: 'offline';
  readonly provenance: EvidenceProvenance;
  readonly claimBoundary: string;
  readonly sourceClaimBoundaries: {
    readonly routePlan: string;
    readonly provisioningAudit: string;
  };
}
