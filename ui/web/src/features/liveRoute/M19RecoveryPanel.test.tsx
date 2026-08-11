import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { M19RecoveryPanel } from './M19RecoveryPanel';
import { decodeM19Liveness, decodeM19RecoveryPlan, decodeM19RecoveryRuntime } from './m19Recovery';
import { livenessFixture, planFixture, runtimeFixture } from './m19Recovery.test';

const evidence = () => ({ liveness: decodeM19Liveness(livenessFixture()), plan: decodeM19RecoveryPlan(planFixture()), runtime: decodeM19RecoveryRuntime(runtimeFixture()) });
describe('M19RecoveryPanel', () => {
  it('shows replay without a KV transfer claim in inference history', () => { const value = evidence(); render(<M19RecoveryPanel {...value} view="inference" />); expect(screen.getByText('full context replay')).toBeInTheDocument(); expect(screen.getByText('not transferred')).toBeInTheDocument(); });
  it('shows scoped failures and explicit aborts', () => { const value = evidence(); render(<M19RecoveryPanel {...value} view="incidents" />); expect(screen.getByText(/placement · active disconnect/i)).toBeInTheDocument(); expect(screen.getByText(/truthful abort/i)).toBeInTheDocument(); expect(screen.getByText(/no continuity claimed/i)).toBeInTheDocument(); });
  it('shows hysteresis and surviving tracks', () => { const value = evidence(); render(<M19RecoveryPanel {...value} view="plans" />); expect(screen.getByText('hysteresis pending')).toBeInTheDocument(); expect(screen.getByText(/incumbent tracks remain immutable/i)).toBeInTheDocument(); });
});
