# SPDX-License-Identifier: AGPL-3.0-only
"""Optional one-click execution ceiling, bound to one OwnerRun before Start.

The automatic chain checks the actual compiled proposal synchronously before
publishing any work announcement. This is not a polling client or a substitute
for the runtime's own approval and resource checks.
"""
from collections.abc import Mapping

# Review-only A13 rehearsal envelope. Never silently increase these limits.
REHEARSAL_MAX = {
    'max_execution_download_bytes': 0,
    'max_combined_rss_bytes': 8589934592,
    'max_additional_disk_bytes': 8589934592,
    'max_wall_seconds': 1800,
    'run_count': 1,
}


def checked_ceiling(value):
    """Accept an exact, bounded request body; bool/float/string are not ints."""
    if not isinstance(value, dict) or set(value) != set(REHEARSAL_MAX):
        raise ValueError('execution_ceiling_invalid')
    for key, upper in REHEARSAL_MAX.items():
        observed = value[key]
        if type(observed) is not int or observed < 0 or observed > upper:
            raise ValueError('execution_ceiling_invalid')
        if key not in {'max_execution_download_bytes', 'run_count'} and observed == 0:
            raise ValueError('execution_ceiling_invalid')
    if value['run_count'] != 1:
        raise ValueError('execution_ceiling_invalid')
    return dict(value)


def proposal_within_ceiling(proposal, ceiling):
    try:
        ceiling = checked_ceiling(ceiling)
    except ValueError:
        return False
    if not isinstance(proposal, Mapping):
        return False
    return all(key in proposal and type(proposal[key]) is int and
               0 <= proposal[key] <= bound and
               (key == 'max_execution_download_bytes' or proposal[key] > 0) and
               (key not in {'max_execution_download_bytes', 'run_count'} or
                proposal[key] == bound)
               for key, bound in ceiling.items())
