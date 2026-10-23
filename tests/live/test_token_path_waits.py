# SPDX-License-Identifier: AGPL-3.0-only
"""Native polling waits obey the installed grant, not a reset command budget."""
import pytest
from tests.live.test_token_path_admission import world, real_prefill_node, payload


@pytest.mark.parametrize('phase',['prefill','first-token','decode'])
@pytest.mark.parametrize('cause',['expiry','cancel'])
def test_native_wait_rechecks_grant_deadline_before_sleep(world,phase,cause):
    n=real_prefill_node(world)
    def revoke():
        if cause=='expiry': world.now=16.0
        else: n._request_controls['request']['cancellation_generation']=1
    def status(_):
        revoke()
        return 'PREFILL' if phase=='prefill' else 'DECODING'
    if phase in {'prefill','first-token'}:
        n.router.start_locked_distributed_prefill=lambda request,sink,**_:request.request_id
        n.router.request_status=status
    else:
        def decode(_):
            revoke()
            return True  # asynchronous runtime admitted, no token yet
        n.router.decode_one_distributed=decode
    code='token_path_grant_expired' if cause=='expiry' else 'token_path_grant_stale'
    with pytest.raises(RuntimeError,match=code):
        n._infer_start(payload(world))
    assert [r['statement']['token_ids'] for r in world.receipts] == ([[1]] if phase=='decode' else [])
