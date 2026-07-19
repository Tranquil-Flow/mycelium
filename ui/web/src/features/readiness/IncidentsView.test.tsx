import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { loadStaticObservatoryBundle } from '../../data/observatorySource';
import { IncidentsView } from '../../views/IncidentsView';

describe('IncidentsView replay', () => {
  const { incidents } = loadStaticObservatoryBundle();

  it('labels every record as supplied synthetic evidence and never invents severity', () => {
    render(<IncidentsView incidents={incidents} />);

    expect(screen.getByText(/no live incident occurred/i)).toBeInTheDocument();
    expect(screen.getByText(/no severity inferred/i)).toBeInTheDocument();
    expect(screen.getByText(/detector scope/i)).toBeInTheDocument();
    expect(screen.queryByText(/critical|warning severity|sev-[0-9]/i)).not.toBeInTheDocument();
  });

  it('supports timeline playback and does not call an activating replacement active', () => {
    render(<IncidentsView incidents={incidents} />);
    const replay = screen.getByRole('region', { name: /incident timeline replay controls/i });

    fireEvent.click(within(replay).getByRole('button', { name: /previous transition/i }));
    expect(screen.getByText(/activating replacement/i)).toBeInTheDocument();
    expect(screen.queryByText(/^active replacement$/i)).not.toBeInTheDocument();
    expect(within(replay).getByRole('slider', { name: /incident replay position/i })).toBeInTheDocument();
  });

  it('shows circuit break as 503 with no replacement route', () => {
    render(<IncidentsView incidents={incidents} />);
    fireEvent.click(screen.getByRole('button', { name: /circuit break/i }));

    expect(screen.getByText(/replacement route intentionally absent/i)).toBeInTheDocument();
    expect(screen.getByText(/503.*no reroute claimed/i)).toBeInTheDocument();
  });
});
