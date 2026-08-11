import type { M22ReleaseEvidence } from './m22Release';

const digest = `sha256:${'a'.repeat(64)}`;
export const m22ReleaseFixture: M22ReleaseEvidence = Object.freeze({
  protocol: 'mycelium.m22_release_closure.v1', generated_at_unix_ms: 1_786_420_000_000,
  source: Object.freeze({ revision: 'm22-release', contract_manifest_digest: digest, sbom_digest: digest, clean_bootstrap: true }),
  ui_audit: Object.freeze({ protocol: 'mycelium.m22_ui_audit.v1', requirement_count: 20, verified_count: 20, excluded_count: 0, audit_digest: digest }),
  services: Object.freeze({ package_count: 3, roles: Object.freeze(['seed','node','supervisor']), platform_classes: Object.freeze(['launchd','systemd']), continuous_renewal: true, bounded_restart: true, foreground_route_restart_verified: true, restart_verified: true, coordinator_restart_verified: true, managed_restart_evidence_digest: `sha256:${'e'.repeat(64)}`, log_rotation: true, graceful_drain: true }),
  physical: Object.freeze({ simulated: false, participant_count: 3, runtime_class_count: 2, activation_transport: 'endpointid_authenticated_iroh', tailscale_product_dependency: false, frame_count_before: 10, frame_count_after: 20, output_token_count: 4, request_completed: true }),
  model: Object.freeze({ model_id: 'Qwen/Qwen2.5-3B-Instruct', revision: 'aa8e72537993ba99e69dfaafa59ed015b17504d1', parameter_class: '3B', weight_bytes: 6_171_927_653, architecture_adapter: 'qwen2', local_cache_reused: true, network_download_performed: false, qualified: true, reason: 'physical_usefulness_gate_passed' }),
  qwen3_8b: Object.freeze({ model_id: 'Qwen/Qwen3-8B', revision: 'b968826d9c46dd6066d109eabc6255188de91218', adapter_id: 'qwen3', local_snapshot_complete: true, adapter_verified: true, qualified: false, reason: 'insufficient_swarm_memory_and_disk' }),
  tests: Object.freeze({ python_passed: 3725, python_skipped: 13, ui_passed: 433, rust_passed: 20, browser_engines: Object.freeze(['chromium','firefox','webkit']), production_build: true, accessibility: true, performance: true, privacy: true, security: true, claim_boundary: true }),
  reviewer: Object.freeze({ bundle_version: 'astras-macbook-m22-1', preflight_idempotent: true, surrogate_verified: true, external_network: true, assigned_stage: true, inference_completed: true, negative_case_verified: true }),
  gate_state: 'qualified', exclusions: Object.freeze([]), privacy: 'no credentials, prompts, output, token arrays, tensors, activations, kv, raw endpoint ids, private addresses, paths, or usernames', evidence_digest: digest,
});
