"""Bounded packaged app lifetime; only its owned process is stopped."""
import argparse,subprocess,os,json
from pathlib import Path
from app_rpc import AppClient
p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--executable',required=True);p.add_argument('--max-seconds',type=int,default=1800);p.add_argument('--resume',action='store_true');a=p.parse_args();assert 30<=a.max_seconds<=1800
root=Path(a.root)
if a.resume:
    assert root.is_dir() and not root.is_symlink() and not (root/'native-control.json').exists()
else:assert not root.exists()
env=os.environ.copy()
for k in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV'):env.pop(k,None)
child=subprocess.Popen([a.executable,'--headless','--state-root',str(root)],env=env)
try:child.wait(timeout=a.max_seconds)
except subprocess.TimeoutExpired:
    try:AppClient(root).call('/api/quit',{})
    except Exception:pass
finally:
    if child.poll() is None:
        child.terminate()
        try:child.wait(15)
        except subprocess.TimeoutExpired:child.kill();child.wait(5)
    print(json.dumps({'event':'packaged_app_closed','exit_code':child.returncode}),flush=True)
