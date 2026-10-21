// SPDX-License-Identifier: AGPL-3.0-only
import type {
  InferenceAcceptedResponse,
  InferenceCancelResponse,
  InferenceEvent,
  InferenceSubmission,
  ProductQualification,
} from '../../app/contracts';
import type { InferenceClient } from '../inference/requestClient';

/**
 * Client-side latency observation for one consumer request.
 *
 * The product route surface publishes no server TTFT/TPOT: the physical-run
 * transcript records `has_ttft: false` and `has_tpot: false` for both
 * `/api/v1/product/snapshot` and `/api/v1/observatory/snapshot`
 * (`~/Documents/mycelium-core-program/runs/v2/m1-real-origin/route-bringup/70-real-inference-transcript.jsonl`).
 * Anything shown as measured here is therefore measured in the browser from
 * real event arrivals on the request's own clock, and is labelled as such.
 */
export type ClientTimingBasis = 'client_measured' | 'unavailable';

export type ClientTimingUnavailableReason =
  | 'no_request_accepted'
  | 'no_token_observed'
  | 'single_token_observed'
  | 'non_monotonic_clock';

export interface ClientTimingMeasurement {
  /** `client_measured` only when a first token and an acceptance were both observed. */
  readonly basis: ClientTimingBasis;
  /** First token arrival minus the 202 acceptance of this request. Null when unavailable. */
  readonly ttft_ms: number | null;
  /** Mean inter-token interval across distinct applied token events. Null below two tokens. */
  readonly tpot_ms: number | null;
  /** Last token arrival minus the 202 acceptance of this request. */
  readonly last_token_ms: number | null;
  /** Distinct token events applied for this request (strict sequence order). */
  readonly tokens: number;
  /** How many times the same request's event stream was reopened at a cursor. */
  readonly resumed_streams: number;
  readonly unavailable_reason: ClientTimingUnavailableReason | null;
}

interface RequestTiming {
  submitted_at_ms: number | null;
  accepted_at_ms: number | null;
  first_token_at_ms: number | null;
  last_token_at_ms: number | null;
  tokens: number;
  last_generation: number | null;
  last_sequence: number | null;
  resumed_streams: number;
}

function emptyTiming(): RequestTiming {
  return {
    submitted_at_ms: null,
    accepted_at_ms: null,
    first_token_at_ms: null,
    last_token_at_ms: null,
    tokens: 0,
    last_generation: null,
    last_sequence: null,
    resumed_streams: 0,
  };
}

function unavailable(
  reason: ClientTimingUnavailableReason,
  timing: RequestTiming,
): ClientTimingMeasurement {
  return Object.freeze({
    basis: 'unavailable' as const,
    ttft_ms: null,
    tpot_ms: null,
    last_token_ms: null,
    tokens: timing.tokens,
    resumed_streams: timing.resumed_streams,
    unavailable_reason: reason,
  });
}

/**
 * A pass-through `InferenceClient` that timestamps the responses it forwards.
 *
 * It never invents, interpolates or estimates a token arrival: the only clock
 * reads are taken when the delegate returns an acceptance and when the delegate
 * hands back an event. Token events are counted in strict
 * `(publisher_generation, sequence)` order, so a resume that replays already
 * applied events cannot inflate the count.
 */
export class TimingInferenceClient implements InferenceClient {
  private timing: RequestTiming = emptyTiming();

  constructor(
    private readonly delegate: InferenceClient,
    private readonly now: () => number = Date.now,
  ) {}

  measure(): ClientTimingMeasurement {
    const timing = this.timing;
    if (timing.accepted_at_ms === null) return unavailable('no_request_accepted', timing);
    if (timing.tokens === 0 || timing.first_token_at_ms === null) {
      return unavailable('no_token_observed', timing);
    }
    const ttft = timing.first_token_at_ms - timing.accepted_at_ms;
    if (ttft < 0) return unavailable('non_monotonic_clock', timing);
    const last = timing.last_token_at_ms;
    const lastToken = last === null ? null : last - timing.accepted_at_ms;
    if (timing.tokens < 2 || last === null) {
      return Object.freeze({
        basis: 'unavailable' as const,
        ttft_ms: ttft,
        tpot_ms: null,
        last_token_ms: lastToken,
        tokens: timing.tokens,
        resumed_streams: timing.resumed_streams,
        unavailable_reason: 'single_token_observed' as const,
      });
    }
    return Object.freeze({
      basis: 'client_measured' as const,
      ttft_ms: ttft,
      tpot_ms: (last - timing.first_token_at_ms) / (timing.tokens - 1),
      last_token_ms: lastToken,
      tokens: timing.tokens,
      resumed_streams: timing.resumed_streams,
      unavailable_reason: null,
    });
  }

  private acceptToken(event: InferenceEvent): boolean {
    const timing = this.timing;
    const generation = event.publisher_generation;
    const sequence = event.sequence;
    if (timing.last_generation !== null) {
      if (generation < timing.last_generation) return false;
      if (generation === timing.last_generation && timing.last_sequence !== null &&
        sequence <= timing.last_sequence) {
        return false;
      }
    }
    timing.last_generation = generation;
    timing.last_sequence = sequence;
    return true;
  }

  async loadQualification(signal?: AbortSignal): Promise<ProductQualification> {
    return this.delegate.loadQualification(signal);
  }

  async submit(
    submission: InferenceSubmission,
    signal?: AbortSignal,
  ): Promise<InferenceAcceptedResponse> {
    const submittedAt = this.now();
    this.timing = emptyTiming();
    this.timing.submitted_at_ms = submittedAt;
    const accepted = await this.delegate.submit(submission, signal);
    this.timing.accepted_at_ms = this.now();
    return accepted;
  }

  async stream(
    request: InferenceAcceptedResponse,
    lastEventId: number | null,
    onEvent: (event: InferenceEvent) => void,
    signal?: AbortSignal,
    publisherGeneration?: number,
  ): Promise<void> {
    if (lastEventId !== null) this.timing.resumed_streams += 1;
    const observed = (event: InferenceEvent): void => {
      if (event.type === 'token' && this.acceptToken(event)) {
        const at = this.now();
        this.timing.tokens += 1;
        if (this.timing.first_token_at_ms === null) this.timing.first_token_at_ms = at;
        this.timing.last_token_at_ms = at;
      }
      onEvent(event);
    };
    return this.delegate.stream(request, lastEventId, observed, signal, publisherGeneration);
  }

  async cancel(
    request: InferenceAcceptedResponse,
    signal?: AbortSignal,
  ): Promise<InferenceCancelResponse> {
    return this.delegate.cancel(request, signal);
  }
}
