import type { ReactNode } from 'react';
import { PRODUCT_ROUTES, productRouteHref, type ProductRouteId } from '../app/navigation';
import type { RouteReadinessState } from '../app/ProductState';
import type { ProductSourceMode } from '../app/contracts';
import type {
  ObservatorySourceState,
} from '../data/observatorySource';

interface AppShellProps {
  readonly activeView: ProductRouteId;
  readonly onViewChange: (view: ProductRouteId) => void;
  readonly scopeLabel: string;
  readonly sourceMode: ProductSourceMode;
  readonly sourceState: ObservatorySourceState | null;
  readonly routeReadiness: RouteReadinessState;
  readonly children: ReactNode;
}

function NavIcon({ name }: { readonly name: ProductRouteId }) {
  if (name === 'network' || name === 'nodes') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="5" cy="12" r="2.25" />
        <circle cx="18.5" cy="5.5" r="2.25" />
        <circle cx="18.5" cy="18.5" r="2.25" />
        <path d="M7 11l9.25-4.4M7 13l9.25 4.4" />
      </svg>
    );
  }
  if (name === 'plans') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M5 4.5h14v15H5zM8 8h8M8 12h8M8 16h5" />
      </svg>
    );
  }
  if (name === 'incidents') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 3.5l9 16H3l9-16zM12 9v4.5M12 17h.01" />
      </svg>
    );
  }
  if (name === 'inference') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4 6h16v12H4zM8 10l3 2-3 2M13 14h3" />
      </svg>
    );
  }
  if (name === 'settings') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="3" />
        <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M6 3.5h9l3 3v14H6zM15 3.5v4h3M9 12l2 2 4-4M9 17h6" />
    </svg>
  );
}

export function AppShell({
  activeView,
  onViewChange,
  scopeLabel,
  sourceMode,
  sourceState,
  routeReadiness,
  children,
}: AppShellProps) {
  const isFixture = sourceMode === 'fixture';
  const isReplay = sourceMode === 'replay';
  const isLiveCurrent =
    sourceState?.source_mode === 'live' &&
    sourceState.status === 'connected' &&
    sourceState.freshness === 'current';
  const currentLabel = isFixture
    ? 'Fixture data · not current'
    : isReplay
      ? sourceState === null
        ? 'Replay evidence · loading'
        : `Replay evidence · g${sourceState.generation}`
    : sourceState === null
      ? 'Connecting'
      : sourceState.status === 'disconnected'
        ? `Disconnected · g${sourceState.generation}`
        : isLiveCurrent
          ? `Current evidence · g${sourceState.generation}`
          : `Stale evidence · g${sourceState.generation}`;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          <div>
            <div className="brand-eyebrow-row">
              <p className="eyebrow">Private distributed inference</p>
              <span className="maturity-badge" aria-label="Product lifecycle: MVP">MVP</span>
            </div>
            <h1>Mycelium</h1>
          </div>
        </div>

        <div
          className="fixture-badge"
          aria-label={
            isFixture
              ? 'Fixture data, not live'
              : isReplay
                ? 'Replay evidence, not live'
                : 'Live semantic evidence state'
          }
        >
          <span className="fixture-dot" aria-hidden="true" />
          {isFixture
            ? 'FIXTURE DATA · NOT LIVE'
            : isReplay
              ? 'REPLAY EVIDENCE · NOT LIVE'
            : isLiveCurrent
              ? 'LIVE EVIDENCE · CURRENT'
              : 'LIVE EVIDENCE · NOT CURRENT'}
        </div>

        <nav className="primary-nav" aria-label="Product sections">
          <p className="nav-caption">Workspace</p>
          {PRODUCT_ROUTES.map((item) => (
            <a
              key={item.id}
              href={productRouteHref(item.id)}
              className="nav-button"
              aria-current={activeView === item.id ? 'page' : undefined}
              onClick={() => onViewChange(item.id)}
            >
              <span className="nav-icon"><NavIcon name={item.id} /></span>
              <span className="nav-copy">
                <span>{item.label}</span>
                <small>{item.detail}</small>
              </span>
              <span className="nav-marker" aria-hidden="true" />
            </a>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="source-state">
            <span className="source-glyph" aria-hidden="true">◇</span>
            <div>
              <strong>
                {isFixture
                  ? 'Local evidence bundle'
                  : isReplay
                    ? 'Local evidence replay'
                    : 'Same-origin product gateway'}
              </strong>
              <span>
                {isFixture || isReplay
                  ? 'No network dependency'
                  : 'Browser credentials stay same-origin'}
              </span>
            </div>
          </div>
          <p>
            Route readiness {routeReadiness.status} · {routeReadiness.authority}
          </p>
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <div className="topbar-context">
            <span className="topbar-label">Scope</span>
            <strong>{scopeLabel}</strong>
            <span className="separator" aria-hidden="true">/</span>
            <span>
              {isFixture
                ? 'fixture evidence'
                : isReplay
                  ? 'replay evidence'
                  : 'semantic gateway projection'}
            </span>
          </div>
          <button type="button" className="current-control" disabled>
            <span className="status-ring" aria-hidden="true" />
            {currentLabel}
          </button>
        </header>
        <main className="main-content" id="main-content" tabIndex={-1}>
          {children}
        </main>
      </div>
    </div>
  );
}
