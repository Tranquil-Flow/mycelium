# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise http_verify.py against an isolated real checkout, never a live job.

Uses ephemeral loopback ports, an in-memory directory and a dummy capability.
No model, relay, live database, LaunchAgent or public origin is contacted.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

CHILD = '''
import json, sys, threading
from mycelium_swarm_directory.crypto import verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.server import DirectoryServer
from mycelium_swarm_directory.store import MemoryStore
from mycelium_demo.public_origin_proxy import ProxyConfig, PublicOriginProxyServer
directory=DirectoryServer(Directory(MemoryStore(),verify)).start()
cfg=ProxyConfig(listen_host='127.0.0.1',listen_port=4350,upstream_host='127.0.0.1',upstream_port=1,
                public_host='mycelium.now',capability='a'*64,directory_host='127.0.0.1',
                directory_port=directory.server_port,redirect_root_to_swarms=sys.argv[1]=='rollback')
proxy=PublicOriginProxyServer(('127.0.0.1',0),cfg)
thread=threading.Thread(target=proxy.serve_forever,daemon=True);thread.start()
try:
 print(json.dumps({'origin':'http://127.0.0.1:'+str(proxy.server_port)}),flush=True)
 sys.stdin.readline()
finally:
 proxy.shutdown();proxy.server_close();thread.join(5);directory.close()
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('--release',type=Path,required=True)
    parser.add_argument('--mode',choices=('candidate','rollback'),required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    xattr=subprocess.run(['/usr/bin/xattr','-p','com.apple.quarantine',sys.executable],capture_output=True,text=True)
    if xattr.returncode==0 or 'No such xattr' not in xattr.stderr:
        raise RuntimeError('Quarantined or uninspectable Python')
    env=dict(os.environ)
    for key in ('PYTHONPATH','MYCELIUM_PHYSICAL_SERVICE_PYTHON'):env.pop(key,None)
    child=subprocess.Popen([sys.executable,'-B','-c',CHILD,args.mode],cwd=args.release,env=env,
                           stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    assert child.stdout and child.stderr
    try:
        line=child.stdout.readline()
        if not line: raise RuntimeError('Loopback child failed: '+child.stderr.read())
        origin=json.loads(line)['origin']
        sys.path.insert(0,str(args.release))
        spec=importlib.util.spec_from_file_location('site_http_verify',Path(__file__).with_name('http_verify.py'))
        assert spec and spec.loader
        verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)
        result=verifier.verify(args.release,args.mode,origin,client_ip='203.0.113.7')
        result['qualification']='real checkout, ephemeral loopback, no launchd or public traffic'
        args.output.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2))
        return 0 if result['ok'] else 1
    finally:
        child.communicate('\n',timeout=10)


if __name__=='__main__':
    sys.exit(main())
