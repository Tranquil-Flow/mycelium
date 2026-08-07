import { useEffect, useState } from 'react';
import { ProductFeatureSlot } from './app/ProductFeatureSlot';
import { createInitialProductState, type ProductState } from './app/ProductState';
import {
  createProductFeatureRegistry,
  parseProductRoute,
  productRouteHref,
  type ProductFeatureRegistry,
  type ProductRouteId,
} from './app/navigation';
import { FixtureInferenceClient } from './app/fixtureInferenceClient';
import { ProductNodesWorkspace } from './app/ProductNodesWorkspace';
import { AppShell } from './components/AppShell';
import {
  createObservatorySource,
  type LiveObservatorySourceState,
  type ObservatoryDataSource,
  type ObservatorySourceState,
} from './data/observatorySource';
import type {
  LiveObservatoryEventSource,
  LiveObservatoryEventState,
} from './data/observatoryEventSource';
import { EvidenceView } from './views/EvidenceView';
import { DeviceLabWorkspace } from './features/deviceLab/DeviceLabWorkspace';
import { InferenceWorkspace } from './features/inference/InferenceWorkspace';
import { LifecycleCoveragePanel } from './features/lifecycle/LifecycleBadge';
import {
  LIFECYCLE_STATE_ORDER,
  projectLifecycle,
} from './features/lifecycle/lifecycleProjection';
import { SettingsProvider } from './features/settings/SettingsContext';
import { SettingsWorkspace } from './features/settings/SettingsWorkspace';
import { IncidentsView } from './views/IncidentsView';
import { NetworkView } from './views/NetworkView';
import { PlansView } from './views/PlansView';
import { preparingFixture } from './test/lifecycleFixtures/recordedSnapshots';
import './styles.css';

type AppSource = ObservatoryDataSource | LiveObservatoryEventSource;
type AppSourceState = ObservatorySourceState | LiveObservatoryEventState;

type SourceResult =
  | { readonly source: AppSource; readonly state: 'loading' }
  | {
      readonly source: AppSource;
      readonly state: 'ready';
      readonly sourceState: AppSourceState;
    }
  | { readonly source: AppSource; readonly state: 'error'; readonly message: string };

export interface AppProps {
  readonly source?: AppSource;
  readonly featureRegistry?: ProductFeatureRegistry;
  readonly productState?: ProductState;
  /** Memory-only capability consumed from the Device Lab URL fragment. */
  readonly deviceLabOperatorToken?: string | null;
}

const defaultSource = createObservatorySource({ source_mode: 'fixture' });
const emptyFeatureRegistry = createProductFeatureRegistry([]);
const fixtureInferenceClient = new FixtureInferenceClient();
const recordedLifecycleProjections = Object.freeze(
  LIFECYCLE_STATE_ORDER.map((state) => projectLifecycle(preparingFixture({ state }))),
);

function sourceErrorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : 'Unknown Observatory source error';
}

function initialSourceResult(source: AppSource): SourceResult {
  const current = source.getState();
  if (current !== null) return { source, state: 'ready', sourceState: current };
  if (source.source_mode !== 'fixture') return { source, state: 'loading' };
  try {
    const initial = source.loadInitial();
    return initial instanceof Promise
      ? { source, state: 'loading' }
      : { source, state: 'ready', sourceState: initial };
  } catch (reason: unknown) {
    return { source, state: 'error', message: sourceErrorMessage(reason) };
  }
}

function BundleError({
  message,
  sourceMode,
}: {
  readonly message: string;
  readonly sourceMode: ObservatoryDataSource['source_mode'];
}) {
  return (
    <section className="bundle-error panel" role="alert">
      <span aria-hidden="true">!</span>
      <div>
        <p className="eyebrow caution">
          {sourceMode === 'fixture' ? 'Offline fixture error' : 'Semantic source error'}
        </p>
        <h1>Evidence projection unavailable</h1>
        <p>{message}</p>
        <small>No fallback values were inferred or presented.</small>
      </div>
    </section>
  );
}

function BundleLoading({ sourceMode }: { readonly sourceMode: ObservatoryDataSource['source_mode'] }) {
  return (
    <section className="bundle-error panel" role="status" aria-live="polite">
      <span className="layout-loader" aria-hidden="true" />
      <div>
        <p className="eyebrow">Read-only source</p>
        <h1>
          {sourceMode === 'fixture'
            ? 'Loading coherent evidence snapshot'
            : 'Loading semantic Observatory snapshot'}
        </h1>
        <small>No partial generation is rendered.</small>
      </div>
    </section>
  );
}

function isProductEventState(state: AppSourceState): state is LiveObservatoryEventState {
  return state.source_mode === 'live' && 'projection' in state;
}

function ProductGatewayProjectionView({ state }: { readonly state: LiveObservatoryEventState }) {
  const qualification = state.projection.snapshot.qualification;
  return (
    <section className="panel semantic-observatory" aria-label="Product gateway Observatory projection">
      <p className="eyebrow">Privacy-preserving product gateway projection</p>
      <h1>Live deployment observation</h1>
      <dl>
        <div><dt>Generation</dt><dd>{state.generation}</dd></div>
        <div><dt>Source cursor</dt><dd>{state.source_cursor}</dd></div>
        <div><dt>Connection</dt><dd>{state.status} · {state.freshness}</dd></div>
        <div><dt>Request sessions</dt><dd>{state.projection.snapshot.sessions.length}</dd></div>
        <div><dt>Incidents</dt><dd>{state.projection.incidents.length}</dd></div>
        <div><dt>Route readiness</dt><dd>{state.route_ready ? 'Accepted physical qualification' : 'Not accepted'}</dd></div>
      </dl>
      {qualification === null ? <p role="status">No qualifier-owned evidence is present.</p> : null}
    </section>
  );
}

function SemanticProjectionView({ state }: { readonly state: LiveObservatorySourceState }) {
  const { snapshot } = state;
  return (
    <section className="panel semantic-observatory" aria-label="Semantic Observatory projection">
      <p className="eyebrow">Privacy-preserving semantic projection</p>
      <h1>Semantic deployment observation</h1>
      <dl>
        <div><dt>Deployment</dt><dd>{snapshot.binding.deployment.id}</dd></div>
        <div><dt>Model</dt><dd>{snapshot.binding.model.id} · {snapshot.binding.model.revision}</dd></div>
        <div><dt>Route</dt><dd>{snapshot.binding.route.id} · g{snapshot.binding.route.generation}</dd></div>
        <div><dt>Challenge</dt><dd>{snapshot.route_challenge.status}</dd></div>
        <div><dt>Request lifecycle</dt><dd>{snapshot.request_lifecycle.state}</dd></div>
        <div><dt>Conflicts</dt><dd>{snapshot.conflicts.length}</dd></div>
      </dl>
      {!state.live_qualified && (
        <p role="status">Not live: {state.qualification_reasons.join(', ')}</p>
      )}
    </section>
  );
}

export default function App({
  source = defaultSource,
  featureRegistry = emptyFeatureRegistry,
  productState,
  deviceLabOperatorToken = null,
}: AppProps) {
  const effectiveProductState =
    productState !== undefined && productState.source_mode === source.source_mode
      ? productState
      : createInitialProductState({
          source_mode: source.source_mode,
          now_unix_ms: Date.now(),
        });
  const [activeView, setActiveView] = useState<ProductRouteId>(
    () => parseProductRoute(window.location.hash) ?? 'inference',
  );
  const [sourceResult, setSourceResult] = useState<SourceResult>(() =>
    initialSourceResult(source),
  );

  useEffect(() => {
    const synchronizeHash = () => {
      const nextView = parseProductRoute(window.location.hash);
      if (nextView === null) {
        window.history.replaceState(null, '', productRouteHref('inference'));
        setActiveView('inference');
        return;
      }
      const canonicalHash = productRouteHref(nextView);
      if (window.location.hash !== canonicalHash) {
        window.history.replaceState(null, '', canonicalHash);
      }
      setActiveView(nextView);
    };
    synchronizeHash();
    window.addEventListener('hashchange', synchronizeHash);
    return () => window.removeEventListener('hashchange', synchronizeHash);
  }, []);

  useEffect(() => {
    let active = true;
    let unsubscribe: (() => void) | undefined;

    const acceptState = (nextState: AppSourceState) => {
      if (!active) return;
      if (nextState.source_mode !== source.source_mode) {
        setSourceResult({
          source,
          state: 'error',
          message: 'Observatory source_mode/state mismatch',
        });
        return;
      }
      setSourceResult({ source, state: 'ready', sourceState: nextState });
    };
    const acceptError = (reason: unknown) => {
      if (!active) return;
      const current = source.getState();
      if (current !== null) acceptState(current);
      else setSourceResult({ source, state: 'error', message: sourceErrorMessage(reason) });
    };

    const current = source.getState();
    setSourceResult(
      current === null
        ? { source, state: 'loading' }
        : { source, state: 'ready', sourceState: current },
    );

    try {
      unsubscribe = source.subscribe?.(acceptState);
      const initial = source.loadInitial();
      if (initial instanceof Promise) {
        void initial
          .then((loadedState) => acceptState(source.getState() ?? loadedState))
          .catch(acceptError);
      } else {
        acceptState(source.getState() ?? initial);
      }
    } catch (reason: unknown) {
      acceptError(reason);
    }

    return () => {
      active = false;
      unsubscribe?.();
    };
  }, [source]);

  const navigate = (view: ProductRouteId) => {
    setActiveView(view);
    const nextHash = productRouteHref(view);
    if (window.location.hash !== nextHash) window.history.pushState(null, '', nextHash);
  };

  const rendered: SourceResult =
    sourceResult.source === source ? sourceResult : { source, state: 'loading' };

  let content;
  if (activeView === 'lab') {
    content = <DeviceLabWorkspace operatorToken={deviceLabOperatorToken} />;
  } else if (activeView === 'settings') {
    content = <SettingsWorkspace />;
  } else if (rendered.state === 'loading') {
    content = <BundleLoading sourceMode={source.source_mode} />;
  } else if (rendered.state === 'error') {
    content = <BundleError message={rendered.message} sourceMode={source.source_mode} />;
  } else if (activeView === 'inference') {
    content = (
      <InferenceWorkspace
        client={source.source_mode !== 'live' ? fixtureInferenceClient : undefined}
        externalBlockReason={
          (source.source_mode !== 'live' || productState !== undefined) &&
          !effectiveProductState.route_readiness.value
            ? `Inference disabled: ${effectiveProductState.route_readiness.reasons.join(', ')}`
            : null
        }
      />
    );
  } else if (rendered.sourceState.source_mode === 'live') {
    content = activeView === 'nodes'
      ? <ProductNodesWorkspace sourceMode="live" />
      : isProductEventState(rendered.sourceState)
        ? <ProductGatewayProjectionView state={rendered.sourceState} />
        : <SemanticProjectionView state={rendered.sourceState} />;
  } else {
    const { snapshot, incidents, provisioning } = rendered.sourceState.bundle;
    switch (activeView) {
      case 'network':
        content = <NetworkView snapshot={snapshot} />;
        break;
      case 'nodes':
        content = <ProductNodesWorkspace sourceMode="fixture" snapshot={snapshot} provisioning={provisioning} />;
        break;
      case 'plans':
        content = <PlansView snapshot={snapshot} />;
        break;
      case 'incidents':
        content = <IncidentsView incidents={incidents} />;
        break;
      case 'readiness':
        content = (
          <>
            <EvidenceView
              snapshot={snapshot}
              incidents={incidents}
              provisioning={provisioning}
            />
            <LifecycleCoveragePanel projections={recordedLifecycleProjections} />
          </>
        );
        break;
    }
  }

  const sourceState = rendered.state === 'ready' ? rendered.sourceState : null;
  const acceptedPhysicalLiveReadiness =
    productState === undefined &&
    sourceState !== null &&
    isProductEventState(sourceState) &&
    sourceState.status === 'connected' &&
    sourceState.freshness === 'current' &&
    sourceState.route_ready &&
    sourceState.projection.snapshot.qualification?.evidence_class === 'physical_qualification';
  const renderedProductState: ProductState = acceptedPhysicalLiveReadiness
    ? {
        ...effectiveProductState,
        route_readiness: {
          value: true,
          status: 'accepted',
          authority: 'mycelium_qualification.qualifier:RouteQualificationV1',
          reasons: [],
        },
      }
    : effectiveProductState;
  const scopeLabel = (() => {
    if (activeView === 'lab') return 'local browser-stage swarm';
    if (sourceState === null) {
      if (source.source_mode === 'fixture') return 'offline fixture';
      if (source.source_mode === 'replay') return 'replay evidence';
      return 'product gateway';
    }
    if ('bundle' in sourceState) return sourceState.bundle.snapshot.source.scenarioName;
    if (isProductEventState(sourceState)) {
      return sourceState.projection.snapshot.qualification?.binding.deployment_id ?? 'product gateway';
    }
    return sourceState.snapshot.binding.deployment.id;
  })();

  return (
    <SettingsProvider>
      <AppShell
        activeView={activeView}
        onViewChange={navigate}
        scopeLabel={scopeLabel}
        sourceMode={source.source_mode}
        sourceState={sourceState}
        routeReadiness={renderedProductState.route_readiness}
      >
        <ProductFeatureSlot
          route={activeView}
          registry={featureRegistry}
          productState={renderedProductState}
        >
          {content}
        </ProductFeatureSlot>
      </AppShell>
    </SettingsProvider>
  );
}
