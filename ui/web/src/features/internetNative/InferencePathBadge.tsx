// SPDX-License-Identifier: AGPL-3.0-or-later
//
// InferencePathBadge — Inference workspace component.
//
// Shows only the OBSERVED path class. Membership alone never implies
// inference availability: an unqualified route renders the explicit copy
// regardless of path class.

import type { PathClass } from './types';
import { renderPathClass } from './projections';

export interface InferencePathBadgeProps {
  readonly path_class: PathClass;
  readonly route_qualified: boolean;
}

export function InferencePathBadge(props: InferencePathBadgeProps) {
  return (
    <section aria-label="Inference path">
      <div aria-label="observed path class">{renderPathClass(props.path_class)}</div>
      {props.route_qualified ? null : (
        <div>membership alone does not make inference available</div>
      )}
    </section>
  );
}
