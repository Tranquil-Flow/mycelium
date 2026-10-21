// SPDX-License-Identifier: AGPL-3.0-only
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { createObservatorySource } from './data/observatorySource';
import { PRODUCT_API_PATHS, PRODUCT_BOOTSTRAP_PROTOCOL, PRODUCT_QUALIFIER_AUTHORITY, MAX_NEW_TOKENS, MAX_PROMPT_UTF8_BYTES } from './app/contracts';

// Test-only HTTP contracts. No live provider or physical qualification is asserted.
function bootstrap(mode: 'live' | 'fixture') {
  return { protocol: PRODUCT_BOOTSTRAP_PROTOCOL, source_mode: mode, session: {
    csrf_header: 'X-Mycelium-CSRF', csrf_token: 'test-only-csrf-token-not-a-secret', expires_at_unix_ms: Date.now() + 60_000,
  }, api: PRODUCT_API_PATHS, qualification_authority: PRODUCT_QUALIFIER_AUTHORITY, limits: { max_prompt_utf8_bytes: MAX_PROMPT_UTF8_BYTES, max_new_tokens: MAX_NEW_TOKENS } };
}
beforeEach(() => { window.history.replaceState(null, '', '/'); });
afterEach(() => vi.restoreAllMocks());
describe('consumer/operator route composition', () => {
  it('mounts the root consumer with live HTTP and no fixture fallback when the backend is missing', async () => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('test backend unavailable'));
    render(<App source={createObservatorySource({ source_mode: 'fixture' })} />);
    expect(screen.getByRole('heading', {level:1})).toHaveTextContent('A little question.');
    expect(await screen.findByRole('button', {name:'Retry connection'})).toBeVisible();
    expect(screen.getByRole('button', {name:'Send prompt'})).toBeDisabled();
    expect(screen.queryByRole('complementary', {name:'Full-context replay recovery status'})).not.toBeInTheDocument();
    expect(screen.queryByText(/fixture data · not live/i)).not.toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls.every(([url]) => String(url) === '/api/v1/bootstrap')).toBe(true);
    fireEvent.click(screen.getByRole('button',{name:'Retry connection'}));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('button', {name:'Retry connection'})).toBeVisible();
    expect(fetcher).toHaveBeenCalledTimes(2); // no automatic resubmit after failure
    fireEvent.click(screen.getByRole('button',{name:'Retry connection'}));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
    expect(fetcher.mock.calls.every(([url, init]) => String(url) === '/api/v1/bootstrap' && init?.method === 'GET')).toBe(true);
  });
  it('refuses a fixture bootstrap rather than opening the live evidence gate', async () => {
    const fetcher = vi.spyOn(globalThis,'fetch').mockImplementation(async url => {
      if (String(url) === '/api/v1/bootstrap') return new Response(JSON.stringify(bootstrap('fixture')), {headers:{'Content-Type':'application/json'}});
      return new Response('{}',{status:503,headers:{'Content-Type':'application/json'}});
    });
    render(<App />);
    expect(await screen.findByText(/product_gateway_source_mode_mismatch/)).toBeVisible();
    expect(screen.getByRole('button',{name:'Send prompt'})).toBeDisabled();
    expect(fetcher.mock.calls.some(([url]) => String(url) === PRODUCT_API_PATHS.product_snapshot)).toBe(false);
  });
  it('starts the real evidence source only after live bootstrap and keeps missing evidence unavailable', async () => {
    const fetcher = vi.spyOn(globalThis,'fetch').mockImplementation(async url => {
      if (String(url) === '/api/v1/bootstrap') return new Response(JSON.stringify(bootstrap('live')), {headers:{'Content-Type':'application/json'}});
      return new Response('{}',{status:503,headers:{'Content-Type':'application/json'}});
    });
    render(<App />);
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url) === PRODUCT_API_PATHS.product_snapshot)).toBe(true));
    expect(screen.getByRole('button',{name:'Send prompt'})).toBeDisabled();
    expect(screen.getByLabelText('Published peers')).toHaveTextContent('Unavailable');
  });
  it('preserves operator hash navigation, A6 recovery status and the evidence alias', async () => {
    window.history.replaceState(null,'','/operator#evidence');
    render(<App />);
    expect(screen.getByRole('complementary',{name:'Full-context replay recovery status'})).toHaveAttribute('data-route','readiness');
    expect(window.location.hash).toBe('#readiness');
    fireEvent.click(screen.getByRole('link',{name:/plans/i}));
    expect(window.location.pathname).toBe('/operator');
    expect(window.location.hash).toBe('#plans');
    expect(screen.queryByLabelText('Your prompt')).not.toBeInTheDocument();
    act(() => { window.history.pushState(null,'','/operator#network'); window.dispatchEvent(new PopStateEvent('popstate')); });
    expect(screen.getByRole('complementary',{name:'Full-context replay recovery status'})).toHaveAttribute('data-route','network');
  });
  it('moves legacy root hashes to /operator and swaps shells on history navigation', async () => {
    vi.spyOn(globalThis,'fetch').mockRejectedValue(new Error('unavailable'));
    window.history.replaceState(null,'','/#network');
    render(<App />);
    expect(window.location.pathname).toBe('/operator');
    act(() => { window.history.pushState(null,'','/#consumer-how'); window.dispatchEvent(new PopStateEvent('popstate')); });
    expect(screen.getByLabelText('Your prompt')).toBeInTheDocument();
    expect(screen.queryByRole('complementary',{name:'Full-context replay recovery status'})).not.toBeInTheDocument();
    expect(screen.getAllByRole('main')).toHaveLength(1);
    await screen.findByRole('button',{name:'Retry connection'});
  });
  it('does not treat a lookalike pathname as an operator route', () => {
    window.history.replaceState(null,'','/operator-extra');
    render(<App />);
    expect(screen.getByRole('heading',{name:'Page not found'})).toBeVisible();
    expect(screen.queryByRole('complementary',{name:'Full-context replay recovery status'})).not.toBeInTheDocument();
  });
});
