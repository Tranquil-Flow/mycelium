# A13 macOS HTTP/native-launcher review

**Verdict: BLOCKED for the bounded launcher lifecycle.** The reachable loopback HTTP guards held in isolated fixtures, but four launcher/application integration defects remain. Review scope was only `mycelium_a13_macos/app.py`, `__main__.py`, `web/*`, their HTTP tests, and the published authority interface. `authority.py`, `pairing.py`, TLS internals, serving, model execution, public exposure, and other platforms were not reviewed.

Reviewed lane: `demo/a13-macos`, base/HEAD `b9001e6ac3fc11dd9a16f451426453621b24852a`. The reviewed files were untracked concurrent work. Final reviewed hashes are in **Evidence and scope** below.

## Blockers

### B1 — `close()` is not exception-safe; a stop failure skips every remaining cleanup

**Impact.** `DemoApp.close()` calls `authority.stop()` before server shutdown, launch-file removal, and lock release, with no `finally` (`app.py:208-217`). Any stop exception aborts teardown. In-process observation: the HTTP listener still served `200`, the one-use launch file remained, the lock FD remained held, and a second launch failed `already_running`. `start()` also invokes this unsafe close on startup failure (`app.py:140-141`), while `__main__.py:39-40` does not contain a close failure.

**Reproduction (repo root; fake authority, dynamic loopback port):**

```bash
.venv/bin/python -B - <<'PY'
import http.client, tempfile
from pathlib import Path
from mycelium_a13_macos.app import DemoApp, DemoError
class Bad:
    def __init__(self, root): pass
    def start(self): return self
    def renew(self): pass
    def status(self): return {'membership':'not_joined','route_ready':False}
    def stop(self): raise RuntimeError('fixture_stop_failed')
class Good(Bad):
    def stop(self): pass
with tempfile.TemporaryDirectory(dir='/private/tmp') as td:
    root=Path(td)/'state'; app=DemoApp(root,authority_factory=Bad,check_platform=False).start(); port=app.port
    try:
        try: app.close()
        except RuntimeError as e: print('close_raised',e)
        c=http.client.HTTPConnection('127.0.0.1',port,timeout=3); c.request('GET','/')
        r=c.getresponse(); print('server_after_failed_close',r.status); r.read(); c.close()
        print('launch_file_after_failed_close',app.launch_file.exists())
        print('lock_fd_after_failed_close',app.lock_fd is not None)
        try: DemoApp(root,authority_factory=Good,check_platform=False).start()
        except DemoError as e: print('second_start',e)
    finally:
        app.authority.stop=lambda:None; app.close()
PY
```

Observed: `close_raised fixture_stop_failed`, `server_after_failed_close 200`, launch file `True`, lock FD `True`, second start `already_running`.

**Smallest repair:** make close idempotent and cleanup-unconditional; retain/sanitize the authority-stop failure only after listener shutdown, launch-file removal, lock release, and owned-thread termination are attempted.

### B2 — an authenticated request already queued on `guard` can restart authority after close

**Impact.** `closed` is set before `close()` acquires `guard`, but neither `action()` nor the handler checks it (`app.py:173-206`, `208-217`, `263-265`, `314-317`). A request that passed HTTP authorization and is waiting on the guard can run after teardown. The isolated race returned `200` from `/api/start`, invoked `authority.start()` after close, and left the contradictory state `closed=True, running=True` with the HTTP server gone. A real authority could leave listener/member resources active without a management server.

**Reproduction:**

```bash
.venv/bin/python -B - <<'PY'
import http.client,re,tempfile,threading,time,urllib.parse
from pathlib import Path
from mycelium_a13_macos.app import DemoApp
class A:
    def __init__(self,root): self.starts=0; self.stops=0
    def start(self): self.starts+=1; return self
    def stop(self): self.stops+=1
    def renew(self): pass
    def status(self): return {'membership':'not_joined','route_ready':False}
def post(app,path,cookie):
    c=http.client.HTTPConnection('127.0.0.1',app.port,timeout=4)
    c.request('POST',path,'{}',{'Content-Type':'application/json','Cookie':cookie,'Origin':app.origin,'X-Mycelium-Action':'1'})
    r=c.getresponse(); out=(r.status,r.read().decode()); c.close(); return out
with tempfile.TemporaryDirectory(dir='/private/tmp') as td:
    app=DemoApp(Path(td)/'state',authority_factory=A,check_platform=False).start()
    code=re.search(r'name="launch" value="([^"]+)"',app.launch_file.read_text()).group(1)
    c=http.client.HTTPConnection('127.0.0.1',app.port,timeout=3)
    c.request('POST','/session',urllib.parse.urlencode({'launch':code}),{'Content-Type':'application/x-www-form-urlencoded','Origin':'null'})
    r=c.getresponse(); cookie=r.getheader('Set-Cookie').split(';')[0]; r.read(); c.close()
    print('stop_control',post(app,'/api/stop',cookie)); result=[]
    app.guard.acquire()
    try:
        t=threading.Thread(target=lambda:result.append(post(app,'/api/start',cookie))); t.start(); time.sleep(.15); app.close()
    finally: app.guard.release()
    t.join(4)
    print('late_start_response',result[0][0]); print('closed_running',app.closed.is_set(),app.running)
    print('authority_starts_stops',app.authority.starts,app.authority.stops)
    if app.running: app.authority.stop(); app.running=False
PY
```

Observed: control stop `200`; late start `200`; `closed_running True True`; starts/stops `2 2`.

**Smallest repair:** linearize closing/closed state with request admission under the same guard and fail closed before every status/action publication; then wait for owned request/renewal threads or otherwise prove they cannot publish/restart after closure.

### B3 — the browser's accepted consent object does not match the published authority contract

**Impact.** The UI submits seven policy fields (`web/index.html:7-8`, `web/app.js:71-75`); `consent_valid()` requires exactly those fields and `/api/create` passes them unchanged (`app.py:63-69`, `173-179`). The current authority interface requires exactly three booleans (`docs/handover/A13-MACOS-AUTHORITY.md:36-40`). A strict fake implementing that published boundary produced HTTP `409 action_not_completed` for the only valid browser payload. The existing app test fake accepts any consent dictionary (`tests/a13_macos/test_app.py:13-24`), so the six green app tests do not cover this integration seam.

**Reproduction:**

```bash
.venv/bin/python -B - <<'PY'
import http.client,json,re,tempfile,urllib.parse
from pathlib import Path
from mycelium_a13_macos.app import DemoApp
EXPECTED={'cooperative_peer_visibility_acknowledged','resource_use_acknowledged','contribution_enabled'}
class Strict:
    def __init__(self,root): self.received=None
    def start(self): return self
    def stop(self): pass
    def renew(self): pass
    def status(self): return {'membership':'not_joined','route_ready':False}
    def create_swarm(self,c):
        self.received=set(c)
        if self.received != EXPECTED: raise ValueError('consent_contract_mismatch')
        return self.status()
with tempfile.TemporaryDirectory(dir='/private/tmp') as td:
    app=DemoApp(Path(td)/'state',authority_factory=Strict,check_platform=False).start()
    try:
        code=re.search(r'name="launch" value="([^"]+)"',app.launch_file.read_text()).group(1)
        c=http.client.HTTPConnection('127.0.0.1',app.port,timeout=3)
        c.request('POST','/session',urllib.parse.urlencode({'launch':code}),{'Content-Type':'application/x-www-form-urlencoded','Origin':'null'})
        r=c.getresponse(); cookie=r.getheader('Set-Cookie').split(';')[0]; r.read(); c.close()
        consent={'peer_visibility':True,'power':'ac_only','storage_bytes':2000000000,'network':'unmetered_only','background':False,'diagnostics':False,'thermal':'stop_on_serious'}
        c=http.client.HTTPConnection('127.0.0.1',app.port,timeout=3)
        c.request('POST','/api/create',json.dumps({'consent':consent}),{'Content-Type':'application/json','Cookie':cookie,'Origin':app.origin,'X-Mycelium-Action':'1'})
        r=c.getresponse(); print(r.status,r.read().decode()); c.close()
        print('received',sorted(app.authority.received)); print('documented',sorted(EXPECTED))
    finally: app.close()
PY
```

Observed: `409 {"error":"action_not_completed"}` and disjoint received/documented key sets.

**Smallest repair:** define one closed app→authority consent DTO and test it with a signature/shape-strict fake. Keep resource-policy values, but explicitly map the UI acknowledgement and `contribution_enabled=False` into the authority contract rather than relying on a permissive fake.

### B4 — session expiry leaves no reachable reauthorization path while the launcher owns the lock

**Impact.** Startup creates the launch file once (`app.py:136,143-153`). Successful POST consumes the code and deletes the file (`app.py:291-300`). After the one-hour session expires, authorization correctly returns `401`, but no HTTP/native path recreates a one-use launch file. Starting another launcher is rejected by the existing lock (`app.py:120-124`; `__main__.py:24-30`). Thus a legitimate user must externally terminate the process to recover and cannot use the authenticated Stop/Quit controls. The UI instruction to reopen the private launch file (`web/app.js:53-56`) points to a file that no longer exists.

**Reproduction (expiry accelerated by setting the same monotonic deadline field):**

```bash
.venv/bin/python -B - <<'PY'
import http.client,re,tempfile,time,urllib.parse
from pathlib import Path
from mycelium_a13_macos.app import DemoApp,DemoError
class A:
    def __init__(self,root): pass
    def start(self): return self
    def stop(self): pass
    def renew(self): pass
    def status(self): return {'membership':'not_joined','route_ready':False}
with tempfile.TemporaryDirectory(dir='/private/tmp') as td:
    root=Path(td)/'state'; app=DemoApp(root,authority_factory=A,check_platform=False).start()
    try:
        code=re.search(r'name="launch" value="([^"]+)"',app.launch_file.read_text()).group(1)
        c=http.client.HTTPConnection('127.0.0.1',app.port,timeout=3)
        c.request('POST','/session',urllib.parse.urlencode({'launch':code}),{'Content-Type':'application/x-www-form-urlencoded','Origin':'null'})
        r=c.getresponse(); cookie=r.getheader('Set-Cookie').split(';')[0]; r.read(); c.close()
        app.session_expires=time.monotonic()-1
        c=http.client.HTTPConnection('127.0.0.1',app.port,timeout=3); c.request('GET','/api/status',headers={'Cookie':cookie})
        r=c.getresponse(); print('expired_status',r.status,'launch_file_exists',app.launch_file.exists()); r.read(); c.close()
        try: DemoApp(root,authority_factory=A,check_platform=False).start()
        except DemoError as e: print('relaunch_while_running',e)
    finally: app.close()
PY
```

Observed: `expired_status 401 launch_file_exists False`; `relaunch_while_running already_running`.

**Smallest repair:** provide a native-owner-controlled way to mint a fresh short-lived, one-use launch file for the already-running process without putting credentials in URLs or permitting a second browser automatically.

## Guards that held in the exercised HTTP fixture

- Exact `Host` guard rejected a foreign host; exact same-origin plus `X-Mycelium-Action: 1` rejected cross-origin and headerless mutation (`app.py:237-242`, `268-275`).
- Raw duplicate `Content-Length` and `Transfer-Encoding`+`Content-Length` both returned `400 framing_rejected` before the fake authority's stop counter changed; a valid raw control returned `200` and changed the counter once.
- One-use launch POST returned `303`; replay returned `403`; launch file was removed. A clean second cookie jar and an expired cookie each returned `401`; the original unexpired cookie returned `200`.
- Root, launch HTML, and lock modes observed as `0700`, `0600`, and `0600`; a symlink state root failed `unsafe_state_path` (`app.py:38-60`, `117-124`).
- Static shell contained no launch credential. Responses carried `no-store`, `nosniff`, no-referrer, and CSP `frame-ancestors 'none'` (`app.py:227-235`).

Raw probe summary (fake authority, dynamically allocated loopback port):

```text
modes 0o700 0o600 0o600
first_launch 303 replay 403 launch_exists False
clean_second_browser 401 authorized_browser 200
expired_browser 401
duplicate_cl HTTP/1.1 400 ... framing_rejected; stops 0
te_plus_cl HTTP/1.1 400 ... framing_rejected; stops 0
csrf HTTP/1.1 403 ... origin_rejected; stops 0
valid_control HTTP/1.1 200; stops 1
symlink_root unsafe_state_path
```

## Evidence and scope

Commands run from `/Users/evinova-self/Documents/playground/mycelium-a13-macos-demo`:

```bash
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -B -m pytest tests/a13_macos/test_app.py
# 6 passed in 3.07s

env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV .venv/bin/python -B -m mycelium_a13_macos --preflight
# {"platform":"macos","architecture":"arm64","os_version":"26.5.1","distribution":"unsigned_development_demo"}
```

Reviewed hashes at final source check:

```text
2b24e0fbaa13b52461ece7d28134fc4c292951b45643258e3f589772f094ab86  mycelium_a13_macos/app.py
df2f994ab04a1b148fa3468466f104af0e84b4a648330d0c4c2e56db2dae77a7  mycelium_a13_macos/__main__.py
710d93c68e2c3bc9f9e9ecd4dc51780543e8eff85789bff77a3ba97d7958a2de  mycelium_a13_macos/web/index.html
ab6c0631d57d20a6a0a25b3ebc076587ac5d8f8d9fc890a5460e0ae2682befd3  mycelium_a13_macos/web/app.js
47e9ea4c58dd1c9f8901bcdf9eb65c2452b7a7204da15d24bf1d76a2689f9473  mycelium_a13_macos/web/style.css
3c970fa22de2f9a31a048abfbe9fa6a61bd302b17db3779cd3542b95c4c2aeaf  tests/a13_macos/test_app.py
```

**Evidence boundary:** all HTTP observations used a tiny fake authority and local dynamic loopback ports. Preflight ran on the actual arm64/macOS 26.5.1 host. No default/native browser was opened, so real `file://` form `Origin: null`, browser cookie behavior, Back/Forward/reload, accessibility/responsive behavior, and `webbrowser.open()` handling remain unverified. No real `DemoAuthority`, TLS enrollment, durable identity, renewal/revocation, native serving, model, physical placement, public listener, W6 state, reserved port, payment, ENS, or external service was exercised. This is test-boundary review, not physical-serving or release qualification.
