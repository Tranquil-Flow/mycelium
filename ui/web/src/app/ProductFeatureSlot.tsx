import { lazy, Suspense, type LazyExoticComponent, type ReactNode } from 'react';
import type {
  ProductFeatureComponent,
  ProductFeatureLoader,
  ProductFeatureRegistry,
  ProductRouteId,
} from './navigation';
import type { ProductState } from './ProductState';

const lazyFeatures = new WeakMap<
  ProductFeatureLoader,
  LazyExoticComponent<ProductFeatureComponent>
>();

function lazyFeature(loader: ProductFeatureLoader): LazyExoticComponent<ProductFeatureComponent> {
  const existing = lazyFeatures.get(loader);
  if (existing !== undefined) return existing;
  const created = lazy(loader);
  lazyFeatures.set(loader, created);
  return created;
}

export interface ProductFeatureSlotProps {
  readonly route: ProductRouteId;
  readonly registry: ProductFeatureRegistry;
  readonly productState: ProductState;
  readonly children: ReactNode;
}

export function ProductFeatureSlot({
  route,
  registry,
  productState,
  children,
}: ProductFeatureSlotProps) {
  if (route === 'inference' && productState.route_readiness.value !== true) return children;
  const loader = registry[route];
  if (loader === undefined) return children;
  const Feature = lazyFeature(loader);
  return (
    <Suspense
      fallback={
        <section className="panel bundle-error" role="status" aria-live="polite">
          <span className="layout-loader" aria-hidden="true" />
          <div>
            <p className="eyebrow">Product feature</p>
            <h2>Loading {route} workspace</h2>
          </div>
        </section>
      }
    >
      <Feature />
    </Suspense>
  );
}
