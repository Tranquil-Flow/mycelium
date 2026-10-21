# SPDX-License-Identifier: AGPL-3.0-only
"""Translated HTTP errors must not leave their response owned only by GC."""
from __future__ import annotations
import io,unittest,urllib.error
from types import SimpleNamespace
from unittest.mock import patch
from scripts import run_a5_benchmark_gate as gate

class Response(io.BytesIO):
 def __init__(self,*,close_error=None):
  super().__init__(b'PRIVATE-ERROR-BODY');self.close_calls=0;self.read_calls=0;self.close_error=close_error
 def close(self):
  self.close_calls+=1
  if self.close_error:raise self.close_error
  return super().close()
 def read(self,*args):
  self.read_calls+=1;raise AssertionError('error body must not be read')

class HttpErrorCleanupTests(unittest.TestCase):
 def session(self,opener):
  value=object.__new__(gate.ProductSession);value.base_url='http://127.0.0.1:1'
  value._opener=opener;value._control_opener=opener
  return value
 def test_http_error_closed_before_status_translation(self):
  for control in (False,True):
   for method,status in [('GET',404),('POST',429),('POST',503)]:
    with self.subTest(control=control,method=method,status=status):
     fp=Response();error=urllib.error.HTTPError('http://127.0.0.1:1/fake',status,'fixture',{},fp)
     def fail(*args,**kwargs):raise error
     try:
      with self.assertRaises(gate.GateError) as raised:
       self.session(SimpleNamespace(open=fail)).request(method,'/fake',control=control)
      self.assertEqual(str(raised.exception),f'http_{method.lower()}_{status}')
      self.assertIs(raised.exception.__cause__,error)
      self.assertEqual(fp.close_calls,1);self.assertTrue(fp.closed);self.assertEqual(fp.read_calls,0)
     finally:
      error.close();fp.close_error=None
      if not fp.closed:io.BytesIO.close(fp)
 def test_cleanup_failure_preserves_primary_http_status(self):
  for close_error in (OSError('PRIVATE-CLOSE-ERROR'),RuntimeError('PRIVATE-CLOSE-ERROR')):
   with self.subTest(kind=type(close_error).__name__):
    fp=Response(close_error=close_error)
    error=urllib.error.HTTPError('http://127.0.0.1:1/fake',429,'fixture',{},fp)
    def fail(*args,**kwargs):raise error
    try:
     with self.assertRaises(gate.GateError) as raised:
      self.session(SimpleNamespace(open=fail)).request('POST','/fake')
     self.assertEqual(str(raised.exception),'http_post_429');self.assertIs(raised.exception.__cause__,error)
     self.assertEqual(fp.close_calls,1);self.assertEqual(fp.read_calls,0)
     self.assertNotIn('PRIVATE-',str(raised.exception))
    finally:
     fp.close_error=None;error.close()
     if not fp.closed:io.BytesIO.close(fp)
 def test_sibling_refusal_helpers_release_error_response(self):
  for helper,code in [('public','public_status_unavailable'),('install','qualification_install_http_503')]:
   for failure in (None,OSError('PRIVATE-CLOSE-ERROR')):
    with self.subTest(helper=helper,close_fails=failure is not None):
     fp=Response(close_error=failure)
     error=urllib.error.HTTPError('http://127.0.0.1:1/fake',503,'fixture',{},fp)
     try:
      with patch.object(gate.urllib.request,'urlopen',side_effect=error),patch.dict(gate.os.environ,{'MYCELIUM_A5_OPERATOR_TOKEN':'fixture-only-'+'x'*32}):
       with self.assertRaises(gate.GateError) as raised:
        if helper=='public':gate.public_json('http://127.0.0.1:1','/fake')
        else:gate.install_replica_qualification('http://127.0.0.1:1',[])
      self.assertEqual(str(raised.exception),code);self.assertIs(raised.exception.__cause__,error)
      self.assertEqual(fp.close_calls,1);self.assertEqual(fp.read_calls,0)
      if failure is None:self.assertTrue(fp.closed)
     finally:
      fp.close_error=None;error.close()
      if not fp.closed:io.BytesIO.close(fp)
 def test_successful_response_remains_caller_owned(self):
  fp=Response()
  try:
   result=self.session(SimpleNamespace(open=lambda *a,**kw:fp)).request('GET','/fake')
   self.assertIs(result,fp);self.assertFalse(fp.closed);self.assertEqual(fp.close_calls,0)
  finally:fp.close()

if __name__=='__main__':unittest.main()
