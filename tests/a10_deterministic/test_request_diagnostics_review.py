# SPDX-License-Identifier: AGPL-3.0-only
"""Parent controls for truthful client-side observations only."""
import pytest
from tests.a10_deterministic import test_request_diagnostics as cases

def test_submit_timeout_does_not_claim_server_refusal(monkeypatch):
 product=cases.client(monkeypatch,cases.FakeSession(submit_error=TimeoutError('private timeout')),diagnostic_limit=4)
 assert product.run(cases.SHAPE).outcome=='failed'
 observed=cases.row(product)
 assert observed.stage=='submit_error' and observed.status is None

def test_observed_terminal_survives_stream_close_error(monkeypatch):
 class BrokenClose(cases.FakeResponse):
  def __exit__(self,*_args):raise OSError('private cleanup error')
 session=cases.FakeSession()
 def broken_request(method,path):
  assert method=='GET' and path==session.accepted['event_path']
  return BrokenClose([b'event: completed\n'])
 session.request=broken_request
 product=cases.client(monkeypatch,session,diagnostic_limit=4)
 assert product.run(cases.SHAPE).outcome=='failed'  # preserve original scoring
 observed=cases.row(product)
 assert observed.stage=='stream_exception'
 assert observed.terminal_kind=='completed'  # observation is not a product-success claim

@pytest.mark.parametrize('limit',[-1,4097,True,False,1.5,'8',None])
def test_invalid_diagnostic_limits_refused_before_session_creation(monkeypatch,limit):
 def forbidden(_url):pytest.fail('session constructed before validation')
 monkeypatch.setattr(cases.a5,'ProductSession',forbidden)
 with pytest.raises(ValueError,match='diagnostic_limit_invalid'):
  cases.gate.ProductClient('http://invalid.example',diagnostic_limit=limit)
