import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { createObservatorySource } from './data/observatorySource';

const root = document.getElementById('root');

if (root === null) {
  throw new Error('Network Observatory root element is missing');
}

const configuredMode: unknown = import.meta.env.VITE_OBSERVATORY_SOURCE_MODE ?? 'fixture';
const source =
  configuredMode === 'fixture'
    ? createObservatorySource({ source_mode: 'fixture' })
    : configuredMode === 'live'
      ? createObservatorySource({ source_mode: 'live' })
      : (() => {
          throw new TypeError('Unknown VITE_OBSERVATORY_SOURCE_MODE');
        })();

createRoot(root).render(
  <StrictMode>
    <App source={source} />
  </StrictMode>,
);
