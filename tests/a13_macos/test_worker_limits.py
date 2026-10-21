# SPDX-License-Identifier: AGPL-3.0-only
"""Additional real-native worker failure controls; no model weights opened."""
import time
import pytest
from test_worker import worker
from mycelium_a13_macos.worker import WorkerError, WorkerLimits

def wait_closed(w):
    deadline=time.monotonic()+8
    while (w.status()['running'] or w.process_group_present()) and time.monotonic()<deadline:time.sleep(.1)
    assert not w.status()['running']
    assert not w.process_group_present()

@pytest.mark.parametrize('returned',[False,None,1])
def test_non_true_load_approval_cannot_reach_native_configure(tmp_path,returned):
    w=worker(tmp_path,authorize_load=lambda _:returned)
    try:
        w.start()
        with pytest.raises(WorkerError,match='model_execution_not_authorized'):
            w.send(command_id='load',command='configure',payload={})
        assert w.status()['model_load_authorized'] is False
        assert w.status()['command_count']==0
    finally:w.close()

def test_real_resident_memory_over_limit_rejects_start_and_cleans(tmp_path):
    w=worker(tmp_path,limits=WorkerLimits(max_rss_bytes=1,max_disk_bytes=10000000,max_wall_seconds=30,max_commands=8))
    with pytest.raises(WorkerError,match='worker_memory_limit_exceeded'):w.start()
    assert w.pid is not None
    wait_closed(w)

def test_disk_growth_stops_worker_without_deleting_evidence(tmp_path):
    w=worker(tmp_path)
    try:
        w.start()
        retained=w.artifact_root/'retained-evidence'
        with retained.open('wb') as f:f.truncate(w.limits.max_disk_bytes+1)
        wait_closed(w)
        assert w.status()['error_code']=='worker_disk_limit_exceeded'
        assert retained.stat().st_size==w.limits.max_disk_bytes+1
    finally:w.close()

def test_budget_counts_commands_and_forbids_ambiguous_retry(tmp_path):
    w=worker(tmp_path,limits=WorkerLimits(max_rss_bytes=2000000000,max_disk_bytes=10000000,max_wall_seconds=30,max_commands=1))
    try:
        w.start()
        assert w.send(command_id='first',command='hello',payload={})['ok']
        with pytest.raises(WorkerError,match='worker_command_reconciliation_required'):
            w.send(command_id='first',command='hello',payload={})
        with pytest.raises(WorkerError,match='worker_command_limit_exceeded'):
            w.send(command_id='second',command='hello',payload={})
    finally:w.close()

def test_wall_deadline_stops_native_process(tmp_path):
    w=worker(tmp_path)
    try:
        w.start()
        # Deterministic elapsed-time injection; the killed node itself is real.
        w._started_at-=w.limits.max_wall_seconds
        wait_closed(w)
        assert w.status()['error_code']=='worker_wall_limit_exceeded'
    finally:w.close()


def test_membership_rechecked_after_load_authorization(tmp_path):
    current=[True]
    def approve(_):
        current[0]=False
        return True
    w=worker(tmp_path,membership_current=lambda:current[0],authorize_load=approve)
    try:
        w.start()
        with pytest.raises(WorkerError,match='worker_membership_not_current'):
            w.send(command_id='load',command='configure',payload={})
        assert w.status()['command_count']==0
        assert w.status()['model_load_authorized'] is False
    finally:w.close()

def test_missing_artifact_root_rejects_before_spawn(tmp_path):
    w=worker(tmp_path)
    w.artifact_root.rmdir()
    with pytest.raises(WorkerError,match='worker_storage_observation_failed'):w.start()
    assert w.pid is None


def test_deadline_is_independent_of_a_blocked_policy_observer(tmp_path):
    import threading
    from test_worker import SAFE
    entered=threading.Event();release=threading.Event();block=[False]
    def observation():
        if block[0]:entered.set();release.wait(15)
        return dict(SAFE)
    w=worker(tmp_path,_test_observer=observation,
        limits=WorkerLimits(max_rss_bytes=2000000000,max_disk_bytes=10000000,max_wall_seconds=12,max_commands=8))
    try:
        w.start();block[0]=True
        # The wall deadline is armed AFTER the handshake, for the whole remaining
        # budget (12 s minus the 10 s cleanup reserve).  If the spawn+hello were
        # still charged against that budget, the timer would already have fired
        # here -- the node would be closing, and the blocked observer would never
        # be reached at all.
        timer=w._deadline_timer
        assert timer is not None and 0 < timer.interval <= w.limits.max_wall_seconds - 10
        assert w.status()['running'] is True and not w._closed.is_set()
        assert entered.wait(3)
        wait_closed(w)
        assert w.status()['error_code']=='worker_wall_limit_exceeded'
    finally:release.set();w.close()
