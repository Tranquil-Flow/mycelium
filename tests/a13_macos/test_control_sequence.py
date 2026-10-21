# SPDX-License-Identifier: AGPL-3.0-only
"""Durable signer-wide allocation, legacy migration and fail-closed scope."""
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from mycelium_a13_macos.control_sequence import next_target_sequence
from mycelium_a13_macos.authority import _private_directory, _atomic_private_json


def test_counter_survives_new_run_and_concurrent_allocations(tmp_path):
    root=_private_directory(tmp_path/'target',create=True)
    def allocate(_=None):
        return next_target_sequence(root,signer_key_digest='sha256:'+'a'*64,
            swarm_id='swarm-test',at_least=0)
    assert allocate()==1
    with ThreadPoolExecutor(max_workers=4) as pool:
        values=list(pool.map(allocate,range(12)))
    assert sorted(values)==list(range(2,14))
    assert allocate()==14
    with pytest.raises(ValueError,match='scope_invalid'):
        next_target_sequence(root,signer_key_digest='sha256:'+'b'*64,
            swarm_id='swarm-test',at_least=0)
    assert allocate()==15


def test_counter_migrates_retained_signer_bound_run_counters(tmp_path):
    root=_private_directory(tmp_path/'target',create=True)
    for run,sequence,key in [('old-one',31,'a'),('old-two',76,'a'),('other-signer',999,'b')]:
        pump=_private_directory(root/'managed'/run/'pump',create=True)
        _atomic_private_json(pump/'state.json',{'binding':{'target_key_digest':'sha256:'+key*64},'sequence':sequence})
    assert next_target_sequence(root,signer_key_digest='sha256:'+'a'*64,
        swarm_id='swarm-test',at_least=1)==77
    assert json.loads((root/'managed/old-two/pump/state.json').read_text())['sequence']==76


def test_counter_rejects_unsafe_lock_without_touching_target(tmp_path):
    root=_private_directory(tmp_path/'target',create=True)
    other=tmp_path/'untouched';other.write_text('unchanged')
    (root/'control-sequence.lock').symlink_to(other)
    with pytest.raises(OSError):
        next_target_sequence(root,signer_key_digest='sha256:'+'a'*64,
            swarm_id='swarm-test',at_least=0)
    assert other.read_text()=='unchanged'
    assert not (root/'control-sequence.json').exists()
