import { describe, expect, it } from 'vitest';
import { validObservatoryAdapterEvent } from '../test/observatoryEventFixture';
import {
  OBSERVATORY_EVENT_PROJECTION_PROTOCOL,
  OBSERVATORY_EVENT_STATUS_PROTOCOL,
  UnsupportedObservatoryAdapterProtocolError,
  decodeObservatoryAdapterEvent,
  parseObservatoryAdapterEventHeader,
} from './observatoryEventProjection';

const CANARY = 'OBSERVATORY_UI_PRIVATE_CANARY_DO_NOT_RETAIN';

function clone<T>(value: T): T {
  return structuredClone(value);
}

describe('read-only Observatory event projection', () => {
  it('strictly decodes, copies, freezes, and preserves deterministic metadata order', () => {
    const input = validObservatoryAdapterEvent(7, 9);
    const decoded = decodeObservatoryAdapterEvent(input);
    input.bundle.snapshot.sessions[0].request_id = 'mutated';

    expect(decoded.generation).toBe(7);
    expect(decoded.bundle.snapshot.protocol).toBe(OBSERVATORY_EVENT_PROJECTION_PROTOCOL);
    expect(decoded.bundle.provisioning.protocol).toBe(OBSERVATORY_EVENT_STATUS_PROTOCOL);
    expect(decoded.bundle.provisioning.route_ready).toBe(false);
    expect(decoded.bundle.snapshot.sessions[0].request_id).toBe('request-a');
    expect(Object.isFrozen(decoded.bundle.snapshot.sessions[0])).toBe(true);
    expect(parseObservatoryAdapterEventHeader(input)).toEqual({
      protocol: 'mycelium.observatory_stream.v1',
      generation: 7,
    });
  });

  it.each([
    ['event', 'mycelium.observatory_stream.v2'],
    ['snapshot', 'mycelium.observatory.request_projection.v2'],
    ['status', 'mycelium.observatory.event_adapter_status.v2'],
  ])('fails closed on unknown %s protocol', (kind, protocol) => {
    const event = validObservatoryAdapterEvent();
    if (kind === 'event') event.protocol = protocol;
    else if (kind === 'snapshot') event.bundle.snapshot.protocol = protocol;
    else event.bundle.provisioning.protocol = protocol;

    expect(() => decodeObservatoryAdapterEvent(event)).toThrow(
      UnsupportedObservatoryAdapterProtocolError,
    );
  });

  it.each(['prompt', 'text', 'token', 'token_ids', 'credentials', 'activations', 'kv']) (
    'rejects unknown private field %s without echoing its value',
    (field) => {
      const event = validObservatoryAdapterEvent() as unknown as Record<string, unknown>;
      const bundle = (event.bundle as Record<string, unknown>);
      (bundle.snapshot as Record<string, unknown>)[field] = CANARY;
      let message = '';
      try {
        decodeObservatoryAdapterEvent(event);
      } catch (reason) {
        message = reason instanceof Error ? reason.message : String(reason);
      }
      expect(message).toMatch(/field|exact/i);
      expect(message).not.toContain(CANARY);
    },
  );

  it('rejects route promotion, cursor mismatch, endpoint identifiers, and unordered sessions', () => {
    const promoted = validObservatoryAdapterEvent();
    promoted.bundle.provisioning.route_ready = true;
    expect(() => decodeObservatoryAdapterEvent(promoted)).toThrow(/route_ready|false/i);

    const mismatch = validObservatoryAdapterEvent();
    mismatch.bundle.provisioning.source_cursor += 1;
    expect(() => decodeObservatoryAdapterEvent(mismatch)).toThrow(/cursor/i);

    const endpoint = validObservatoryAdapterEvent();
    endpoint.bundle.snapshot.qualification!.binding.deployment_id = 'https://private.invalid';
    expect(() => decodeObservatoryAdapterEvent(endpoint)).toThrow(/identifier|endpoint/i);

    const unordered = validObservatoryAdapterEvent();
    const second = clone(unordered.bundle.snapshot.sessions[0]);
    second.request_id = 'request-0';
    unordered.bundle.snapshot.sessions.push(second);
    unordered.bundle.provisioning.buffered_sessions = 2;
    expect(() => decodeObservatoryAdapterEvent(unordered)).toThrow(/order|unique/i);
  });

  it('rejects private endpoint and credential shapes from every retained identifier', () => {
    for (const privateValue of [
      'localhost:8080',
      'worker.private.internal',
      '127.0.0.1:9000',
      'sk-' + 'privateabcdefghijklmnop6789',
    ]) {
      const event = validObservatoryAdapterEvent();
      event.bundle.snapshot.qualification!.binding.deployment_id = privateValue;
      let message = '';
      try {
        decodeObservatoryAdapterEvent(event);
      } catch (reason) {
        message = reason instanceof Error ? reason.message : String(reason);
      }
      expect(message).toMatch(/identifier|endpoint/i);
      expect(message).not.toContain(privateValue);
    }
  });

  it('rejects duplicate reasons, impossible session counters, and future qualification time', () => {
    const duplicateReason = validObservatoryAdapterEvent();
    duplicateReason.bundle.snapshot.qualification!.reason_codes.push(
      duplicateReason.bundle.snapshot.qualification!.reason_codes[0],
    );
    expect(() => decodeObservatoryAdapterEvent(duplicateReason)).toThrow(/reason.*unique/i);

    const impossibleAccepted = validObservatoryAdapterEvent();
    const session = impossibleAccepted.bundle.snapshot.sessions[0];
    session.state = 'accepted';
    session.terminal = false;
    expect(() => decodeObservatoryAdapterEvent(impossibleAccepted)).toThrow(/state|count/i);

    const future = validObservatoryAdapterEvent();
    future.bundle.snapshot.qualification!.issued_at_unix_ms =
      future.bundle.snapshot.observed_at_unix_ms + 1;
    expect(() => decodeObservatoryAdapterEvent(future)).toThrow(/issued|observation|future/i);
  });

  it('rejects sessions that are temporally or qualification-incoherent', () => {
    const futureSession = validObservatoryAdapterEvent();
    futureSession.bundle.snapshot.sessions[0].updated_at_unix_ms =
      futureSession.bundle.snapshot.observed_at_unix_ms + 1;
    expect(() => decodeObservatoryAdapterEvent(futureSession)).toThrow(
      /session.*observation|timestamp/i,
    );

    const missingQualification = validObservatoryAdapterEvent();
    (
      missingQualification.bundle.snapshot as unknown as {
        qualification: unknown;
      }
    ).qualification = null;
    expect(() => decodeObservatoryAdapterEvent(missingQualification)).toThrow(
      /session.*qualification/i,
    );

    const activeMismatch = validObservatoryAdapterEvent();
    const session = activeMismatch.bundle.snapshot.sessions[0];
    session.state = 'streaming';
    session.terminal = false;
    session.qualification_id = 'other-qualification';
    expect(() => decodeObservatoryAdapterEvent(activeMismatch)).toThrow(
      /session.*qualification/i,
    );
  });

  it('enforces bounded arrays and exact count coherence', () => {
    const event = validObservatoryAdapterEvent();
    event.bundle.provisioning.buffered_sessions = 2;
    expect(() => decodeObservatoryAdapterEvent(event)).toThrow(/buffered_sessions/i);

    const incidentOverflow = validObservatoryAdapterEvent();
    incidentOverflow.bundle.provisioning.quarantine_capacity = 1;
    incidentOverflow.bundle.incidents.push(clone(incidentOverflow.bundle.incidents[0]));
    expect(() => decodeObservatoryAdapterEvent(incidentOverflow)).toThrow(/capacity/i);
  });
});
