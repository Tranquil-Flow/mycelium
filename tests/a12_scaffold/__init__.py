# SPDX-License-Identifier: AGPL-3.0-only
"""A12 scaffold lane: RED-first tests and the honest refusal-boundary suite.

Three suites live here, and the distinction between them is the point:

``test_a12_strong_red_absent_behaviour.py``
    STRONG RED.  Imports only surfaces that already exist at the frozen acceptance
    base ``c573e0f`` and never imports this lane's scaffold.  Each failure is
    evidence that the baseline cannot do the thing.

``test_a12_scaffold_boundaries.py``
    GREEN.  Pins that every declared execution seam refuses with its exact coded
    error and that the honesty guards behave as designed.  Passing here is evidence
    of an honest boundary, never of working A12 behaviour.

``test_a12_unimplemented_execution_red.py``
    WEAK RED.  Fails because this lane's own scaffold raises
    ``A12NotImplementedError``.  That is honest incompleteness, not absent baseline
    behaviour, and it is recorded as such.
"""
