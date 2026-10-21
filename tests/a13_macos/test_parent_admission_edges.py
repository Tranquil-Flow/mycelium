# SPDX-License-Identifier: AGPL-3.0-only
import pytest
from mycelium_a13_macos.execution_admission import proposal_within_ceiling,REHEARSAL_MAX
@pytest.mark.parametrize('field',['max_combined_rss_bytes','max_additional_disk_bytes','max_wall_seconds'])
def test_zero_resource_proposal_is_malformed(field):
    assert not proposal_within_ceiling(dict(REHEARSAL_MAX,**{field:0}),REHEARSAL_MAX)
def test_missing_private_ceiling_cannot_vacuously_admit():
    assert not proposal_within_ceiling(REHEARSAL_MAX,{})
