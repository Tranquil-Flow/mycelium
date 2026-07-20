import { describe, expect, it, vi } from 'vitest';
import {
  PRODUCT_ROUTES,
  ProductNavigationError,
  createProductFeatureRegistry,
  parseProductRoute,
  productRouteHref,
} from './navigation';

describe('product feature registry and navigation', () => {
  it('exposes all eight product workspaces in stable order', () => {
    expect(PRODUCT_ROUTES.map(({ id, label }) => ({ id, label }))).toEqual([
      { id: 'inference', label: 'Inference' },
      { id: 'lab', label: 'Device Lab' },
      { id: 'network', label: 'Network' },
      { id: 'nodes', label: 'Nodes' },
      { id: 'plans', label: 'Plans' },
      { id: 'readiness', label: 'Readiness' },
      { id: 'incidents', label: 'Incidents' },
      { id: 'settings', label: 'Settings' },
    ]);
    expect(new Set(PRODUCT_ROUTES.map((route) => route.id)).size).toBe(8);
    expect(Object.isFrozen(PRODUCT_ROUTES)).toBe(true);
  });

  it('parses product hashes and keeps the former evidence deep link as readiness', () => {
    expect(parseProductRoute('#inference')).toBe('inference');
    expect(parseProductRoute('#lab')).toBe('lab');
    expect(parseProductRoute('#readiness')).toBe('readiness');
    expect(parseProductRoute('#evidence')).toBe('readiness');
    expect(parseProductRoute('#unknown')).toBeNull();
    expect(productRouteHref('nodes')).toBe('#nodes');
  });

  it('registers lazy modules without executing loaders during shell setup', () => {
    const loader = vi.fn(async () => ({ default: () => null }));
    const registry = createProductFeatureRegistry([
      { id: 'inference', load: loader },
      { id: 'nodes', load: loader },
    ]);
    expect(loader).not.toHaveBeenCalled();
    expect(registry.inference).toBe(loader);
    expect(registry.nodes).toBe(loader);
    expect(registry.network).toBeUndefined();
    expect(Object.isFrozen(registry)).toBe(true);
  });

  it('rejects duplicate and unknown feature slots', () => {
    const loader = async () => ({ default: () => null });
    expect(() =>
      createProductFeatureRegistry([
        { id: 'inference', load: loader },
        { id: 'inference', load: loader },
      ]),
    ).toThrow(ProductNavigationError);
    expect(() =>
      createProductFeatureRegistry([{ id: 'bogus' as 'inference', load: loader }]),
    ).toThrow('Unknown product feature route');
  });
});
