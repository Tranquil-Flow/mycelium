// SPDX-License-Identifier: AGPL-3.0-only
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PRODUCT_ROUTES, productRouteHref } from '../app/navigation';
import { PRODUCT_QUALIFIER_AUTHORITY } from '../app/contracts';
import { AppShell } from './AppShell';

function shell(onViewChange = vi.fn()) {
  return render(
    <AppShell
      activeView="readiness"
      onViewChange={onViewChange}
      scopeLabel="Local source"
      sourceMode="fixture"
      sourceState={null}
      routeReadiness={{ authority: PRODUCT_QUALIFIER_AUTHORITY, value: false, status: 'unknown', reasons: [] }}
    >
      <h1>Readiness</h1>
    </AppShell>,
  );
}

describe('AppShell product navigation', () => {
  it('retains route-derived accessible link names when mobile CSS hides nav-copy', () => {
    // Match the responsive product rule: the icon is aria-hidden, and nav-copy disappears.
    const style = document.createElement('style');
    style.textContent = '.nav-copy { display: none !important; }';
    document.head.append(style);
    const onViewChange = vi.fn();
    try {
      shell(onViewChange);
      const nav = screen.getByRole('navigation', { name: 'Product sections' });
      for (const route of PRODUCT_ROUTES) {
        const link = within(nav).getByRole('link', { name: route.label });
        expect(link).toHaveAttribute('href', productRouteHref(route.id));
        if (route.id === 'readiness') expect(link).toHaveAttribute('aria-current', 'page');
        else expect(link).not.toHaveAttribute('aria-current');
      }
      const settings = within(nav).getByRole('link', { name: 'Settings' });
      settings.focus();
      expect(settings).toHaveFocus();
      fireEvent.click(settings);
      expect(onViewChange).toHaveBeenCalledWith('settings');
      style.remove();
      expect(within(nav).getByRole('link', { name: 'Readiness' })).toHaveAccessibleName('Readiness');
    } finally {
      style.remove();
    }
  });
});
