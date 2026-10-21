# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded packaged app lifetime; only its owned process is stopped."""
import argparse,subprocess,os,json,signal,sys,threading,time
from pathlib import Path
from app_rpc import AppClient
p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--executable',required=True);p.add_argument('--max-seconds',type=int,default=1800);p.add_argument('--resume',action='store_true');a=p.parse_args();assert 30<=a.max_seconds<=1800
root=Path(a.root)
if a.resume:
    assert root.is_dir() and not root.is_symlink() and not (root/'native-control.json').exists()
else:assert not root.exists()
env=os.environ.copy()
for k in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV'):env.pop(k,None)
keepalive=float(env.pop('A13_DAEMON_KEEPALIVE_SECONDS','15'));assert 0.05<=keepalive<=300
child=subprocess.Popen([a.executable,'--headless','--state-root',str(root)],env=env)
# The app also ends with its controlling channel.  Behind ssh a dropped link
# leaves this daemon unaware (two-Mac run 21 counted run 20's daemon and app
# still running; round15_strays.py:3), so write a keepalive line and, once a
# write fails, quit the app.  SIGTERM/SIGHUP stop the app as well.
def stop_app():
    try:AppClient(root).call('/api/quit',{});child.wait(30)
    except Exception:pass
    if child.poll() is None:child.terminate()
def watch_channel():
    while child.poll() is None:
        time.sleep(keepalive)
        try:sys.stdout.write('{"event": "daemon_alive"}\n');sys.stdout.flush()
        except (OSError,ValueError):stop_app();return
def end(signum,_frame):raise SystemExit(128+signum)
signal.signal(signal.SIGTERM,end);signal.signal(signal.SIGHUP,end)
threading.Thread(target=watch_channel,daemon=True).start()
try:child.wait(timeout=a.max_seconds)
except subprocess.TimeoutExpired:
    try:AppClient(root).call('/api/quit',{})
    except Exception:pass
finally:
    if child.poll() is None:
        child.terminate()
        try:child.wait(15)
        except subprocess.TimeoutExpired:child.kill();child.wait(5)
    try:print(json.dumps({'event':'packaged_app_closed','exit_code':child.returncode}),flush=True)
    except (OSError,ValueError):pass
