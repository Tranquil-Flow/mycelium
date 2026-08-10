import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { createObservatorySource } from './data/observatorySource';
import {
  createProductObservatoryLiveSource,
  resolveProductObservatorySourceMode,
} from './features/observatory/live/ObservatoryController';
import { bootstrapProductGatewaySession } from './features/observatory/live/productGatewaySession';
import { consumeDeviceLabOperatorCapability } from './features/deviceLab/deviceLabCapability';
import { ProductEvidenceProvider } from './features/productEvidence/ProductEvidenceContext';
import {
  createLiveProductEvidenceSource,
  type LiveProductEvidenceSource,
} from './features/productEvidence/source';

const deviceLabOperatorToken = consumeDeviceLabOperatorCapability();

const rootElement = document.getElementById('root');
if (rootElement === null) throw new Error('Network Observatory root element is missing');
const root = createRoot(rootElement);
const sourceMode = resolveProductObservatorySourceMode(import.meta.env.VITE_OBSERVATORY_SOURCE_MODE);

function renderProduct(
  source: Parameters<typeof App>[0]['source'],
  productEvidenceSource?: LiveProductEvidenceSource,
): void {
  root.render(
    <StrictMode>
      <ProductEvidenceProvider source={productEvidenceSource}>
        <App source={source} deviceLabOperatorToken={deviceLabOperatorToken} />
      </ProductEvidenceProvider>
    </StrictMode>,
  );
}

async function bootstrapLiveProduct(): Promise<void> {
  const bootstrap = await bootstrapProductGatewaySession();
  if (bootstrap.source_mode !== 'live') throw new Error('product_gateway_source_mode_mismatch');
  renderProduct(
    createProductObservatoryLiveSource(),
    createLiveProductEvidenceSource(),
  );
}

if (deviceLabOperatorToken !== null) {
  // The isolated Device Lab server intentionally exposes only its same-origin
  // operator API, not the physical product bootstrap. App renders the live local
  // Device Lab workspace before consulting this non-authoritative fixture source.
  renderProduct(createObservatorySource({ source_mode: 'fixture' }));
} else if (sourceMode === 'fixture') {
  renderProduct(createObservatorySource({ source_mode: 'fixture' }));
} else {
  root.render(
    <StrictMode>
      <main role="status" aria-live="polite">Establishing same-origin product gateway session…</main>
    </StrictMode>,
  );
  void bootstrapLiveProduct().catch(() => {
    root.render(
      <StrictMode>
        <main role="alert">
          Product gateway unavailable. No direct Observatory or request-gateway fallback was attempted.
        </main>
      </StrictMode>,
    );
  });
}
