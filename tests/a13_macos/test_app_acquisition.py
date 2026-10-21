# SPDX-License-Identifier: AGPL-3.0-only
"""Real private HTTP/authority with an inert acquisition boundary, no weights."""
import json
import threading
import time
import pytest
from mycelium_a13_macos.app import DemoApp
from test_app import request, login
from test_app_authority import CONSENT

GOOD={'power':'external','thermal':'nominal','network':'unmetered'}
GRANT={'download_acknowledged':True,'max_download_bytes':999_604_126,'storage_limit_bytes':2_000_000_000}

class Acquisition:
    def __init__(self, root):
        self.state='idle';self.starts=[];self.cancelled=threading.Event();self.closed=False
    def status(self):
        return {'state':self.state,'downloaded_bytes':17 if self.starts else 0,
                'total_bytes':100,'error_code':None,'model':{}}
    def start(self, consent):
        self.starts.append(dict(consent));self.state='downloading';return self.status()
    def cancel(self):
        self.state='cancelled';self.cancelled.set()
    def close(self): self.cancel();self.closed=True

@pytest.fixture
def owned(tmp_path):
    a=DemoApp(tmp_path/'owner',check_platform=False).start()
    # Only the acquisition and OS-observation seams are replaced, never HTTP,
    # membership, TLS or SQLite. The real acquisition is covered separately.
    if getattr(a,'acquisition',None) is not None:a.acquisition.close()
    acq=Acquisition(a.root/'model-source');a.acquisition=acq
    observation=dict(GOOD)
    a._policy_observer=lambda:dict(observation)
    h=login(a)
    try: yield a,acq,observation,h
    finally:a.close()

def enroll(a,h):
    # These acquisition tests exercise the model-download and lifecycle HTTP
    # surfaces. Create the prerequisite real authority membership through the
    # same product action body but outside test_app.request's 3 s client read
    # bound: first-time TLS material generation is intentionally expensive and
    # was the recorded L2 batch-13 tail timeout, but it is not the contract under
    # test here.
    status=a.action('/api/create',{'consent':CONSENT})
    assert status['membership_state']=='current'

def start(a,h,body=None):
    return request(a,'/api/model/download','POST',body or {'consent':GRANT},h)

def test_progress_and_authorized_start_reach_acquisition_only(owned):
    a,acq,observation,h=owned
    assert start(a,{})[0]==401
    assert start(a,{**h,'Origin':'https://untrusted.invalid'})[0]==403
    assert start(a,h)[0]==400
    assert acq.starts==[]
    enroll(a,h)
    response=start(a,h)
    assert response[0]==200
    assert acq.starts==[GRANT]
    status=json.loads(request(a,'/api/status',headers=h)[2])
    assert status['acquisition']['downloaded_bytes']==17
    assert status['resource_observation']==GOOD
    assert status['role']=='not_yet_serving' and not status['serving']['route_ready']
    assert request(a,'/api/model/cancel','POST',{},h)[0]==200
    assert acq.cancelled.is_set()

@pytest.mark.parametrize('delta',[{'download_acknowledged':False},{'storage_limit_bytes':4_000_000_000},
                                {'max_download_bytes':True},{'extra':'reject'}])
def test_browser_cannot_widen_resource_consent(owned,delta):
    a,acq,_,h=owned;enroll(a,h)
    assert start(a,h,{'consent':{**GRANT,**delta}})[0]==400
    assert acq.starts==[]

@pytest.mark.parametrize('bad',[{'power':'battery'},{'thermal':'serious'},
                               {'thermal':'unknown'},{'network':'metered'},{'network':'unknown'}])
def test_resource_policy_rejects_before_download(owned,bad):
    a,acq,observation,h=owned;enroll(a,h);observation.update(bad)
    assert start(a,h)[0]==400
    assert acq.starts==[]

@pytest.mark.parametrize('operation',['stop','revoke-self','quit'])
def test_lifecycle_fences_active_download(owned,operation):
    a,acq,_,h=owned;enroll(a,h);assert start(a,h)[0]==200
    body={'confirm':True} if operation=='revoke-self' else {}
    assert request(a,'/api/'+operation,'POST',body,h)[0]==200
    assert acq.cancelled.wait(4)
    if operation=='stop':assert start(a,h)[0]==400

def test_live_resource_change_cancels_not_merely_warns(owned):
    a,acq,observation,h=owned;enroll(a,h);assert start(a,h)[0]==200
    observation['network']='metered'
    assert acq.cancelled.wait(5)
    status=json.loads(request(a,'/api/status',headers=h)[2])
    assert status['acquisition_policy_error']=='resource_network_policy_violation'

def test_revoked_authority_cannot_start_new_download(owned):
    a,acq,_,h=owned;enroll(a,h)
    assert request(a,'/api/revoke-self','POST',{'confirm':True},h)[0]==200
    assert start(a,h)[0]==400
    assert acq.starts==[]


def test_storage_policy_ceiling_covers_the_seeded_7b_budget():
    """G2 (D-1): the policy must be able to express the 7B seeded-path storage.

    Derived (runs/wp8-02-g1/g2-derivation.json): laptop seeded 7B snapshot
    15,242,807,270 B + [0,14)+embedding stage copy 7,810,167,792 B =
    23,052,975,062 B <= serving.RESOURCE_PROPOSAL.additional_disk_bytes.  The
    ceiling itself is now the largest per-model storage need
    (model_resources.storage_consent_ceiling: the Qwen3-14B owner download).
    """

    from mycelium_a13_macos.app import consent_valid
    from mycelium_a13_macos.model_resources import storage_consent_ceiling

    policy={'peer_visibility':True,'power':'ac_only','network':'unmetered_only',
            'background':False,'diagnostics':False,'thermal':'stop_on_serious'}
    assert consent_valid({**policy,'storage_bytes':23_052_975_062})
    assert consent_valid({**policy,'storage_bytes':24_854_140_006})
    assert storage_consent_ceiling() == 32_568_709_325 > 24_854_140_006
    assert consent_valid({**policy,'storage_bytes':storage_consent_ceiling()})
    assert not consent_valid({**policy,'storage_bytes':storage_consent_ceiling()+1})
    assert not consent_valid({**policy,'storage_bytes':99_999_999})
