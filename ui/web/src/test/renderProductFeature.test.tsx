import { cleanup, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import {
  makeAcceptedQualificationContractFixture,
  makeProductQualificationFixture,
} from './productFixtures';
import { renderProductFeature, type RenderProductResult } from './renderProductFeature';

let rendered: RenderProductResult | null = null;

afterEach(() => {
  rendered?.networkRecorder?.restore();
  rendered = null;
  cleanup();
});

describe('renderProductFeature harness', () => {
  it('renders a selected product route from coherent fixture evidence', () => {
    rendered = renderProductFeature({ route: 'network' });

    expect(window.location.hash).toBe('#network');
    expect(screen.getByRole('heading', { name: /network topology/i })).toBeInTheDocument();
    expect(screen.getByText(/fixture data · not live/i)).toBeInTheDocument();
    expect(rendered.source.source_mode).toBe('fixture');
  });

  it('defaults to disabled inference and records zero browser requests', () => {
    rendered = renderProductFeature({ recordNetwork: true });

    expect(window.location.hash).toBe('#inference');
    expect(screen.getByRole('heading', { name: /inference workspace/i })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: /prompt/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /submit request/i })).toBeDisabled();
    expect(screen.getByText(/inference disabled: fixture_source_not_authoritative/i)).toBeVisible();
    expect(screen.getByText(/no model request was made/i)).toBeInTheDocument();
    expect(rendered.networkRecorder?.requests).toHaveLength(0);
  });

  it('renders live truth without deriving readiness from semantic evidence', () => {
    rendered = renderProductFeature({ source_mode: 'live' });
    expect(screen.getByText(/live evidence · current/i)).toBeVisible();
    expect(rendered.productState.route_readiness).toMatchObject({
      value: false,
      status: 'unknown',
    });
  });

  it('renders explicit route_ready=false qualifier evidence as disabled', () => {
    rendered = renderProductFeature({
      source_mode: 'live',
      qualification: makeProductQualificationFixture({
        issued_at_unix_ms: Date.parse('2026-07-18T12:00:00Z'),
      }),
    });
    expect(rendered.productState.qualification?.route_ready).toBe(false);
    expect(screen.getByRole('textbox', { name: /prompt/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /submit request/i })).toBeDisabled();
    expect(screen.getByText(/physical_qualification_missing/i)).toBeVisible();
  });

  it('renders replay truth and clears even accepted physical qualification', () => {
    rendered = renderProductFeature({
      source_mode: 'replay',
      qualification: makeAcceptedQualificationContractFixture({
        issued_at_unix_ms: Date.parse('2026-07-18T12:00:00Z'),
      }),
    });
    expect(screen.getByText(/replay evidence · not live/i)).toBeVisible();
    expect(screen.getByRole('button', { name: /submit request/i })).toBeDisabled();
    expect(rendered.productState.route_readiness).toMatchObject({
      value: false,
      status: 'unknown',
    });
    expect(rendered.productState.qualification).toBeNull();
  });
});
