// SPDX-License-Identifier: AGPL-3.0-only
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactElement } from 'react';
const calls = vi.hoisted(() => ({ render: vi.fn(), bootstrap: vi.fn(), live: vi.fn(() => ({source_mode:'live'})), fixture: vi.fn(() => ({source_mode:'fixture'})) }));
vi.mock('react-dom/client', () => ({createRoot: () => ({render:calls.render})}));
vi.mock('./App', () => ({default: () => null}));
vi.mock('./data/observatorySource', () => ({createObservatorySource:calls.fixture}));
vi.mock('./features/observatory/live/ObservatoryController', () => ({createProductObservatoryLiveSource:calls.live, resolveProductObservatorySourceMode:(v: string | undefined) => v ?? 'live'}));
vi.mock('./features/observatory/live/productGatewaySession', () => ({bootstrapProductGatewaySession:calls.bootstrap}));
vi.mock('./features/productEvidence/ProductEvidenceContext', () => ({ProductEvidenceProvider: () => null}));
function appProps() {
  const strict = calls.render.mock.calls.at(-1)![0] as ReactElement<{children:ReactElement<{children:ReactElement<Record<string,unknown>>}>}>;
  return strict.props.children.props.children.props;
}
beforeEach(() => {
  vi.resetModules(); vi.clearAllMocks();
  document.body.innerHTML = '<div id="root"></div>';
  window.history.replaceState(null,'','/');
  calls.bootstrap.mockRejectedValue(new Error('no local backend'));
});
afterEach(() => vi.unstubAllEnvs());
describe('actual entrypoint surface selection', () => {
  it('ignores an explicit fixture build for the consumer root', async () => {
    vi.stubEnv('VITE_OBSERVATORY_SOURCE_MODE','fixture');
    await import('./main');
    expect(calls.fixture).not.toHaveBeenCalled();
    expect(appProps().source).toEqual({source_mode:'live'});
    expect(appProps().deviceLabOperatorToken).toBeNull();
  });
  it('preserves explicit operator fixture mode only under /operator', async () => {
    vi.stubEnv('VITE_OBSERVATORY_SOURCE_MODE','fixture');
    window.history.replaceState(null,'','/operator#network');
    await import('./main');
    expect(calls.fixture).toHaveBeenCalledWith({source_mode:'fixture'});
    expect(appProps().source).toEqual({source_mode:'fixture'});
  });
  it('scrubs legacy Device Lab capabilities before moving to /operator and keeps them memory-only', async () => {
    const token = 'test-only-capability-'.padEnd(40,'a');
    window.history.replaceState(null,'',`/#lab/operator/${token}`);
    await import('./main');
    expect(window.location.pathname).toBe('/operator');
    expect(window.location.hash).toBe('#lab');
    expect(appProps().deviceLabOperatorToken).toBe(token);
    expect(calls.bootstrap).not.toHaveBeenCalled();
    expect(window.sessionStorage.length).toBe(0);
    expect(window.localStorage.length).toBe(0);
  });
  it('never grants a capability for a malformed capability-shaped fragment', async () => {
    window.history.replaceState(null,'','/#operator/short');
    await import('./main');
    await vi.waitFor(() => expect(calls.bootstrap).toHaveBeenCalled());
    expect(window.location.pathname).toBe('/operator');
    expect(window.location.hash).toBe('#lab');
    expect(calls.fixture).not.toHaveBeenCalled();
  });
});
