import type { ComponentType } from 'react';

export type ProductRouteId =
  | 'inference'
  | 'network'
  | 'nodes'
  | 'plans'
  | 'readiness'
  | 'incidents'
  | 'settings';

export interface ProductRouteDescriptor {
  readonly id: ProductRouteId;
  readonly label: string;
  readonly detail: string;
}

export const PRODUCT_ROUTES: readonly ProductRouteDescriptor[] = Object.freeze([
  Object.freeze({ id: 'inference', label: 'Inference', detail: 'Qualified requests' }),
  Object.freeze({ id: 'network', label: 'Network', detail: 'Route topology' }),
  Object.freeze({ id: 'nodes', label: 'Nodes', detail: 'Native and browser peers' }),
  Object.freeze({ id: 'plans', label: 'Plans', detail: 'Modeled strategies' }),
  Object.freeze({ id: 'readiness', label: 'Readiness', detail: 'Evidence and authority' }),
  Object.freeze({ id: 'incidents', label: 'Incidents', detail: 'Failure history' }),
  Object.freeze({ id: 'settings', label: 'Settings', detail: 'Local diagnostics' }),
]);

const PRODUCT_ROUTE_IDS = new Set<ProductRouteId>(PRODUCT_ROUTES.map((route) => route.id));

export type ProductFeatureComponent = ComponentType;
export interface ProductFeatureModule {
  readonly default: ProductFeatureComponent;
}
export type ProductFeatureLoader = () => Promise<ProductFeatureModule>;
export interface ProductFeatureRegistration {
  readonly id: ProductRouteId;
  readonly load: ProductFeatureLoader;
}
export type ProductFeatureRegistry = Readonly<
  Partial<Record<ProductRouteId, ProductFeatureLoader>>
>;

export class ProductNavigationError extends TypeError {
  constructor(message: string) {
    super(message);
    this.name = 'ProductNavigationError';
  }
}

export function isProductRoute(value: string): value is ProductRouteId {
  return PRODUCT_ROUTE_IDS.has(value as ProductRouteId);
}

export function parseProductRoute(hash: string): ProductRouteId | null {
  const candidate = hash.replace(/^#/, '');
  if (candidate === 'evidence') return 'readiness';
  return isProductRoute(candidate) ? candidate : null;
}

export function productRouteHref(route: ProductRouteId): `#${ProductRouteId}` {
  return `#${route}`;
}

export function createProductFeatureRegistry(
  registrations: readonly ProductFeatureRegistration[],
): ProductFeatureRegistry {
  const registry: Partial<Record<ProductRouteId, ProductFeatureLoader>> = {};
  for (const registration of registrations) {
    if (!isProductRoute(registration.id)) {
      throw new ProductNavigationError(`Unknown product feature route: ${registration.id}`);
    }
    if (typeof registration.load !== 'function') {
      throw new ProductNavigationError(`Feature loader for ${registration.id} must be callable`);
    }
    if (registry[registration.id] !== undefined) {
      throw new ProductNavigationError(`Duplicate product feature route: ${registration.id}`);
    }
    registry[registration.id] = registration.load;
  }
  return Object.freeze(registry);
}
