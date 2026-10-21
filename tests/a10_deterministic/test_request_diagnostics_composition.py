# SPDX-License-Identifier: AGPL-3.0-only
"""Real ProductSession composition with an intercepted urllib opener, no sockets."""
import io,json,urllib.error,urllib.parse
from email.message import Message
import pytest
from tests.a10_deterministic import test_request_diagnostics as cases

@pytest.mark.parametrize('mode',['completed','post429','get404'])
def test_stock_session_request_and_error_composition(monkeypatch,mode):
 calls=[]
 class Opener:
  def open(self,request,*,timeout):
   method=request.get_method();path=urllib.parse.urlsplit(request.full_url).path
   calls.append((method,path));assert timeout>0
   if path=='/api/v1/bootstrap':
    body={'session':{'csrf_header':'X-Test-CSRF','csrf_token':cases.CANARY}}
   elif path=='/api/v1/qualification/current':body={'binding':{}}
   elif method=='POST':
    assert path=='/api/v1/inference'
    assert request.get_header('X-test-csrf')==cases.CANARY
    assert json.loads(request.data)['protocol']=='mycelium.request_gateway.v2'
    if mode=='post429':raise urllib.error.HTTPError(request.full_url,429,cases.CANARY,Message(),None)
    body={'request_id':'request-7','event_path':'/events?private='+cases.CANARY}
   else:
    assert method=='GET' and path=='/events'
    if mode=='get404':raise urllib.error.HTTPError(request.full_url,404,cases.CANARY,Message(),None)
    return io.BytesIO(b'event: token\ndata: '+cases.CANARY.encode()+b'\nevent: completed\n')
   return io.BytesIO(json.dumps(body).encode())
 monkeypatch.setattr(cases.a5.urllib.request,'build_opener',lambda *_handlers:Opener())
 monkeypatch.setattr(cases.a5.urllib.request,'urlopen',lambda *_a,**_k:pytest.fail('unintercepted transport'))
 product=cases.gate.ProductClient('http://invalid.example',diagnostic_limit=4)
 assert type(product._session) is cases.a5.ProductSession  # not the fake-session adapter
 result=product.run(cases.SHAPE);observed=cases.row(product)
 assert calls[:2]==[('GET','/api/v1/bootstrap'),('GET','/api/v1/qualification/current')]
 if mode=='post429':
  assert result.outcome=='overflowed' and observed.stage=='submit_error' and observed.status==429
 elif mode=='get404':
  assert result.outcome=='failed' and observed.stage=='stream_exception' and observed.status==404
 else:
  assert result.outcome=='completed' and observed.stage=='terminal' and observed.terminal_kind=='completed'
 assert cases.CANARY not in json.dumps(observed.__dict__)
 assert 'invalid.example' not in repr(observed)
