import {
  PRODUCT_QUALIFIER_AUTHORITY,
  inferenceBlockReason,
  type ProductQualification,
  type ProductSourceMode,
} from './contracts';
import type { ProductRouteId } from './navigation';

export interface RouteReadinessState {
  readonly authority: typeof PRODUCT_QUALIFIER_AUTHORITY;
  readonly value: boolean;
  readonly status: 'unknown' | 'blocked' | 'accepted';
  readonly reasons: readonly string[];
}

export interface ProductState {
  readonly active_route: ProductRouteId;
  readonly source_mode: ProductSourceMode;
  readonly qualification: ProductQualification | null;
  readonly latest_qualification_issued_at_unix_ms: number | null;
  readonly latest_qualification_digest: string | null;
  readonly qualification_conflicted_at_watermark: boolean;
  readonly route_readiness: RouteReadinessState;
  readonly now_unix_ms: number;
  readonly observatory_generation: number | null;
  readonly browser_ready_workers: number | null;
}

export type ProductStateAction =
  | { readonly type: 'navigated'; readonly route: ProductRouteId }
  | {
      readonly type: 'observatory_updated';
      readonly generation: number;
      readonly source_mode: ProductSourceMode;
    }
  | {
      readonly type: 'qualification_updated';
      readonly qualification: ProductQualification | null;
      readonly now_unix_ms: number;
    }
  | { readonly type: 'clock_ticked'; readonly now_unix_ms: number }
  | { readonly type: 'browser_swarm_updated'; readonly ready_workers: number | null };

function freezeReadiness(value: RouteReadinessState): RouteReadinessState {
  return Object.freeze({
    ...value,
    reasons: Object.freeze([...value.reasons]),
  });
}

function freezeState(value: ProductState): ProductState {
  return Object.freeze(value);
}

export function routeReadinessFromQualification(
  qualification: ProductQualification | null,
  nowUnixMs: number,
  sourceMode: ProductSourceMode = 'live',
): RouteReadinessState {
  if (sourceMode !== 'live') {
    return freezeReadiness({
      authority: PRODUCT_QUALIFIER_AUTHORITY,
      value: false,
      status: 'unknown',
      reasons: [`${sourceMode}_source_not_authoritative`],
    });
  }
  if (qualification === null) {
    return freezeReadiness({
      authority: PRODUCT_QUALIFIER_AUTHORITY,
      value: false,
      status: 'unknown',
      reasons: ['qualification_unavailable'],
    });
  }
  const blocked = inferenceBlockReason(qualification, nowUnixMs);
  if (blocked !== null) {
    return freezeReadiness({
      authority: PRODUCT_QUALIFIER_AUTHORITY,
      value: false,
      status: 'blocked',
      reasons:
        qualification.route_ready || qualification.reason_codes.length === 0
          ? [blocked]
          : qualification.reason_codes,
    });
  }
  return freezeReadiness({
    authority: PRODUCT_QUALIFIER_AUTHORITY,
    value: true,
    status: 'accepted',
    reasons: [],
  });
}

export function normalizeOptionalMetric(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

export interface InitialProductStateOptions {
  readonly source_mode?: ProductSourceMode;
  readonly now_unix_ms?: number;
}

export function createInitialProductState(
  options: InitialProductStateOptions = {},
): ProductState {
  const sourceMode = options.source_mode ?? 'fixture';
  const nowUnixMs = normalizeOptionalMetric(options.now_unix_ms) ?? 0;
  return freezeState({
    active_route: 'inference',
    source_mode: sourceMode,
    qualification: null,
    latest_qualification_issued_at_unix_ms: null,
    latest_qualification_digest: null,
    qualification_conflicted_at_watermark: false,
    route_readiness: routeReadinessFromQualification(null, nowUnixMs, sourceMode),
    now_unix_ms: nowUnixMs,
    observatory_generation: null,
    browser_ready_workers: null,
  });
}

function routeReadinessConflict(): RouteReadinessState {
  return freezeReadiness({
    authority: PRODUCT_QUALIFIER_AUTHORITY,
    value: false,
    status: 'blocked',
    reasons: ['qualification_conflict_at_watermark'],
  });
}

export function productStateReducer(
  state: ProductState,
  action: ProductStateAction,
): ProductState {
  switch (action.type) {
    case 'navigated':
      return action.route === state.active_route
        ? state
        : freezeState({ ...state, active_route: action.route });
    case 'observatory_updated': {
      const generation = normalizeOptionalMetric(action.generation);
      const live = action.source_mode === 'live';
      const qualification = live ? state.qualification : null;
      return freezeState({
        ...state,
        source_mode: action.source_mode,
        qualification,
        latest_qualification_issued_at_unix_ms: live
          ? state.latest_qualification_issued_at_unix_ms
          : null,
        latest_qualification_digest: live ? state.latest_qualification_digest : null,
        qualification_conflicted_at_watermark: live
          ? state.qualification_conflicted_at_watermark
          : false,
        route_readiness:
          live && state.qualification_conflicted_at_watermark
            ? routeReadinessConflict()
            : routeReadinessFromQualification(
                qualification,
                state.now_unix_ms,
                action.source_mode,
              ),
        observatory_generation: generation,
      });
    }
    case 'qualification_updated': {
      const candidateNow = normalizeOptionalMetric(action.now_unix_ms) ?? state.now_unix_ms;
      const nowUnixMs = Math.max(state.now_unix_ms, candidateNow);
      if (state.source_mode !== 'live') {
        return freezeState({
          ...state,
          qualification: null,
          latest_qualification_issued_at_unix_ms: null,
          latest_qualification_digest: null,
          qualification_conflicted_at_watermark: false,
          now_unix_ms: nowUnixMs,
          route_readiness: routeReadinessFromQualification(null, nowUnixMs, state.source_mode),
        });
      }
      if (action.qualification === null) {
        return freezeState({
          ...state,
          qualification: null,
          now_unix_ms: nowUnixMs,
          route_readiness: state.qualification_conflicted_at_watermark
            ? routeReadinessConflict()
            : routeReadinessFromQualification(null, nowUnixMs, 'live'),
        });
      }

      const issuedAt = action.qualification.issued_at_unix_ms;
      const digest = action.qualification.binding.qualification_digest;
      const watermark = state.latest_qualification_issued_at_unix_ms;
      if (watermark !== null && issuedAt < watermark) {
        return freezeState({
          ...state,
          now_unix_ms: nowUnixMs,
          route_readiness: state.qualification_conflicted_at_watermark
            ? routeReadinessConflict()
            : routeReadinessFromQualification(state.qualification, nowUnixMs, 'live'),
        });
      }
      if (
        watermark !== null &&
        issuedAt === watermark &&
        state.qualification_conflicted_at_watermark
      ) {
        return freezeState({
          ...state,
          qualification: null,
          now_unix_ms: nowUnixMs,
          route_readiness: routeReadinessConflict(),
        });
      }
      if (
        watermark !== null &&
        issuedAt === watermark &&
        state.latest_qualification_digest !== null &&
        digest !== state.latest_qualification_digest
      ) {
        return freezeState({
          ...state,
          qualification: null,
          qualification_conflicted_at_watermark: true,
          now_unix_ms: nowUnixMs,
          route_readiness: routeReadinessConflict(),
        });
      }

      const qualification = action.qualification;
      return freezeState({
        ...state,
        qualification,
        latest_qualification_issued_at_unix_ms: issuedAt,
        latest_qualification_digest: digest,
        qualification_conflicted_at_watermark: false,
        now_unix_ms: nowUnixMs,
        route_readiness: routeReadinessFromQualification(
          qualification,
          nowUnixMs,
          state.source_mode,
        ),
      });
    }
    case 'clock_ticked': {
      const candidateNow = normalizeOptionalMetric(action.now_unix_ms) ?? state.now_unix_ms;
      const nowUnixMs = Math.max(state.now_unix_ms, candidateNow);
      return freezeState({
        ...state,
        now_unix_ms: nowUnixMs,
        route_readiness: state.qualification_conflicted_at_watermark
          ? routeReadinessConflict()
          : routeReadinessFromQualification(
              state.qualification,
              nowUnixMs,
              state.source_mode,
            ),
      });
    }
    case 'browser_swarm_updated':
      return freezeState({
        ...state,
        browser_ready_workers: normalizeOptionalMetric(action.ready_workers),
      });
  }
}
