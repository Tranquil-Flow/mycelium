import { render, type RenderResult } from '@testing-library/react';
import App from '../App';
import type { ProductQualification, ProductSourceMode } from '../app/contracts';
import type { ProductFeatureRegistry, ProductRouteId } from '../app/navigation';
import { productRouteHref } from '../app/navigation';
import {
  createInitialProductState,
  productStateReducer,
  type ProductState,
} from '../app/ProductState';
import {
  ReplayObservatorySource,
  StaticObservatorySource,
  type ObservatoryDataSource,
  type ObservatorySourceState,
  type LiveObservatorySourceState,
} from '../data/observatorySource';
import { decodeObservatorySnapshot } from '../model/semanticProjection';
import { validSemanticSnapshot } from './semanticFixture';
import {
  installProductNetworkRecorder,
  type ProductNetworkRecorder,
} from './networkRecorder';

const HARNESS_NOW_UNIX_MS = Date.parse('2026-07-18T12:00:00Z');

class SemanticHarnessSource implements ObservatoryDataSource {
  readonly kind = 'live' as const;
  readonly source_mode = 'live' as const;
  private readonly state: LiveObservatorySourceState;

  constructor() {
    const snapshot = decodeObservatorySnapshot(validSemanticSnapshot());
    this.state = Object.freeze({
      source_mode: 'live',
      status: 'connected',
      generation: 1,
      snapshot,
      live_qualified: false,
      qualification_reasons: Object.freeze(['harness_source_not_qualifier']),
      freshness: 'current',
    });
  }

  loadInitial(): ObservatorySourceState {
    return this.state;
  }

  getState(): ObservatorySourceState {
    return this.state;
  }
}

export interface RenderProductFeatureOptions {
  readonly route?: ProductRouteId;
  readonly source_mode?: ProductSourceMode;
  readonly source?: ObservatoryDataSource;
  readonly featureRegistry?: ProductFeatureRegistry;
  readonly qualification?: ProductQualification | null;
  readonly now_unix_ms?: number;
  readonly recordNetwork?: boolean;
}

export interface RenderProductResult extends RenderResult {
  readonly source: ObservatoryDataSource;
  readonly productState: ProductState;
  readonly networkRecorder: ProductNetworkRecorder | null;
}

function sourceForMode(mode: ProductSourceMode): ObservatoryDataSource {
  if (mode === 'live') return new SemanticHarnessSource();
  if (mode === 'replay') return new ReplayObservatorySource();
  return new StaticObservatorySource();
}

export function renderProductFeature(
  options: RenderProductFeatureOptions = {},
): RenderProductResult {
  const sourceMode = options.source_mode ?? options.source?.source_mode ?? 'fixture';
  const source = options.source ?? sourceForMode(sourceMode);
  if (source.source_mode !== sourceMode) {
    throw new TypeError('renderProductFeature source_mode/source mismatch');
  }
  const route = options.route ?? 'inference';
  const nowUnixMs = options.now_unix_ms ?? HARNESS_NOW_UNIX_MS;
  let productState = createInitialProductState({
    source_mode: sourceMode,
    now_unix_ms: nowUnixMs,
  });
  productState = productStateReducer(productState, {
    type: 'observatory_updated',
    source_mode: sourceMode,
    generation: source.getState()?.generation ?? 0,
  });
  if (options.qualification !== undefined) {
    productState = productStateReducer(productState, {
      type: 'qualification_updated',
      qualification: options.qualification,
      now_unix_ms: nowUnixMs,
    });
  }
  window.history.replaceState(null, '', productRouteHref(route));
  const networkRecorder = options.recordNetwork ? installProductNetworkRecorder() : null;
  const result = render(
    <App
      source={source}
      featureRegistry={options.featureRegistry}
      productState={productState}
    />,
  );
  return Object.assign(result, { source, productState, networkRecorder });
}
