import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import type { ProductEvidenceState } from './source';

export interface ProductEvidenceSourcePort {
  getState(): ProductEvidenceState | null;
  loadInitial(): Promise<ProductEvidenceState>;
  subscribe(listener: (state: ProductEvidenceState) => void): () => void;
}

interface ProductEvidenceContextValue {
  readonly configured: boolean;
  readonly latest: ProductEvidenceState | null;
  readonly visible: ProductEvidenceState | null;
  readonly history: readonly ProductEvidenceState[];
  readonly loading: boolean;
  readonly error_code: string | null;
  readonly frozen: boolean;
  freeze(): void;
  resume(): void;
}

const unavailable: ProductEvidenceContextValue = {
  configured: false,
  latest: null,
  visible: null,
  history: [],
  loading: false,
  error_code: null,
  frozen: false,
  freeze: () => undefined,
  resume: () => undefined,
};

const Context = createContext<ProductEvidenceContextValue>(unavailable);

export function ProductEvidenceProvider({
  source,
  children,
}: {
  readonly source?: ProductEvidenceSourcePort;
  readonly children: ReactNode;
}) {
  const [latest, setLatest] = useState<ProductEvidenceState | null>(() => source?.getState() ?? null);
  const [history, setHistory] = useState<readonly ProductEvidenceState[]>(() => {
    const initial = source?.getState() ?? null;
    return initial === null ? [] : [initial];
  });
  const [frozenState, setFrozenState] = useState<ProductEvidenceState | null>(null);
  const [loading, setLoading] = useState(source !== undefined && latest === null);
  const [errorCode, setErrorCode] = useState<string | null>(null);

  useEffect(() => {
    if (source === undefined) {
      setLatest(null);
      setHistory([]);
      setFrozenState(null);
      setLoading(false);
      setErrorCode(null);
      return;
    }
    let active = true;
    setLatest(source.getState());
    setHistory(source.getState() === null ? [] : [source.getState() as ProductEvidenceState]);
    setFrozenState(null);
    setLoading(source.getState() === null);
    setErrorCode(null);
    const unsubscribe = source.subscribe((state) => {
      if (!active) return;
      setLatest(state);
      setErrorCode(null);
      setHistory((current) => {
        const previous = current.at(-1);
        const next = previous?.cursor === state.cursor
          ? [...current.slice(0, -1), state]
          : [...current, state];
        return next.slice(-64);
      });
      setLoading(false);
    });
    void source.loadInitial().then((state) => {
      if (!active) return;
      setLatest(source.getState() ?? state);
      setErrorCode(null);
      setHistory((current) => {
        const accepted = source.getState() ?? state;
        const previous = current.at(-1);
        if (previous?.cursor === accepted.cursor) {
          return [...current.slice(0, -1), accepted];
        }
        return [...current, accepted].slice(-64);
      });
      setLoading(false);
    }).catch(() => {
      if (!active) return;
      const recovered = source.getState();
      if (recovered !== null) {
        setLatest(recovered);
        setErrorCode(null);
      } else {
        setErrorCode('product_evidence_unavailable');
      }
      setLoading(false);
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, [source]);

  const value = useMemo<ProductEvidenceContextValue>(() => ({
    configured: source !== undefined,
    latest,
    visible: frozenState ?? latest,
    history,
    loading,
    error_code: errorCode,
    frozen: frozenState !== null,
    freeze: () => {
      if (latest !== null) setFrozenState(latest);
    },
    resume: () => setFrozenState(null),
  }), [errorCode, frozenState, history, latest, loading, source]);

  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useProductEvidence(): ProductEvidenceContextValue {
  return useContext(Context);
}
