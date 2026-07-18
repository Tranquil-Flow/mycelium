import { useEffect, useState } from 'react';
import { AppShell, type ObservatoryView } from './components/AppShell';
import {
  createObservatorySource,
  type LiveObservatorySourceState,
  type ObservatoryDataSource,
  type ObservatorySourceState,
} from './data/observatorySource';
import { EvidenceView } from './views/EvidenceView';
import { IncidentsView } from './views/IncidentsView';
import { NetworkView } from './views/NetworkView';
import { PlansView } from './views/PlansView';
import './styles.css';

type SourceResult =
  | { readonly source: ObservatoryDataSource; readonly state: 'loading' }
  | {
      readonly source: ObservatoryDataSource;
      readonly state: 'ready';
      readonly sourceState: ObservatorySourceState;
    }
  | { readonly source: ObservatoryDataSource; readonly state: 'error'; readonly message: string };

export interface AppProps {
  readonly source?: ObservatoryDataSource;
}

const defaultSource = createObservatorySource({ source_mode: 'fixture' });
const OBSERVATORY_VIEWS: readonly ObservatoryView[] = [
  'network',
  'plans',
  'incidents',
  'evidence',
];

function viewFromHash(hash: string): ObservatoryView | null {
  const candidate = hash.replace(/^#/, '');
  return OBSERVATORY_VIEWS.includes(candidate as ObservatoryView)
    ? (candidate as ObservatoryView)
    : null;
}

function sourceErrorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : 'Unknown Observatory source error';
}

function initialSourceResult(source: ObservatoryDataSource): SourceResult {
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
        <h2>Evidence projection unavailable</h2>
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
        <h2>
          {sourceMode === 'fixture'
            ? 'Loading coherent evidence snapshot'
            : 'Loading semantic Observatory snapshot'}
        </h2>
        <small>No partial generation is rendered.</small>
      </div>
    </section>
  );
}

function SemanticProjectionView({ state }: { readonly state: LiveObservatorySourceState }) {
  const { snapshot } = state;
  return (
    <section className="panel semantic-observatory" aria-label="Semantic Observatory projection">
      <p className="eyebrow">Privacy-preserving semantic projection</p>
      <h2>Semantic deployment observation</h2>
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

export default function App({ source = defaultSource }: AppProps) {
  const [activeView, setActiveView] = useState<ObservatoryView>(
    () => viewFromHash(window.location.hash) ?? 'network',
  );
  const [sourceResult, setSourceResult] = useState<SourceResult>(() =>
    initialSourceResult(source),
  );

  useEffect(() => {
    const synchronizeHash = () => {
      const nextView = viewFromHash(window.location.hash);
      if (nextView === null) {
        window.history.replaceState(null, '', '#network');
        setActiveView('network');
        return;
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

    const acceptState = (nextState: ObservatorySourceState) => {
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

  const navigate = (view: ObservatoryView) => {
    setActiveView(view);
    const nextHash = `#${view}`;
    if (window.location.hash !== nextHash) window.history.pushState(null, '', nextHash);
  };

  const rendered: SourceResult =
    sourceResult.source === source ? sourceResult : { source, state: 'loading' };

  let content;
  if (rendered.state === 'loading') {
    content = <BundleLoading sourceMode={source.source_mode} />;
  } else if (rendered.state === 'error') {
    content = <BundleError message={rendered.message} sourceMode={source.source_mode} />;
  } else if (rendered.sourceState.source_mode === 'live') {
    content = <SemanticProjectionView state={rendered.sourceState} />;
  } else {
    const { snapshot, incidents, provisioning } = rendered.sourceState.bundle;
    switch (activeView) {
      case 'network':
        content = <NetworkView snapshot={snapshot} />;
        break;
      case 'plans':
        content = <PlansView snapshot={snapshot} />;
        break;
      case 'incidents':
        content = <IncidentsView incidents={incidents} />;
        break;
      case 'evidence':
        content = (
          <EvidenceView
            snapshot={snapshot}
            incidents={incidents}
            provisioning={provisioning}
          />
        );
        break;
    }
  }

  const sourceState = rendered.state === 'ready' ? rendered.sourceState : null;
  const scopeLabel =
    sourceState === null
      ? source.source_mode === 'fixture'
        ? 'offline fixture'
        : 'semantic gateway'
      : sourceState.source_mode === 'fixture'
        ? sourceState.bundle.snapshot.source.scenarioName
        : sourceState.snapshot.binding.deployment.id;

  return (
    <AppShell
      activeView={activeView}
      onViewChange={navigate}
      scopeLabel={scopeLabel}
      sourceMode={source.source_mode}
      sourceState={sourceState}
    >
      {content}
    </AppShell>
  );
}
