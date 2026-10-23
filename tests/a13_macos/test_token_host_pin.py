# SPDX-License-Identifier: AGPL-3.0-only
"""Local host pin crosses real CPU native startup; no model load."""
import json
import time
import pytest

from mycelium_qualification.signing import generate_ed25519_signer
from test_worker import worker


def pin_file(tmp_path):
    owner=generate_ed25519_signer(endpoint_id='owner')
    node=generate_ed25519_signer(endpoint_id='node')
    doc=dict(protocol='mycelium.token_path_host_pin.v1',run_id='run-test',deployment_id='deployment-test',
        node_id='mac-test',owner_endpoint_id=owner.endpoint_id,owner_key=owner.public_key_record(),
        member=dict(node_id='mac-test',endpoint_id=node._public_key_bytes.hex(),generation=1,
            control_endpoint_id=node.endpoint_id,verification_key=node.public_key_record()),
        process_deadline_monotonic_s=time.monotonic()+60,lease_expires_at_unix_ms=int(time.time()*1000)+60000,
        clock_uncertainty_ms=100)
    path=tmp_path/'host.private.json';path.write_text(json.dumps(doc));path.chmod(0o600)
    return path


def test_worker_passes_only_host_pin_path_and_native_loads_it(tmp_path):
    path=pin_file(tmp_path)
    w=worker(tmp_path,token_policy_trust_file=path)
    argv=w._native_argv()
    assert argv[argv.index('--token-policy-trust-file')+1]==str(path)
    try:
        assert w.start()['ok'] is True
        # Trust alone is not a running/loaded route, or a request grant.
        assert w.status()['model_load_authorized'] is False
        with pytest.raises(RuntimeError,match='worker_command_invalid'):
            w.send(command_id='install',command='install_token_path',payload={'policy':{}})
    finally:w.close()
    assert not w.process_group_present()


def test_installed_member_change_blocks_worker_even_if_membership_advanced(tmp_path):
    from types import SimpleNamespace
    w=worker(tmp_path)
    w._token_policy=SimpleNamespace(current=lambda:False)
    with pytest.raises(RuntimeError,match='worker_membership_not_current'):
        w._check()


def test_worker_default_has_no_token_host_pin(tmp_path):
    w=worker(tmp_path)
    assert '--token-policy-trust-file' not in w._native_argv()
