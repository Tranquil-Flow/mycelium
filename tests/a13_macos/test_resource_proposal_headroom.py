"""The RSS proposal must clear the MEASURED load peak, not just a modelled one.

The runner halves `RESOURCE_PROPOSAL.max_combined_rss_bytes` into the per-node worker
limit, and the worker's guard measures the process GROUP (app + worker). The original
derivation modelled only the heaviest worker stage and never the app's own resident
footprint, so the bound was ~625 MB short.

Measured 2026-09-16 by the guard's own self-report (rounds 66r1/67r1,
`native-exit.private.json` -> `error_detail`):
    observed_group_rss_bytes 5,885,624,320
    max_rss_bytes            5,752,698,265
    overshoot_bytes            132,926,055   (~2.3%)

Both 66r1 and 67r1 died at the stream step on the resulting zero qualified runtimes
(`qualification_not_complete`). These tests pin that the bound now clears the measured
peak with real headroom, so the accounting cannot silently regress to a modelled-only
number.
"""
from mycelium_a13_macos.serving import RESOURCE_PROPOSAL

MEASURED_GROUP_PEAK_BYTES = 5_885_624_320
OBSERVED_LIMIT_THAT_FAILED = 5_752_698_265


def test_the_runner_derived_per_node_limit_clears_the_measured_group_peak():
    per_node = RESOURCE_PROPOSAL.max_combined_rss_bytes // 2
    assert per_node > MEASURED_GROUP_PEAK_BYTES


def test_the_margin_is_real_headroom_not_a_few_percent():
    # The old bound cleared its own model by 10% and still failed by 2.3% of the
    # measured peak: a margin that only just exceeds the measurement is not a margin.
    per_node = RESOURCE_PROPOSAL.max_combined_rss_bytes // 2
    assert per_node >= int(MEASURED_GROUP_PEAK_BYTES * 1.10)


def test_the_old_value_was_the_failing_one():
    """Guard against a silent revert to the number two rounds died on."""
    assert RESOURCE_PROPOSAL.max_combined_rss_bytes > OBSERVED_LIMIT_THAT_FAILED * 2


def test_the_proposal_still_respects_the_authorization_ceiling_semantics():
    # work_authorization rejects limits.max_rss_bytes > this value; the runner passes
    # exactly this value // 2, so the ceiling must stay the upper bound of the pair.
    per_node = RESOURCE_PROPOSAL.max_combined_rss_bytes // 2
    assert per_node <= RESOURCE_PROPOSAL.max_combined_rss_bytes
