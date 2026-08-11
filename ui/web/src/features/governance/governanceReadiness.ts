export const GOVERNANCE_READINESS_PATH = '/__mycelium/governance-readiness';

export interface GovernanceReadiness {
  readonly protocol: 'mycelium.governance_readiness.v1';
  readonly observed_at_unix_ms: number;
  readonly source_kind: 'source_control';
  readonly source_commit: string | null;
  readonly source_worktree_clean: boolean;
  readonly ledger_protocol: 'mycelium.governance_ledger.v1';
  readonly ledger_digest: string;
  readonly contract_manifest_protocol: 'mycelium.contract_manifest.v1';
  readonly contract_manifest_digest: string;
  readonly governance_gate_protocol: 'mycelium.governance_gate.v1';
  readonly governance_gate_ok: boolean;
  readonly authorized_product_action_count: number;
  readonly capability_count: number;
  readonly milestone_count: number;
  readonly release_exclusions: readonly string[];
  readonly release_ready: false;
}

const SHA256 = /^sha256:[0-9a-f]{64}$/;
const COMMIT = /^[0-9a-f]{40}$/;

export function decodeGovernanceReadiness(value: unknown): GovernanceReadiness {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new TypeError('governance readiness is invalid');
  const item = value as Record<string, unknown>;
  const fields = ['protocol', 'observed_at_unix_ms', 'source_kind', 'source_commit', 'source_worktree_clean', 'ledger_protocol', 'ledger_digest', 'contract_manifest_protocol', 'contract_manifest_digest', 'governance_gate_protocol', 'governance_gate_ok', 'authorized_product_action_count', 'capability_count', 'milestone_count', 'release_exclusions', 'release_ready'];
  if (Object.keys(item).sort().join(',') !== fields.sort().join(',')) throw new TypeError('governance readiness shape is invalid');
  if (
    item.protocol !== 'mycelium.governance_readiness.v1'
    || item.source_kind !== 'source_control'
    || item.ledger_protocol !== 'mycelium.governance_ledger.v1'
    || item.contract_manifest_protocol !== 'mycelium.contract_manifest.v1'
    || item.governance_gate_protocol !== 'mycelium.governance_gate.v1'
    || item.release_ready !== false
    || typeof item.source_worktree_clean !== 'boolean'
    || typeof item.governance_gate_ok !== 'boolean'
    || !Number.isSafeInteger(item.observed_at_unix_ms)
    || !Number.isSafeInteger(item.authorized_product_action_count)
    || !Number.isSafeInteger(item.capability_count)
    || !Number.isSafeInteger(item.milestone_count)
    || !SHA256.test(String(item.ledger_digest))
    || !SHA256.test(String(item.contract_manifest_digest))
    || (item.source_commit !== null && !COMMIT.test(String(item.source_commit)))
    || !Array.isArray(item.release_exclusions)
    || item.release_exclusions.length === 0
    || item.release_exclusions.some((entry) => typeof entry !== 'string' || entry.length === 0 || entry.length > 512)
  ) throw new TypeError('governance readiness contract is invalid');
  return Object.freeze({ ...item, release_exclusions: Object.freeze([...item.release_exclusions]) }) as unknown as GovernanceReadiness;
}

export interface GovernanceReadinessClient {
  load(signal?: AbortSignal): Promise<GovernanceReadiness>;
}

export class HttpGovernanceReadinessClient implements GovernanceReadinessClient {
  async load(signal?: AbortSignal): Promise<GovernanceReadiness> {
    const response = await fetch(GOVERNANCE_READINESS_PATH, { signal, cache: 'no-store', credentials: 'same-origin', redirect: 'error', referrerPolicy: 'no-referrer', headers: { accept: 'application/json' } });
    if (!response.ok) throw new Error(`governance_readiness_${response.status}`);
    if (!(response.headers.get('content-type') ?? '').toLowerCase().startsWith('application/json')) throw new Error('governance_readiness_content_type_invalid');
    return decodeGovernanceReadiness(await response.json());
  }
}
