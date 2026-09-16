"""Managed native worker boundary; real hello/stop, never model configure."""
from pathlib import Path
import time
import pytest
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
