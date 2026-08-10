import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { M16RuntimePanel } from './M16RuntimePanel';
import { decodeM16RuntimeStatus } from './m16Runtime';
import { m16RuntimeFixture } from './m16Runtime.test';

describe('M16RuntimePanel', () => {
  it('shows bounded admission, queue state, and per-placement capacity', () => {
    render(<M16RuntimePanel runtime={decodeM16RuntimeStatus(m16RuntimeFixture())} view="plans" />);
    expect(screen.getByRole('heading', { name: 'Bounded workload scheduler' })).toBeInTheDocument();
    expect(screen.getByText('1 / 64')).toBeInTheDocument();
    expect(screen.getAllByText('placement-a')).toHaveLength(2);
    expect(screen.getByText(/sequential physical dispatch/i)).toBeInTheDocument();
  });
});
