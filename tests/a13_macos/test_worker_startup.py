# SPDX-License-Identifier: AGPL-3.0-only
"""Real no-model launch with one transient post-exec inventory observation."""
import pytest
import mycelium_node.process as native
from test_worker import worker


def test_transient_post_exec_inventory_retries_same_owned_child(tmp_path,monkeypatch):
    original_await=native._await_target_exec
    original_inventory=native._inventory_process
    state={'released':False,'failed':False,'pid':None}
    def completed(*args,**kwargs):
        result=original_await(*args,**kwargs)
        state['released']=True
        return result
    def inventory(pid):
        if state['released'] and not state['failed']:
            state['failed']=True;state['pid']=pid
            raise ProcessLookupError(pid)
        return original_inventory(pid)
    monkeypatch.setattr(native,'_await_target_exec',completed)
    monkeypatch.setattr(native,'_inventory_process',inventory)
    w=worker(tmp_path)
    try:
        result=w.start()
        assert result['ok'] is True
        assert state['failed'] is True and w.pid==state['pid']
    finally:w.close()
    assert not w.process_group_present()
