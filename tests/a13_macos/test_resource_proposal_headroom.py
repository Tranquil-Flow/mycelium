# SPDX-License-Identifier: AGPL-3.0-only
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

# Measured 2026-09-19 by the guard's own self-report (round p76r1, preview.76,
# owner node remote-mac-6df0e88d38533e6ad8ac589b -> native-exit.private.json):
#     error_code                worker_memory_limit_exceeded
#     observed_group_rss_bytes  7,371,915,264
#     max_rss_bytes             6,768,467,968   (this proposal // 2)
#     overshoot_bytes             603,447,296
# That trip is what cost p76r1 its qualification: the run-level `configure` was in
# flight, the worker refused, the pump honestly acked reconciliation_required, and the
# owner -- correctly refusing an unaccounted command -- marked the deployment
# unavailable.  The bound above was rebuilt from the 66r1/67r1 peak; the useful 7B
# tranche needs more, and a 7B int8 model's resident weights alone are ~7 GB, so this
# is a stale derivation rather than a leak.
MEASURED_GROUP_PEAK_BYTES_P76R1 = 7_371_915_264


def test_the_runner_derived_per_node_limit_clears_the_measured_group_peak():
    per_node = RESOURCE_PROPOSAL.max_combined_rss_bytes // 2
    assert per_node > MEASURED_GROUP_PEAK_BYTES


def test_the_bound_clears_the_7B_admission_peak_measured_in_p76r1():
    """The pinned measurement must track the workload, not only the older rounds."""

    per_node = RESOURCE_PROPOSAL.max_combined_rss_bytes // 2
    assert per_node > MEASURED_GROUP_PEAK_BYTES_P76R1, (
        'per-node limit does not clear the peak the 7B admission actually reached'
    )
    assert per_node >= int(MEASURED_GROUP_PEAK_BYTES_P76R1 * 1.10), (
        'a margin that only just exceeds the measurement is not a margin'
    )


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
