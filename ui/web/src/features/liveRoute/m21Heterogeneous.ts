export type M21Member = Readonly<{
  member_id: string; peer_class: string; runtime_backend: string; trust_state: string;
  generation: number; incarnation: string; freshness: string; revocation_state: string;
  activation_eligible: boolean; route_participant: boolean; eligibility_reason: string;
  connectivity: 'direct' | 'relay' | 'unknown'; external_network: boolean;
  endpoint_identity_digest: string;
}>;
export type M21Path = Readonly<{
  source_member_id: string; destination_member_id: string; path_class: 'direct' | 'relay' | 'unknown';
  relay_region: string | null; cold_rtt_ms: number; warm_rtt_ms: number; jitter_ms: number;
  loss_ratio: number; goodput_bytes_per_second: number; reconnect_count: number;
  connection_generation: number; selected_path_changes: number; sample_count: number;
}>;
export type M21HeterogeneousEvidence = Readonly<{
  protocol: 'mycelium.m21_heterogeneous_swarm.v1'; generated_at_unix_ms: number;
  binding: Readonly<Record<string, string | number>>;
  policy: Readonly<Record<string, string | number | boolean>>;
  members: readonly M21Member[]; paths: readonly M21Path[];
  route: Readonly<{ physical: boolean; route_alive: boolean; heterogeneous: boolean;
    participant_count: number; runtime_class_count: number; frame_count_before: number;
    frame_count_after: number; latest_output_token_count: number;
    tailscale_product_dependency: boolean; activation_transport: string;
    operator_staging_transport: string }>;
  gate_state: 'qualified' | 'withheld'; exclusions: readonly string[];
  privacy: string; evidence_digest: string;
}>;

const memberFields = ['member_id', 'peer_class', 'runtime_backend', 'trust_state', 'generation', 'incarnation', 'freshness', 'revocation_state', 'activation_eligible', 'route_participant', 'eligibility_reason', 'connectivity', 'external_network', 'endpoint_identity_digest'] as const;
const pathFields = ['source_member_id', 'destination_member_id', 'path_class', 'relay_region', 'cold_rtt_ms', 'warm_rtt_ms', 'jitter_ms', 'loss_ratio', 'goodput_bytes_per_second', 'reconnect_count', 'connection_generation', 'selected_path_changes', 'sample_count'] as const;
const routeFields = ['physical', 'route_alive', 'heterogeneous', 'participant_count', 'runtime_class_count', 'frame_count_before', 'frame_count_after', 'latest_output_token_count', 'tailscale_product_dependency', 'activation_transport', 'operator_staging_transport'] as const;
const evidenceFields = ['protocol', 'generated_at_unix_ms', 'binding', 'policy', 'members', 'paths', 'route', 'gate_state', 'exclusions', 'privacy', 'evidence_digest'] as const;

function record(value: unknown, fields: readonly string[], label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new TypeError(`${label} must be an object`);
  const source = value as Record<string, unknown>;
  if (Object.keys(source).sort().join('\0') !== [...fields].sort().join('\0')) throw new TypeError(`${label} has unknown or missing fields`);
  return source;
}
function text(value: unknown, label: string): string { if (typeof value !== 'string' || value.length === 0) throw new TypeError(`${label} is invalid`); return value; }
function integer(value: unknown, label: string): number { if (!Number.isSafeInteger(value) || Number(value) < 0) throw new TypeError(`${label} is invalid`); return Number(value); }
function positive(value: unknown, label: string): number { if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw new TypeError(`${label} is invalid`); return value; }
function bool(value: unknown, label: string): boolean { if (typeof value !== 'boolean') throw new TypeError(`${label} is invalid`); return value; }

export function decodeM21Heterogeneous(value: unknown): M21HeterogeneousEvidence {
  const source = record(value, evidenceFields, 'm21 evidence');
  if (source.protocol !== 'mycelium.m21_heterogeneous_swarm.v1') throw new TypeError('m21 protocol is invalid');
  if (source.gate_state !== 'qualified' && source.gate_state !== 'withheld') throw new TypeError('m21 gate state is invalid');
  const binding = source.binding as Record<string, unknown>; const policy = source.policy as Record<string, unknown>;
  if (typeof binding !== 'object' || binding === null || Array.isArray(binding) || typeof policy !== 'object' || policy === null || Array.isArray(policy)) throw new TypeError('m21 authority is invalid');
  if (!Array.isArray(source.members) || !Array.isArray(source.paths) || !Array.isArray(source.exclusions)) throw new TypeError('m21 inventory is invalid');
  const members = source.members.map((value, index): M21Member => { const item = record(value, memberFields, `m21 member ${index}`); const connectivity = text(item.connectivity, 'connectivity'); if (!['direct', 'relay', 'unknown'].includes(connectivity)) throw new TypeError('m21 connectivity is invalid'); return Object.freeze({ member_id: text(item.member_id, 'member_id'), peer_class: text(item.peer_class, 'peer_class'), runtime_backend: text(item.runtime_backend, 'runtime_backend'), trust_state: text(item.trust_state, 'trust_state'), generation: integer(item.generation, 'generation'), incarnation: text(item.incarnation, 'incarnation'), freshness: text(item.freshness, 'freshness'), revocation_state: text(item.revocation_state, 'revocation_state'), activation_eligible: bool(item.activation_eligible, 'activation_eligible'), route_participant: bool(item.route_participant, 'route_participant'), eligibility_reason: text(item.eligibility_reason, 'eligibility_reason'), connectivity: connectivity as M21Member['connectivity'], external_network: bool(item.external_network, 'external_network'), endpoint_identity_digest: text(item.endpoint_identity_digest, 'endpoint_identity_digest') }); });
  const paths = source.paths.map((value, index): M21Path => { const item = record(value, pathFields, `m21 path ${index}`); const pathClass = text(item.path_class, 'path_class'); if (!['direct', 'relay', 'unknown'].includes(pathClass) || (item.relay_region !== null && typeof item.relay_region !== 'string')) throw new TypeError('m21 path is invalid'); return Object.freeze({ source_member_id: text(item.source_member_id, 'source_member_id'), destination_member_id: text(item.destination_member_id, 'destination_member_id'), path_class: pathClass as M21Path['path_class'], relay_region: item.relay_region as string | null, cold_rtt_ms: positive(item.cold_rtt_ms, 'cold_rtt_ms'), warm_rtt_ms: positive(item.warm_rtt_ms, 'warm_rtt_ms'), jitter_ms: positive(item.jitter_ms, 'jitter_ms'), loss_ratio: positive(item.loss_ratio, 'loss_ratio'), goodput_bytes_per_second: positive(item.goodput_bytes_per_second, 'goodput'), reconnect_count: integer(item.reconnect_count, 'reconnect_count'), connection_generation: integer(item.connection_generation, 'connection_generation'), selected_path_changes: integer(item.selected_path_changes, 'selected_path_changes'), sample_count: integer(item.sample_count, 'sample_count') }); });
  const route = record(source.route, routeFields, 'm21 route');
  return Object.freeze({ protocol: 'mycelium.m21_heterogeneous_swarm.v1', generated_at_unix_ms: integer(source.generated_at_unix_ms, 'generated_at_unix_ms'), binding: Object.freeze({ ...binding }) as M21HeterogeneousEvidence['binding'], policy: Object.freeze({ ...policy }) as M21HeterogeneousEvidence['policy'], members: Object.freeze(members), paths: Object.freeze(paths), route: Object.freeze({ physical: bool(route.physical, 'physical'), route_alive: bool(route.route_alive, 'route_alive'), heterogeneous: bool(route.heterogeneous, 'heterogeneous'), participant_count: integer(route.participant_count, 'participant_count'), runtime_class_count: integer(route.runtime_class_count, 'runtime_class_count'), frame_count_before: integer(route.frame_count_before, 'frame_count_before'), frame_count_after: integer(route.frame_count_after, 'frame_count_after'), latest_output_token_count: integer(route.latest_output_token_count, 'latest_output_token_count'), tailscale_product_dependency: bool(route.tailscale_product_dependency, 'tailscale_product_dependency'), activation_transport: text(route.activation_transport, 'activation_transport'), operator_staging_transport: text(route.operator_staging_transport, 'operator_staging_transport') }), gate_state: source.gate_state, exclusions: Object.freeze(source.exclusions.map((item) => text(item, 'exclusion'))), privacy: text(source.privacy, 'privacy'), evidence_digest: text(source.evidence_digest, 'evidence_digest') });
}
