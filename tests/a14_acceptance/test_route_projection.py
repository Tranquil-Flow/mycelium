# SPDX-License-Identifier: AGPL-3.0-only
"""Local deterministic conformance, not physical route evidence."""
import copy
import pytest
from mycelium_product_spine.projector import ProductProjector
def project_route_explorer(value):
    from mycelium_product_spine.route_explorer import project_route_explorer as project
    return project(value)


def validate_route_explorer(value):
    from mycelium_product_spine.route_explorer import validate_route_explorer as validate
    return validate(value)


def snapshot():
    return ProductProjector(pseudonym_salt=b'a' * 32).project(
        members=[], route_status=None, qualification=None,
        now_unix_ms=1000, source_mode='fixture')


def test_production_projector_emits_closed_explorer():
    s = snapshot()
    p = s['route_explorer']
    assert p['protocol'] == 'mycelium.route_explorer.v1'
    assert p['source_mode'] == 'fixture'
    assert p['nodes'] == []
    assert p['segments'] == []
    assert validate_route_explorer(p) == p


def test_projection_is_deterministic_read_only_and_unknown():
    s = snapshot()
    before = copy.deepcopy(s)
    assert project_route_explorer(s) == project_route_explorer(s)
    assert s == before
    assert project_route_explorer(s)['overlays'] == 'unavailable'


@pytest.mark.parametrize('key,value', [('latitude', 1), ('hostname', 'private.example'), ('prompt', 'private')])
def test_closed_projection_rejects_private_fields(key, value):
    p = snapshot()['route_explorer']
    p[key] = value
    with pytest.raises(ValueError):
        validate_route_explorer(p)


def test_digest_tampering_rejected():
    p = snapshot()['route_explorer']
    p['generation'] += 1
    with pytest.raises(ValueError):
        validate_route_explorer(p)
