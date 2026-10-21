# SPDX-License-Identifier: AGPL-3.0-only
"""Managed native worker boundary; real hello/stop, never model configure."""
from pathlib import Path
import os
import subprocess as _subprocess
import time
import pytest
import mycelium_a13_macos.worker as worker_module
from mycelium_a13_macos.worker import ManagedWorker, WorkerLimits, WorkerError

POLICY={'power':'ac_only','network':'unmetered_only','thermal':'stop_on_serious'}
SAFE={'power':'external','network':'unmetered','thermal':'nominal'}

def worker(tmp_path, **changes):
    artifacts=tmp_path/'artifacts';artifacts.mkdir()
    kwargs=dict(node_id='mac-test',run_id='run-test',deployment_id='deployment-test',
        artifact_root=artifacts,socket_root=tmp_path/'socket',sidecar_binary=tmp_path/'not-started',
        resource_policy=POLICY,limits=WorkerLimits(max_rss_bytes=2_000_000_000,max_disk_bytes=10_000_000,max_wall_seconds=30,max_commands=8),
        membership_current=lambda:True,authorize_load=None,_test_observer=lambda:dict(SAFE))
    kwargs.update(changes)
    return ManagedWorker(**kwargs)

def test_real_native_hello_and_stop_without_model_load(tmp_path):
    w=worker(tmp_path)
    try:
        hello=w.start()
        assert hello['ok'] is True
        assert hello['route_ready'] is False
        assert w.status()['running'] is True
        assert w.status()['model_load_authorized'] is False
        with pytest.raises(WorkerError,match='model_execution_not_authorized'):
            w.send(command_id='configure-test',command='configure',payload={})
        with pytest.raises(WorkerError,match='worker_command_not_allowed'):
            w.send(command_id='evil-test',command='exec',payload={'argv':['touch','no']})
    finally:w.close()
    assert w.status()['running'] is False
    assert w.process_group_present() is False

def test_membership_rejection_precedes_process_creation(tmp_path):
    w=worker(tmp_path,membership_current=lambda:False)
    with pytest.raises(WorkerError,match='worker_membership_not_current'):w.start()
    assert w.pid is None

def test_unknown_resource_policy_precedes_process_creation(tmp_path):
    w=worker(tmp_path,_test_observer=lambda:{'power':'unknown','thermal':'unknown','network':'unknown'})
    with pytest.raises(WorkerError,match='resource_power_policy_violation'):w.start()
    assert w.pid is None

def test_revoked_membership_stops_real_native_process(tmp_path):
    current=[True]
    w=worker(tmp_path,membership_current=lambda:current[0])
    try:
        w.start();current[0]=False
        deadline=time.monotonic()+8
        while w.status()['running'] and time.monotonic()<deadline:time.sleep(.1)
        assert w.status()['running'] is False
        assert w.status()['error_code']=='worker_membership_not_current'
        assert w.process_group_present() is False
    finally:w.close()

def test_policy_change_stops_real_native_process(tmp_path):
    observation=dict(SAFE)
    w=worker(tmp_path,_test_observer=lambda:dict(observation))
    try:
        w.start();observation['thermal']='serious'
        deadline=time.monotonic()+8
        while w.status()['running'] and time.monotonic()<deadline:time.sleep(.1)
        assert w.status()['running'] is False
        assert w.status()['error_code']=='resource_thermal_policy_violation'
    finally:w.close()

@pytest.mark.parametrize('field,value',[('max_rss_bytes',True),('max_disk_bytes',0),('max_wall_seconds',-1),('max_commands',0)])
def test_invalid_limits_fail_closed(field,value):
    values=dict(max_rss_bytes=1000,max_disk_bytes=1000,max_wall_seconds=10,max_commands=5)
    values[field]=value
    with pytest.raises(WorkerError,match='worker_limits_invalid'):WorkerLimits(**values)

# ---------------------------------------------------------------------------
# Memory observation robustness (batch-2 L2 2026-09-21, load1 ~57).
#
# Receipts: runs/v2/l2-batch2-launch-r2/launch/attempts/61257b25*/receipts/pytest.log
# Two healthy workers died with `worker_memory_observation_failed` on the
# `subprocess.TimeoutExpired` (deadline_monotonic_s is None) branch of
# `_group_rss`: at load1 ~57 a `/bin/ps -axo pid=,pgid=,rss=` call stalled past
# the flat 2 s per-call bound.  Every other observation in the same run (the
# a13_macos worker suite starts many real workers) returned in ~50 ms, so the
# stall is transient host contention, and one stalled call must not be fatal.
# ---------------------------------------------------------------------------
_PS_ARGV=('/bin/ps','-axo','pid=,pgid=,rss=')

class FakePs:
    """Deterministic stand-in for subprocess.run: `failures` TimeoutExpired first.

    Records the argv and the per-attempt timeout so a test can prove that the
    absolute /bin/ps path and a bounded per-attempt bound were used.
    """
    def __init__(self,report,failures=0):
        self.report=report;self.failures=failures;self.calls=0;self.argv=[];self.timeouts=[]
    def __call__(self,argv,**kwargs):
        self.calls+=1;self.argv.append(tuple(argv));self.timeouts.append(kwargs.get('timeout'))
        assert tuple(argv)==_PS_ARGV,argv
        if self.calls<=self.failures:
            raise _subprocess.TimeoutExpired(cmd=argv,timeout=float(kwargs.get('timeout') or 0.0))
        return _subprocess.CompletedProcess(argv,0,self.report,'')

def test_memory_observation_survives_a_transient_ps_stall(tmp_path,monkeypatch):
    w=worker(tmp_path);w.pid=4242
    fake=FakePs('111 4242 5120\n222 9999 1048576\n',failures=1)
    monkeypatch.setattr(worker_module.subprocess,'run',fake)
    assert w._group_rss()==5120*1024
    assert fake.calls==2
    assert all(t==2.0 for t in fake.timeouts)

def test_memory_observation_retries_inside_a_live_command_deadline(tmp_path,monkeypatch):
    w=worker(tmp_path);w.pid=4242
    fake=FakePs('111 4242 5120\n',failures=1)
    monkeypatch.setattr(worker_module.subprocess,'run',fake)
    assert w._group_rss(time.monotonic()+5)==5120*1024
    assert fake.calls==2

def test_memory_observation_fails_closed_when_ps_never_answers(tmp_path,monkeypatch):
    # A bounded tolerance is not a silent pass: exhausting the attempts still
    # refuses, so a host that cannot be observed never looks like zero RSS.
    w=worker(tmp_path);w.pid=4242
    fake=FakePs('',failures=10**6)
    monkeypatch.setattr(worker_module.subprocess,'run',fake)
    with pytest.raises(WorkerError,match='worker_memory_observation_failed'):w._group_rss()
    assert 1<fake.calls<=8

def test_memory_observation_keeps_the_command_deadline(tmp_path,monkeypatch):
    w=worker(tmp_path);w.pid=4242
    fake=FakePs('',failures=10**6)
    monkeypatch.setattr(worker_module.subprocess,'run',fake)
    with pytest.raises(TimeoutError,match='command_deadline_exceeded'):w._group_rss(time.monotonic()+0.05)

def test_memory_observation_parse_surprise_still_fails_closed(tmp_path,monkeypatch):
    # Host load is transient and retryable; a report we cannot parse is our own
    # assumption breaking, so it stays a refusal rather than an under-count.
    w=worker(tmp_path);w.pid=4242
    fake=FakePs('111 4242 5120 and-something-else\n')
    monkeypatch.setattr(worker_module.subprocess,'run',fake)
    with pytest.raises(WorkerError,match='worker_memory_observation_failed'):w._group_rss()
    assert fake.calls==1

def test_memory_observation_sums_only_the_worker_group_in_a_large_report(tmp_path,monkeypatch):
    w=worker(tmp_path);w.pid=4242
    lines=[f'{1000+i} {5000+i} {i}' for i in range(200000)]
    fake=FakePs('\n'.join(lines+['7777 4242 4096','7778 4242 1024'])+'\n')
    monkeypatch.setattr(worker_module.subprocess,'run',fake)
    assert w._group_rss()==5120*1024

def test_memory_observation_reads_a_real_process_group(tmp_path):
    w=worker(tmp_path);w.pid=os.getpgid(0)
    assert w._group_rss()>0
