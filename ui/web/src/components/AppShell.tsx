import type { ReactNode } from 'react';
import type {
  ObservatorySourceMode,
  ObservatorySourceState,
} from '../data/observatorySource';

export type ObservatoryView = 'network' | 'plans' | 'incidents' | 'evidence';

interface AppShellProps {
  readonly activeView: ObservatoryView;
  readonly onViewChange: (view: ObservatoryView) => void;
  readonly scopeLabel: string;
  readonly sourceMode: ObservatorySourceMode;
  readonly sourceState: ObservatorySourceState | null;
  readonly children: ReactNode;
}

const navItems: ReadonlyArray<{
  id: ObservatoryView;
  label: string;
  detail: string;
  icon: 'network' | 'plans' | 'incidents' | 'evidence';
}> = [
  { id: 'network', label: 'Network', detail: 'Route topology', icon: 'network' },
  { id: 'plans', label: 'Plans', detail: 'Modeled strategies', icon: 'plans' },
  { id: 'incidents', label: 'Incidents', detail: 'Fixture replays', icon: 'incidents' },
  { id: 'evidence', label: 'Evidence', detail: 'Proof boundaries', icon: 'evidence' },
];

function NavIcon({ name }: { readonly name: 'network' | 'plans' | 'incidents' | 'evidence' }) {
  if (name === 'network') {
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
  children,
}: AppShellProps) {
  const isFixture = sourceMode === 'fixture';
  const isLiveQualified =
    sourceState?.source_mode === 'live' && sourceState.live_qualified;
  const currentLabel = isFixture
    ? 'Current unavailable'
    : sourceState === null
      ? 'Connecting'
      : sourceState.status === 'disconnected'
        ? `Disconnected · g${sourceState.generation}`
        : isLiveQualified
          ? `Current · g${sourceState.generation}`
          : `Not live · g${sourceState.generation}`;

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
              <p className="eyebrow">Mycelium systems</p>
              <span className="maturity-badge" aria-label="Product lifecycle: MVP">MVP</span>
            </div>
            <h1>Network Observatory</h1>
          </div>
        </div>

        <div
          className="fixture-badge"
          aria-label={
            isFixture
              ? 'Offline evidence mode'
              : isLiveQualified
                ? 'Qualified live semantic mode'
                : 'Semantic projection not live'
          }
        >
          <span className="fixture-dot" aria-hidden="true" />
          {isFixture
            ? 'SIMULATION · FIXTURE'
            : isLiveQualified
              ? 'LIVE · QUALIFIED'
              : 'SEMANTIC PROJECTION · NOT LIVE'}
        </div>

        <nav className="primary-nav" aria-label="Observatory sections">
          <p className="nav-caption">Workspace</p>
          {navItems.map((item) => (
            <a
              key={item.id}
              href={`#${item.id}`}
              className="nav-button"
              aria-current={activeView === item.id ? 'page' : undefined}
              onClick={() => onViewChange(item.id)}
            >
              <span className="nav-icon"><NavIcon name={item.icon} /></span>
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
              <strong>{isFixture ? 'Local evidence bundle' : 'Gateway semantic source'}</strong>
              <span>{isFixture ? 'No network dependency' : 'Same-origin GET + SSE only'}</span>
            </div>
          </div>
          <p>Read-only qualification surface</p>
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <div className="topbar-context">
            <span className="topbar-label">Scope</span>
            <strong>{scopeLabel}</strong>
            <span className="separator" aria-hidden="true">/</span>
            <span>{isFixture ? 'offline replay' : 'semantic gateway projection'}</span>
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
