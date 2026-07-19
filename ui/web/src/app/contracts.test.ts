import Ajv2020, { type AnySchema, type ValidateFunction } from 'ajv/dist/2020';
import { describe, expect, it } from 'vitest';
import bootstrapSchema from '../../../contracts/product/product-ui-bootstrap-v1.schema.json';
import inferenceSchema from '../../../contracts/product/product-ui-inference-v1.schema.json';
import observatorySchema from '../../../contracts/product/product-ui-observatory-v1.schema.json';
import swarmSchema from '../../../contracts/product/product-ui-swarm-v1.schema.json';
import {
  MAX_PROMPT_UTF8_BYTES,
  PRODUCT_API_PATHS,
  PRODUCT_BOOTSTRAP_PROTOCOL,
  PRODUCT_ERROR_PROTOCOL,
  PRODUCT_INFERENCE_PROTOCOL,
  PRODUCT_OBSERVATORY_PROTOCOL,
  PRODUCT_QUALIFIER_AUTHORITY,
  PRODUCT_SWARM_PROTOCOL,
  ProductContractError,
  buildInferenceSubmission,
  decodeInferenceAccepted,
  decodeInferenceCancelResponse,
  decodeInferenceEvent,
  decodeProductBootstrap,
  decodeProductError,
  decodeProductObservatory,
  decodeProductQualification,
  decodeProductSwarmStatus,
  inferenceBlockReason,
  sourceTruthLabel,
} from './contracts';

const digest = `sha256:${'a'.repeat(64)}`;
const requestId = 'request-1';

const binding = {
  qualification_id: 'qualification-1',
  qualification_digest: digest,
  deployment_id: 'deployment-1',
  deployment_epoch: 3,
  topology_version: 8,
  model_id: 'model-1',
  resolved_commit: 'commit-1',
  manifest_digest: digest,
  path_manifest_digest: digest,
  stage_load_proof_digests: [digest],
};

const bootstrap = {
  protocol: PRODUCT_BOOTSTRAP_PROTOCOL,
  source_mode: 'fixture',
  session: {
    csrf_header: 'X-Mycelium-CSRF',
    csrf_token: 'local-product-session-token',
    expires_at_unix_ms: 1_900_000_000_000,
  },
  api: PRODUCT_API_PATHS,
  limits: {
    max_prompt_utf8_bytes: MAX_PROMPT_UTF8_BYTES,
    max_new_tokens: 4_096,
  },
  qualification_authority: PRODUCT_QUALIFIER_AUTHORITY,
};

const rejectedQualification = {
  protocol: PRODUCT_INFERENCE_PROTOCOL,
  issued_at_unix_ms: 1_800_000_000_000,
  evidence_class: 'synthetic_test_fixture',
  route_ready: false,
  reason_codes: ['physical_qualification_missing'],
  binding,
};

const acceptedQualification = {
  ...rejectedQualification,
  evidence_class: 'physical_qualification',
  route_ready: true,
  reason_codes: [],
};

const observatory = {
  protocol: PRODUCT_OBSERVATORY_PROTOCOL,
  generation: 7,
  status: 'connected',
  source: {
    mode: 'replay',
    freshness: 'replay',
    observed_at_unix_ms: null,
    replay_of_generation: 4,
  },
  metrics: {
    native_node_count: null,
    browser_worker_count: null,
    incident_count: null,
  },
};

const swarm = {
  protocol: PRODUCT_SWARM_PROTOCOL,
  native_nodes: [
    {
      member_id: 'member-1',
      capability: 'native_inference_node',
      membership_state: 'reachable',
      connectivity: 'relayed',
      endpoint_id: 'endpoint-1',
    },
  ],
  browser_workers: [
    {
      peer_id: 'peer-1',
      capability: 'synthetic_browser_probe',
      state: 'ready',
      expires_at_unix_ms: 1_900_000_000_000,
    },
  ],
};

function createSchemaValidator() {
  const ajv = new Ajv2020({ allErrors: true, strict: true });
  ajv.addKeyword({
    keyword: 'maxUtf8Bytes',
    schemaType: 'number',
    type: 'string',
    validate: (limit: number, value: string) => new TextEncoder().encode(value).byteLength <= limit,
  });
  for (const schema of [bootstrapSchema, observatorySchema, inferenceSchema, swarmSchema]) {
    ajv.addSchema(schema as AnySchema);
  }
  return ajv;
}

const ajv = createSchemaValidator();

function schemaValidator(schema: { readonly $id: string }): ValidateFunction {
  const validator = ajv.getSchema(schema.$id);
  if (validator === undefined) throw new Error(`missing schema ${schema.$id}`);
  return validator;
}

function definitionValidator(
  schema: { readonly $id: string },
  definition: string,
): ValidateFunction {
  const validator = ajv.getSchema(`${schema.$id}#/$defs/${definition}`);
  if (validator === undefined) throw new Error(`missing definition ${definition}`);
  return validator;
}

describe('frozen product UI wire contracts', () => {
  it('executes exact draft-2020-12 schemas, not metadata-only checks', () => {
    for (const schema of [bootstrapSchema, observatorySchema, inferenceSchema, swarmSchema]) {
      expect(schema.$schema).toBe('https://json-schema.org/draft/2020-12/schema');
      expect(schema.additionalProperties).toBe(false);
      expect(schema.$id).toMatch(/^https:\/\/mycelium\.local\/schemas\/product-ui-/);
      expect(schemaValidator(schema)(
        schema === bootstrapSchema
          ? bootstrap
          : schema === observatorySchema
            ? observatory
            : schema === inferenceSchema
              ? {
                  protocol: PRODUCT_INFERENCE_PROTOCOL,
                  prompt: 'moon',
                  max_new_tokens: 16,
                  qualification: binding,
                }
              : swarm,
      )).toBe(true);
    }
  });

  it('freezes same-origin endpoint paths and exposes no upstream credential', () => {
    expect(decodeProductBootstrap(bootstrap)).toEqual(bootstrap);
    for (const path of Object.values(PRODUCT_API_PATHS)) {
      expect(path).toMatch(/^\/api\/v1\//);
      expect(path).not.toContain('://');
    }
    expect(JSON.stringify(bootstrap).toLowerCase()).not.toContain('bearer');
    expect(() => decodeProductBootstrap({ ...bootstrap, route_ready: false })).toThrow(
      ProductContractError,
    );
    expect(schemaValidator(bootstrapSchema)({ ...bootstrap, route_ready: false })).toBe(false);
  });

  it('keeps readiness out of non-authoritative observatory and swarm payloads', () => {
    const decodedObservatory = decodeProductObservatory(observatory);
    const decodedSwarm = decodeProductSwarmStatus(swarm);
    expect(JSON.stringify(decodedObservatory)).not.toContain('route_ready');
    expect(JSON.stringify(decodedSwarm)).not.toContain('route_ready');
    expect(decodedObservatory.metrics.native_node_count).toBeNull();
    expect(decodedSwarm.native_nodes[0]).not.toHaveProperty('private_address');
    expect(() =>
      decodeProductObservatory({ ...observatory, route_ready: false }),
    ).toThrow(ProductContractError);
    expect(() =>
      decodeProductSwarmStatus({ ...swarm, route_ready: false }),
    ).toThrow(ProductContractError);
  });

  it.each([
    ['fixture', 'Fixture data'],
    ['live', 'Live evidence'],
    ['replay', 'Replay evidence'],
  ] as const)('keeps %s truth label explicit', (mode, label) => {
    expect(sourceTruthLabel(mode)).toBe(label);
  });

  it('matches exact Python QualificationBinding field names and safe integer bounds', () => {
    const decoded = decodeProductQualification(rejectedQualification);
    expect(Object.keys(decoded.binding)).toEqual([
      'qualification_id',
      'qualification_digest',
      'deployment_id',
      'deployment_epoch',
      'topology_version',
      'model_id',
      'resolved_commit',
      'manifest_digest',
      'path_manifest_digest',
      'stage_load_proof_digests',
    ]);
    expect(inferenceBlockReason(decoded, 1_800_000_000_001)).toBe(
      'Route is not ready: physical_qualification_missing',
    );
    expect(() =>
      decodeProductQualification({
        ...rejectedQualification,
        binding: { ...binding, deployment_epoch: Number.MAX_SAFE_INTEGER + 1 },
      }),
    ).toThrow(ProductContractError);
    expect(
      definitionValidator(inferenceSchema, 'qualification_binding')({
        ...binding,
        deployment_epoch: Number.MAX_SAFE_INTEGER + 1,
      }),
    ).toBe(false);
  });

  it('rejects every semantically inconsistent readiness projection', () => {
    const qualificationValidator = definitionValidator(inferenceSchema, 'qualification_projection');
    const invalid = [
      { ...acceptedQualification, evidence_class: 'synthetic_test_fixture' },
      { ...acceptedQualification, reason_codes: ['not_accepted'] },
      {
        ...acceptedQualification,
        binding: { ...binding, stage_load_proof_digests: [] },
      },
      { ...rejectedQualification, reason_codes: [] },
    ];
    for (const candidate of invalid) {
      expect(() => decodeProductQualification(candidate)).toThrow(ProductContractError);
      expect(qualificationValidator(candidate)).toBe(false);
    }
    expect(qualificationValidator(acceptedQualification)).toBe(true);
    expect(decodeProductQualification(acceptedQualification).route_ready).toBe(true);
  });

  it('fails closed for stale and future qualification evidence', () => {
    const ready = decodeProductQualification(acceptedQualification);
    expect(inferenceBlockReason(ready, 1_800_300_000_001)).toBe('Qualification is stale');
    expect(inferenceBlockReason(ready, 1_799_999_999_999)).toBe(
      'Qualification timestamp is in the future',
    );
  });

  it('accepts bounded Python authority strings such as namespaced model IDs', () => {
    const candidate = {
      ...rejectedQualification,
      binding: {
        ...binding,
        model_id: 'org/model-name',
        deployment_id: 'cluster/segment/a',
      },
    };
    expect(decodeProductQualification(candidate).binding.model_id).toBe('org/model-name');
    expect(definitionValidator(inferenceSchema, 'qualification_projection')(candidate)).toBe(true);
  });

  it('enforces prompt size in UTF-8 bytes in schema and TypeScript', () => {
    const ready = decodeProductQualification(acceptedQualification);
    const oversized = '🌙'.repeat(MAX_PROMPT_UTF8_BYTES / 4 + 1);
    const submission = {
      protocol: PRODUCT_INFERENCE_PROTOCOL,
      prompt: oversized,
      max_new_tokens: 1,
      qualification: binding,
    };
    expect(schemaValidator(inferenceSchema)(submission)).toBe(false);
    expect(() =>
      buildInferenceSubmission(oversized, 1, ready, 1_800_000_000_001),
    ).toThrow(`Prompt exceeds ${MAX_PROMPT_UTF8_BYTES} UTF-8 bytes`);
  });

  it('uses exact discriminated event schemas matching Python StreamEvent', () => {
    const validateEvent = definitionValidator(inferenceSchema, 'stream_event');
    const tokenEvent = {
      protocol: 'mycelium.request_event.v1',
      request_id: requestId,
      sequence: 1,
      type: 'token',
      token_index: 0,
      text: 'moon',
    };
    expect(validateEvent(tokenEvent)).toBe(true);
    expect(decodeInferenceEvent(tokenEvent).type).toBe('token');
    const astralBoundary = {
      ...tokenEvent,
      text: '🌙'.repeat(65_536),
    };
    expect(validateEvent(astralBoundary)).toBe(true);
    expect(decodeInferenceEvent(astralBoundary).type).toBe('token');
    for (const invalid of [
      { ...tokenEvent, token_index: undefined },
      { ...tokenEvent, text: undefined },
      { ...tokenEvent, type: 'accepted', text: 'leak' },
      {
        protocol: 'mycelium.request_event.v1',
        request_id: requestId,
        sequence: 2,
        type: 'failed',
      },
    ]) {
      expect(validateEvent(invalid)).toBe(false);
      expect(() => decodeInferenceEvent(invalid)).toThrow(ProductContractError);
    }
  });

  it('validates accepted/cancel paths against the response request ID', () => {
    const accepted = decodeInferenceAccepted({
      protocol: PRODUCT_INFERENCE_PROTOCOL,
      request_id: requestId,
      accepted: true,
      event_path: `/api/v1/inference/${requestId}/events`,
      cancel_path: `/api/v1/inference/${requestId}/cancel`,
    });
    expect(accepted.accepted).toBe(true);
    expect(() =>
      decodeInferenceAccepted({
        ...accepted,
        event_path: '/api/v1/inference/different/events',
      }),
    ).toThrow(ProductContractError);
    expect(
      decodeInferenceCancelResponse({
        protocol: PRODUCT_INFERENCE_PROTOCOL,
        request_id: requestId,
        cancelled: true,
      }).cancelled,
    ).toBe(true);
  });

  it('preserves stable public error codes without exception strings', () => {
    const error = decodeProductError({
      protocol: PRODUCT_ERROR_PROTOCOL,
      code: 'route_not_ready',
      retryable: false,
    });
    expect(error.code).toBe('route_not_ready');
    expect(error).not.toHaveProperty('message');
    expect(() =>
      decodeProductError({
        ...error,
        message: 'Traceback: secret internal path',
      }),
    ).toThrow(ProductContractError);
  });

  it('freezes exact swarm invite/join/leave action definitions', () => {
    const createInvite = definitionValidator(swarmSchema, 'create_invite_request');
    const join = definitionValidator(swarmSchema, 'join_request');
    const leave = definitionValidator(swarmSchema, 'leave_request');
    expect(
      createInvite({
        protocol: PRODUCT_SWARM_PROTOCOL,
        action: 'create_invite',
        capability: 'native_inference_node',
        expires_in_seconds: 600,
      }),
    ).toBe(true);
    expect(
      join({
        protocol: PRODUCT_SWARM_PROTOCOL,
        action: 'join',
        invite_code: 'invite-code-1234',
        display_name: 'moon-node',
      }),
    ).toBe(true);
    expect(
      leave({
        protocol: PRODUCT_SWARM_PROTOCOL,
        action: 'leave',
        member_id: 'member-1',
      }),
    ).toBe(true);
  });
});
