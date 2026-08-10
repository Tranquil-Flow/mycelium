export interface QualifiedDeploymentSummary {
  readonly deployment_id: string;
  readonly model_id: string;
  readonly model_revision: string;
  readonly quantization: string;
  readonly topology_size: number;
  readonly health: 'qualified' | 'unavailable';
  readonly qualified_at_unix_ms: number;
  readonly qualification_id: string;
}

export interface DeploymentRegistryStatus {
  readonly protocol: 'mycelium.live_deployment_registry.v1';
  readonly selected_deployment_id: string;
  readonly switching_allowed: boolean;
  readonly deployments: readonly QualifiedDeploymentSummary[];
}

export interface DeploymentRegistryClient {
  status(signal?: AbortSignal): Promise<DeploymentRegistryStatus>;
  select(deploymentId: string, signal?: AbortSignal): Promise<DeploymentRegistryStatus>;
}

function decode(value: unknown): DeploymentRegistryStatus {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('invalid_registry');
  const document = value as Record<string, unknown>;
  if (
    Object.keys(document).sort().join(',') !== 'deployments,protocol,selected_deployment_id,switching_allowed' ||
    document.protocol !== 'mycelium.live_deployment_registry.v1' ||
    typeof document.selected_deployment_id !== 'string' ||
    typeof document.switching_allowed !== 'boolean' ||
    !Array.isArray(document.deployments)
  ) throw new Error('invalid_registry');
  const deployments = document.deployments.map((item) => {
    if (typeof item !== 'object' || item === null || Array.isArray(item)) throw new Error('invalid_registry');
    const record = item as Record<string, unknown>;
    if (
      Object.keys(record).sort().join(',') !== 'deployment_id,health,model_id,model_revision,qualification_id,qualified_at_unix_ms,quantization,topology_size' ||
      typeof record.deployment_id !== 'string' ||
      typeof record.model_id !== 'string' ||
      !/^[0-9a-f]{40}$/.test(String(record.model_revision)) ||
      typeof record.quantization !== 'string' ||
      !Number.isSafeInteger(record.topology_size) ||
      (record.health !== 'qualified' && record.health !== 'unavailable') ||
      !Number.isSafeInteger(record.qualified_at_unix_ms)
      || typeof record.qualification_id !== 'string'
    ) throw new Error('invalid_registry');
    return Object.freeze(record as unknown as QualifiedDeploymentSummary);
  });
  return Object.freeze({
    protocol: document.protocol,
    selected_deployment_id: document.selected_deployment_id,
    switching_allowed: document.switching_allowed,
    deployments: Object.freeze(deployments),
  });
}

export class HttpDeploymentRegistryClient implements DeploymentRegistryClient {
  async status(signal?: AbortSignal): Promise<DeploymentRegistryStatus> {
    return this.request('/__mycelium/deployments', { method: 'GET', signal });
  }

  async select(deploymentId: string, signal?: AbortSignal): Promise<DeploymentRegistryStatus> {
    return this.request('/__mycelium/deployments/select', {
      method: 'POST',
      signal,
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ deployment_id: deploymentId }),
    });
  }

  private async request(path: string, init: RequestInit): Promise<DeploymentRegistryStatus> {
    const response = await fetch(path, {
      ...init,
      cache: 'no-store',
      credentials: 'same-origin',
      redirect: 'error',
      referrerPolicy: 'no-referrer',
      headers: { accept: 'application/json', ...init.headers },
    });
    if (!response.ok) throw new Error(`deployment_registry_${response.status}`);
    const type = response.headers.get('content-type')?.toLowerCase() ?? '';
    if (!type.startsWith('application/json')) throw new Error('invalid_registry_content_type');
    return decode(await response.json());
  }
}
