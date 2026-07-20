import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { OnboardingWizard } from './OnboardingWizard';
import { AdminWorkspace } from '../admin/AdminWorkspace';
import type { MembershipClient } from './membershipClient';

function client(): MembershipClient {
  return {
    status: vi.fn(async () => ({ protocol: 'mycelium.product_membership.v1', generated_at: '2026-07-19T00:00:00Z', members: [{ member_id: 'node-a', state: 'qualified', connectivity: 'direct', endpoint_id: null, evidence: 'supplied' }], unknowns: [] } as const)),
    createInvite: vi.fn(async () => ({ invite_code: 'join-once', expires_at: '2026-07-19T00:05:00Z', single_use: true } as const)),
    join: vi.fn(async () => ({ accepted: true, member_id: 'node-b', state: 'invited' } as const)),
    revoke: vi.fn(async () => ({ revoked: true, member_id: 'node-a' })),
  };
}

describe('membership UI', () => {
  it('uses a progressive single-use onboarding flow', async () => {
    const api = client();
    render(<OnboardingWizard client={api} />);
    expect(screen.getByRole('heading', { name: /join a trusted swarm/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /create single-use invite/i }));
    expect(await screen.findByText('join-once')).toBeInTheDocument();
    expect(screen.getByText(/invite is not route readiness/i)).toBeInTheDocument();
  });

  it('disables an already-open join flow when source becomes read-only', () => {
    const api = client();
    const { rerender } = render(<OnboardingWizard client={api} />);
    fireEvent.click(screen.getByRole('button', { name: /join with invite code/i }));

    rerender(<OnboardingWizard client={api} readOnly />);

    expect(screen.getByLabelText(/invite code/i)).toBeDisabled();
    expect(screen.getByLabelText(/endpoint identity/i)).toBeDisabled();
    expect(screen.getByRole('button', { name: /^join swarm$/i })).toBeDisabled();
  });

  it('shows explicit states and requires revocation confirmation', async () => {
    const api = client();
    render(<AdminWorkspace client={api} />);
    expect(await screen.findByText('node-a')).toBeInTheDocument();
    expect(screen.getByText(/qualified/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /revoke node-a/i }));
    expect(screen.getByRole('button', { name: /confirm revoke node-a/i })).toBeInTheDocument();
  });
});
