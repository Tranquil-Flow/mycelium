import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { M20SpeculationPanel } from './M20SpeculationPanel';
import { decodeM20SpeculativePlan, decodeM20SpeculativeRuntime } from './m20Speculation';
import { m20PlanFixture, m20RuntimeFixture } from './m20SpeculationFixtures';

describe('M20SpeculationPanel', () => {
  it('shows target-only and the measured disabled reason', () => {
    render(<M20SpeculationPanel plan={decodeM20SpeculativePlan(structuredClone(m20PlanFixture))} runtime={decodeM20SpeculativeRuntime(structuredClone(m20RuntimeFixture))} view="plans" />);
    expect(screen.getByText('target-only')).toBeInTheDocument();
    expect(screen.getByText('batched target verification unavailable')).toBeInTheDocument();
    expect(screen.getByText(/target-only is the safe baseline/i)).toBeInTheDocument();
  });
});
