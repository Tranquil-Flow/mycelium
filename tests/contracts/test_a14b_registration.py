# SPDX-License-Identifier: AGPL-3.0-only
"""Registration of the existing A14b evidence record, not physical authority."""
import copy
import json
from pathlib import Path

import pytest

from scripts.contract_registry import CONTRACT_SPECS
from mycelium_product_spine.coarse_region import (
    EVIDENCE_PROTOCOL, PROTOCOL, REGIONS, digest, validate_coarse_region_evidence,
)

ROOT = Path(__file__).resolve().parents[2]
NAME = 'coarse-region-evidence-v1.json'


def test_existing_evidence_is_registered_and_generated():
    from scripts.generate_contract_fixtures import documents
    specs = [s for s in CONTRACT_SPECS if s.protocol == EVIDENCE_PROTOCOL]
    assert len(specs) == 1
    assert specs[0].fixture_name == NAME
    assert {'mycelium_product_spine/coarse_region.py', 'ui/web/src/features/routeExplorer/contracts.ts'} <= set(specs[0].owner_sources)
    document = documents()[NAME]
    assert document == json.loads((ROOT / 'contracts/compatibility-fixtures' / NAME).read_text())
    assert validate_coarse_region_evidence(document) == document
    assert document['vocabulary_version'] == PROTOCOL
    assert document['region'] == 'unknown'
    assert set(REGIONS) == {'africa', 'americas_north', 'americas_south', 'asia', 'europe', 'oceania', 'polar', 'unknown'}
    assert document['digest'] == digest({k: v for k, v in document.items() if k != 'digest'})


@pytest.mark.parametrize('change', [
    {'hostname': 'private.example'}, {'region': 'us-east-1'},
    {'subject_ref': '192.0.2.1'}, {'source_kind': 'latency_distance_class'},
    {'issued_at_unix_ms': True}, {'expires_at_unix_ms': -1},
])
def test_registered_fixture_rejects_invalid_or_private_mutations(change):
    document = json.loads((ROOT / 'contracts/compatibility-fixtures' / NAME).read_text())
    bad = copy.deepcopy(document)
    bad.update(change)
    bad['digest'] = digest({k: v for k, v in bad.items() if k != 'digest'})
    with pytest.raises(ValueError):
        validate_coarse_region_evidence(bad)
