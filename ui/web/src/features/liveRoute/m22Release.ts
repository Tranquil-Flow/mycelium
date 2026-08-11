export const M22_RELEASE_PATH = '/__mycelium/m22-release';

export type M22ReleaseEvidence = Readonly<{
  protocol: 'mycelium.m22_release_closure.v1';
  generated_at_unix_ms: number;
  source: Readonly<{ revision: string; contract_manifest_digest: string; sbom_digest: string; clean_bootstrap: boolean }>;
  ui_audit: Readonly<{ protocol: 'mycelium.m22_ui_audit.v1'; requirement_count: number; verified_count: number; excluded_count: number; audit_digest: string }>;
  services: Readonly<{ package_count: number; roles: readonly string[]; platform_classes: readonly string[]; continuous_renewal: boolean; bounded_restart: boolean; foreground_route_restart_verified: boolean; restart_verified: boolean; coordinator_restart_verified: boolean; managed_restart_evidence_digest: string; log_rotation: boolean; graceful_drain: boolean }>;
  physical: Readonly<{ simulated: boolean; participant_count: number; runtime_class_count: number; activation_transport: string; tailscale_product_dependency: boolean; frame_count_before: number; frame_count_after: number; output_token_count: number; request_completed: boolean }>;
  model: Readonly<{ model_id: string; revision: string; parameter_class: string; weight_bytes: number; architecture_adapter: string; local_cache_reused: boolean; network_download_performed: boolean; qualified: boolean; reason: string }>;
  qwen3_8b: Readonly<{ model_id: string; revision: string; adapter_id: string; local_snapshot_complete: boolean; adapter_verified: boolean; qualified: boolean; reason: string }>;
  tests: Readonly<{ python_passed: number; python_skipped: number; ui_passed: number; rust_passed: number; browser_engines: readonly string[]; production_build: boolean; accessibility: boolean; performance: boolean; privacy: boolean; security: boolean; claim_boundary: boolean }>;
  reviewer: Readonly<{ bundle_version: string; preflight_idempotent: boolean; surrogate_verified: boolean; external_network: boolean; assigned_stage: boolean; inference_completed: boolean; negative_case_verified: boolean }>;
  gate_state: 'qualified' | 'withheld'; exclusions: readonly string[]; privacy: string; evidence_digest: string;
}>;

function exact(value: unknown, fields: readonly string[], label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new TypeError(`${label} must be an object`);
  const source = value as Record<string, unknown>;
  if (Object.keys(source).sort().join('\0') !== [...fields].sort().join('\0')) throw new TypeError(`${label} has unknown or missing fields`);
  return source;
}
function text(value: unknown, label: string): string { if (typeof value !== 'string' || value.length === 0) throw new TypeError(`${label} is invalid`); return value; }
function sha(value: unknown, label: string): string { const result = text(value, label); if (!/^sha256:[0-9a-f]{64}$/.test(result)) throw new TypeError(`${label} is invalid`); return result; }
function integer(value: unknown, label: string): number { if (!Number.isSafeInteger(value) || Number(value) < 0) throw new TypeError(`${label} is invalid`); return Number(value); }
function bool(value: unknown, label: string): boolean { if (typeof value !== 'boolean') throw new TypeError(`${label} is invalid`); return value; }
function texts(value: unknown, label: string): readonly string[] { if (!Array.isArray(value)) throw new TypeError(`${label} is invalid`); return Object.freeze(value.map((item) => text(item, label))); }

export function decodeM22Release(value: unknown): M22ReleaseEvidence {
  const root = exact(value, ['protocol','generated_at_unix_ms','source','ui_audit','services','physical','model','qwen3_8b','tests','reviewer','gate_state','exclusions','privacy','evidence_digest'], 'm22 release');
  if (root.protocol !== 'mycelium.m22_release_closure.v1' || !['qualified','withheld'].includes(String(root.gate_state))) throw new TypeError('m22 release protocol or gate is invalid');
  const source = exact(root.source, ['revision','contract_manifest_digest','sbom_digest','clean_bootstrap'], 'm22 source');
  const ui = exact(root.ui_audit, ['protocol','requirement_count','verified_count','excluded_count','audit_digest'], 'm22 ui audit');
  if (ui.protocol !== 'mycelium.m22_ui_audit.v1') throw new TypeError('m22 ui protocol is invalid');
  const services = exact(root.services, ['package_count','roles','platform_classes','continuous_renewal','bounded_restart','foreground_route_restart_verified','restart_verified','coordinator_restart_verified','managed_restart_evidence_digest','log_rotation','graceful_drain'], 'm22 services');
  const physical = exact(root.physical, ['simulated','participant_count','runtime_class_count','activation_transport','tailscale_product_dependency','frame_count_before','frame_count_after','output_token_count','request_completed'], 'm22 physical');
  const model = exact(root.model, ['model_id','revision','parameter_class','weight_bytes','architecture_adapter','local_cache_reused','network_download_performed','qualified','reason'], 'm22 model');
  const qwen3 = exact(root.qwen3_8b, ['model_id','revision','adapter_id','local_snapshot_complete','adapter_verified','qualified','reason'], 'm22 qwen3');
  const tests = exact(root.tests, ['python_passed','python_skipped','ui_passed','rust_passed','browser_engines','production_build','accessibility','performance','privacy','security','claim_boundary'], 'm22 tests');
  const reviewer = exact(root.reviewer, ['bundle_version','preflight_idempotent','surrogate_verified','external_network','assigned_stage','inference_completed','negative_case_verified'], 'm22 reviewer');
  return Object.freeze({
    protocol: 'mycelium.m22_release_closure.v1', generated_at_unix_ms: integer(root.generated_at_unix_ms, 'generated'),
    source: Object.freeze({ revision: text(source.revision,'revision'), contract_manifest_digest: text(source.contract_manifest_digest,'contract digest'), sbom_digest: text(source.sbom_digest,'sbom digest'), clean_bootstrap: bool(source.clean_bootstrap,'clean bootstrap') }),
    ui_audit: Object.freeze({ protocol: 'mycelium.m22_ui_audit.v1', requirement_count: integer(ui.requirement_count,'requirement count'), verified_count: integer(ui.verified_count,'verified count'), excluded_count: integer(ui.excluded_count,'excluded count'), audit_digest: text(ui.audit_digest,'audit digest') }),
    services: Object.freeze({ package_count: integer(services.package_count,'package count'), roles: texts(services.roles,'roles'), platform_classes: texts(services.platform_classes,'platform classes'), continuous_renewal: bool(services.continuous_renewal,'continuous renewal'), bounded_restart: bool(services.bounded_restart,'bounded restart'), foreground_route_restart_verified: bool(services.foreground_route_restart_verified,'foreground route restart'), restart_verified: bool(services.restart_verified,'restart verified'), coordinator_restart_verified: bool(services.coordinator_restart_verified,'coordinator restart'), managed_restart_evidence_digest: sha(services.managed_restart_evidence_digest,'managed restart evidence digest'), log_rotation: bool(services.log_rotation,'log rotation'), graceful_drain: bool(services.graceful_drain,'graceful drain') }),
    physical: Object.freeze({ simulated: bool(physical.simulated,'simulated'), participant_count: integer(physical.participant_count,'participants'), runtime_class_count: integer(physical.runtime_class_count,'runtimes'), activation_transport: text(physical.activation_transport,'transport'), tailscale_product_dependency: bool(physical.tailscale_product_dependency,'tailscale'), frame_count_before: integer(physical.frame_count_before,'frames before'), frame_count_after: integer(physical.frame_count_after,'frames after'), output_token_count: integer(physical.output_token_count,'output tokens'), request_completed: bool(physical.request_completed,'request completed') }),
    model: Object.freeze({ model_id: text(model.model_id,'model id'), revision: text(model.revision,'model revision'), parameter_class: text(model.parameter_class,'parameter class'), weight_bytes: integer(model.weight_bytes,'weight bytes'), architecture_adapter: text(model.architecture_adapter,'adapter'), local_cache_reused: bool(model.local_cache_reused,'cache reuse'), network_download_performed: bool(model.network_download_performed,'download'), qualified: bool(model.qualified,'model qualified'), reason: text(model.reason,'model reason') }),
    qwen3_8b: Object.freeze({ model_id: text(qwen3.model_id,'qwen3 model'), revision: text(qwen3.revision,'qwen3 revision'), adapter_id: text(qwen3.adapter_id,'qwen3 adapter'), local_snapshot_complete: bool(qwen3.local_snapshot_complete,'qwen3 snapshot'), adapter_verified: bool(qwen3.adapter_verified,'qwen3 verified'), qualified: bool(qwen3.qualified,'qwen3 qualified'), reason: text(qwen3.reason,'qwen3 reason') }),
    tests: Object.freeze({ python_passed: integer(tests.python_passed,'python passed'), python_skipped: integer(tests.python_skipped,'python skipped'), ui_passed: integer(tests.ui_passed,'ui passed'), rust_passed: integer(tests.rust_passed,'rust passed'), browser_engines: texts(tests.browser_engines,'browser engines'), production_build: bool(tests.production_build,'build'), accessibility: bool(tests.accessibility,'accessibility'), performance: bool(tests.performance,'performance'), privacy: bool(tests.privacy,'privacy'), security: bool(tests.security,'security'), claim_boundary: bool(tests.claim_boundary,'claim boundary') }),
    reviewer: Object.freeze({ bundle_version: text(reviewer.bundle_version,'reviewer bundle'), preflight_idempotent: bool(reviewer.preflight_idempotent,'preflight'), surrogate_verified: bool(reviewer.surrogate_verified,'surrogate'), external_network: bool(reviewer.external_network,'external network'), assigned_stage: bool(reviewer.assigned_stage,'assigned stage'), inference_completed: bool(reviewer.inference_completed,'inference completed'), negative_case_verified: bool(reviewer.negative_case_verified,'negative case') }),
    gate_state: root.gate_state as M22ReleaseEvidence['gate_state'], exclusions: texts(root.exclusions,'exclusions'), privacy: text(root.privacy,'privacy'), evidence_digest: text(root.evidence_digest,'evidence digest'),
  });
}

export interface M22ReleaseClient { load(signal?: AbortSignal): Promise<M22ReleaseEvidence> }
export class HttpM22ReleaseClient implements M22ReleaseClient {
  async load(signal?: AbortSignal): Promise<M22ReleaseEvidence> {
    const response = await fetch(M22_RELEASE_PATH, { method: 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' }, signal });
    if (!response.ok) throw new Error(`m22_release_${response.status}`);
    return decodeM22Release(await response.json());
  }
}
