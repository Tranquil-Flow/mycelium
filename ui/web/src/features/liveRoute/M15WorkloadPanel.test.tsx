import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import fixture from '../../../../../contracts/compatibility-fixtures/m15-plan-comparison-v1.json';
import { decodeM15PlanComparison } from './m15Comparison';
import { M15WorkloadPanel } from './M15WorkloadPanel';

describe('M15WorkloadPanel', () => {
  it('shows both workload matrices, robust winners, Pareto reasons, and the M16 boundary', () => {
    render(<M15WorkloadPanel comparison={decodeM15PlanComparison(structuredClone(fixture))} />);
    expect(screen.getByRole('heading', { name: 'Workload-aware plan frontier' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'interactive_chat_v1' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'sustained_batch_v1' })).toBeInTheDocument();
    expect(screen.getAllByText(/robust winner/i).length).toBeGreaterThan(1);
    expect(screen.getAllByText(/Pareto frontier/i)).toHaveLength(2);
    expect(screen.getByText(/admission_latency.*queueing/i)).toBeInTheDocument();
    const table = screen.getByRole('table', { name: /interactive_chat_v1 policy comparison/i });
    expect(within(table).getByText('prefill_ttft')).toBeInTheDocument();
    expect(within(table).getAllByText(/modeled/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/physical calibration:/i)).toHaveLength(2);
    expect(screen.getByRole('table', { name: /interactive_chat_v1 modeled versus observed calibration/i })).toBeInTheDocument();
    expect(screen.getAllByText(/approved exclusions:/i)).toHaveLength(2);
  });
});
