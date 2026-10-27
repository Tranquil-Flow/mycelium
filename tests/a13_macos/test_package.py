# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the built, self-contained Mac app; never a source-mode substitute."""
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import urllib.request
import pytest

from playwright.sync_api import sync_playwright, expect

from mycelium_a13_macos.serving import CANDIDATE_MODEL


@pytest.fixture(autouse=True)
def built_package_required():
    # Certification sets this explicitly in candidate.sh's rehearsal script.
    # An explicitly configured but broken artifact must FAIL, never become a skip.
    configured = os.environ.get('A13_PACKAGE_EXECUTABLE')
    if configured is None:
        pytest.skip('built app prerequisite absent: set A13_PACKAGE_EXECUTABLE to the certified Mycelium A13 Demo executable')
    assert configured and Path(configured).is_file(), 'configured_package_executable_missing'
    assert os.access(configured, os.X_OK), 'configured_package_executable_not_executable'


def test_packaged_canonical_staging_in_clean_directory(tmp_path):
    from test_managed_staging import bundle
    configured=os.environ.get('A13_PACKAGE_EXECUTABLE')
    assert configured, 'explicit_successor_package_required'
    executable=Path(configured)
    root=tmp_path/'mycelium-packaged-stage'
    archive,digest=bundle()
    env=dict(os.environ)
    for key in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV'): env.pop(key,None)
    prepared=subprocess.run([str(executable),'--managed-stage','prepare',str(root),
        'packaged-node',digest,str(len(archive))],input=archive,capture_output=True,
        cwd=tmp_path,env=env,timeout=30)
    assert prepared.returncode==0 and prepared.stderr==b''
    ack=json.loads(prepared.stdout)
    assert ack['archive_digest']==digest
    assert (root/'physical_inference_node.py').read_bytes().startswith(b'# fixture')
    cleaned=subprocess.run([str(executable),'--managed-stage','cleanup',str(root),
        'packaged-node',digest,'-'],capture_output=True,cwd=tmp_path,env=env,timeout=30)
    assert cleaned.returncode==0 and cleaned.stderr==b''
    assert json.loads(cleaned.stdout)['removed'] is True and not root.exists()



def test_packaged_native_sidecar_is_installed_and_executable(tmp_path):
    configured=os.environ.get('A13_PACKAGE_EXECUTABLE')
    assert configured, 'explicit_successor_package_required'
    executable=Path(configured)
    binaries=[p for p in executable.parents[2].rglob('mycelium-iroh-sidecar')
        if p.is_file() and not p.is_symlink()]
    assert len(binaries)==1, 'exactly_one_installed_sidecar_required'
    result=subprocess.run([str(binaries[0]),'--help'],cwd=tmp_path,
        capture_output=True,text=True,timeout=10)
    assert result.returncode==0 and result.stderr==''
    assert '--bootstrap-fd' in result.stdout and '--uds' in result.stdout


@pytest.mark.parametrize('role',['owner','target'])
def test_packaged_native_app_in_clean_working_directory(tmp_path,role):
    default=Path(__file__).resolve().parents[2]/'dist/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo'
    executable=Path(os.environ.get('A13_PACKAGE_EXECUTABLE',str(default)))
    assert executable.is_file(), 'build_the_actual_package_first'
    root=tmp_path/'fresh-install'
    env=dict(os.environ)
    for key in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV'): env.pop(key,None)
    process=subprocess.Popen([str(executable),'--headless','--state-root',str(root)],
        cwd=tmp_path,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
    lines=queue.Queue()
    threading.Thread(target=lambda:lines.put(process.stdout.readline()),daemon=True).start()
    try:
        first=lines.get(timeout=30)
        assert first, 'packaged_launcher_failed_before_start'
        started=json.loads(first)
        assert started['event']=='launcher_started'
        # The packaged fresh-state default must match the tested starter.
        from mycelium_a13_macos.run_model import choice, demo_default_choice
        default=demo_default_choice()
        assert default=='a9-route'
        expected=choice(default).model
        assert started['model_contract']['model_id']==expected.model_id=='Qwen/Qwen2.5-0.5B-Instruct'
        assert started['model_contract']['model_id']!=CANDIDATE_MODEL.model_id
        assert started['model_contract']['resolved_commit']==expected.resolved_commit
        assert started['model_contract']['runtime_quantization']=='int8-weight-only'
        assert started['model_contract']['sampling_seed']==0
        with sync_playwright() as p:
            browser=p.chromium.launch()
            context=browser.new_context()
            page=context.new_page()
            page.goto((root/'open-demo.html').as_uri())
            page.get_by_role('button',name='Open private demo',exact=True).click()
            expect(page.locator('#private')).to_be_visible()
            assert page.locator('#verifier-connect').count()==0
            expect(page.locator('#acquisition-state')).to_contain_text('idle')
            assert page.locator('#download-weights').is_disabled()
            # Execute the bundled native probe, not a source-environment helper.
            bundle=executable.parents[2]
            probes=[p for p in bundle.rglob('resource_probe') if p.is_file() and not p.is_symlink()]
            assert probes, 'native_policy_probe_missing_from_package'
            probe=subprocess.run([str(probes[0])],capture_output=True,text=True,timeout=4,check=True)
            observed=json.loads(probe.stdout)
            assert set(observed)=={'power','thermal','network'}
            assert observed['thermal'] in {'nominal','fair','serious','critical'}
            page.locator('#visibility').check()
            page.locator('#create' if role=='owner' else '#join').click()
            if role=='owner':
                expect(page.locator('#ladder')).to_contain_text('current',timeout=20000)
                key=root/'authority/target/identity/node.key'
            else:
                expect(page.locator('#pair-output')).to_be_visible(timeout=20000)
                key=root/'target/identity/node.key'
                assert not list(root.rglob('*.sqlite3'))
                assert not list(root.rglob('seed.key'))
            expected='current' if role=='owner' else 'absent'
            original=hashlib.sha256(key.read_bytes()).hexdigest()
            page.reload();expect(page.locator('#ladder')).to_contain_text(expected)
            page.locator('#stop').click();expect(page.locator('#ladder')).to_contain_text('Stopped')
            page.locator('#start').click();expect(page.locator('#ladder')).to_contain_text('Started')
            expect(page.locator('#ladder')).to_contain_text(expected)
            assert hashlib.sha256(key.read_bytes()).hexdigest()==original
            assert page.locator('#submit').is_disabled()
            assert key.stat().st_mode & 0o777 == 0o600
            page.locator('#quit').click()
            process.wait(timeout=15)
            assert process.returncode==0
            assert key.exists()
            assert not (root/'open-demo.html').exists()
            browser.close()
        assert process.stderr.read()=='', 'packaged_stderr_must_be_empty'
    finally:
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill();process.wait(timeout=10)


def test_packaged_appleevent_quit_runs_cleanup(tmp_path):
    """Headless certification gate: address only the child we started, never a bundle ID."""
    import fcntl
    import socket
    from native_quit_sender import send_quit
    configured = os.environ.get('A13_PACKAGE_EXECUTABLE')
    assert configured, 'explicit_successor_package_required'
    root = tmp_path / 'appleevent-state'
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
        env.pop(key, None)
    process = subprocess.Popen([configured, '--headless', '--state-root', str(root)],
                               cwd=tmp_path, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        assert json.loads(lines.get(timeout=30))['event'] == 'launcher_started'
        # Never print the native-control document: it carries private credentials.
        control = json.loads((root / 'native-control.json').read_text())
        port = control['port']
        assert isinstance(port, int) and 0 < port < 65536
        assert (root / 'open-demo.html').exists()
        retained = root / 'quit-evidence.txt'
        retained.write_text('retain private state')
        send_quit(process.pid)
        output, error = process.communicate(timeout=15)
        assert process.returncode == 0 and error == '', 'packaged_quit_cleanup_failed'
        assert not (root / 'open-demo.html').exists()
        assert not (root / 'native-control.json').exists()
        assert retained.read_text() == 'retain private state'
        with (root / 'application.lock').open('rb') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        with socket.socket() as connection:
            connection.settimeout(1)
            assert connection.connect_ex(('127.0.0.1', port)) != 0
    finally:
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_packaged_expat_does_not_require_newer_system_library():
    executable=Path(os.environ['A13_PACKAGE_EXECUTABLE'])
    app=executable.parents[2]
    library=app/'Contents/Frameworks/libexpat.1.dylib'
    license_file=app/'Contents/Resources/third_party/expat-COPYING.txt'
    assert library.is_file() and not library.is_symlink()
    assert license_file.is_file() and license_file.stat().st_size>0
    extensions=[p for p in (app/'Contents/Frameworks').rglob('pyexpat*.so') if p.is_file() and not p.is_symlink()]
    assert len(extensions)==1
    linkage=subprocess.run(['/usr/bin/otool','-L',str(extensions[0])],capture_output=True,text=True,check=True).stdout
    assert '/usr/lib/libexpat.1.dylib' not in linkage
    assert '@loader_path/' in linkage and 'libexpat.1.dylib' in linkage


def test_packaged_mlx_metallib_is_present():
    executable=Path(os.environ['A13_PACKAGE_EXECUTABLE'])
    libraries=[p for p in executable.parents[2].rglob('mlx.metallib') if p.is_file() and not p.is_symlink()]
    assert libraries, 'mlx_gpu_library_missing_from_package'
    assert all(p.stat().st_size>0 for p in libraries)


def test_packaged_mlx_arithmetic_without_model_or_identity(tmp_path):
    executable=Path(os.environ['A13_PACKAGE_EXECUTABLE'])
    root=tmp_path/'must-not-create-identity'
    env=dict(os.environ)
    for key in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV'):env.pop(key,None)
    result=subprocess.run([str(executable),'--runtime-probe','--state-root',str(root)],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0, result.stderr
    report=json.loads(result.stdout)
    assert report['available'] is True and report['value']==2.0
    assert report['backend']=='mlx' and report['dtype']=='float32'
    assert report['model_loaded'] is False and report['device'].startswith('Device(gpu,')
    assert not root.exists()


def test_packaged_compiler_help_is_self_contained(tmp_path):
    executable=Path(os.environ['A13_PACKAGE_EXECUTABLE'])
    env=dict(os.environ)
    for key in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV'):env.pop(key,None)
    result=subprocess.run([str(executable),'--compile-route','--help'],cwd=tmp_path,
        env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0, result.stderr
    assert '--managed-transport' in result.stdout and '--managed-hello-observations' in result.stdout


def test_packaged_compiler_runtime_sources_present():
    executable=Path(os.environ['A13_PACKAGE_EXECUTABLE'])
    framework=executable.parents[2]/'Contents/Frameworks/a13-runtime-source'
    for relative in ('scripts/build_qwen_live_route.py','physical_inference_node.py','weight_quantization.py',
        'mycelium_a13_macos/serving.py','mycelium_node/identity.py'):
        source=framework/relative
        assert source.is_file(), 'compiler_source_missing:'+relative
        assert source.read_bytes(), 'compiler_source_empty:'+relative


def test_packaged_relay_client_requires_no_system_install(tmp_path):
    executable=Path(os.environ['A13_PACKAGE_EXECUTABLE'])
    app=executable.parents[2]
    binary=app/'Contents/Frameworks/mycelium_a13_macos/native/cloudflared'
    assert binary.is_file(), 'owned_relay_client_missing'
    result=subprocess.run([str(binary),'--version'],cwd=tmp_path,capture_output=True,text=True,timeout=10)
    assert result.returncode==0 and result.stdout.startswith('cloudflared version ')
    assert (app/'Contents/Resources/third_party/cloudflared-LICENSE').is_file()


def test_packaged_process_launch_handshake(tmp_path):
    import select
    executable=os.environ['A13_PACKAGE_EXECUTABLE']
    ready_r,ready_w=os.pipe();release_r,release_w=os.pipe();exec_r,exec_w=os.pipe()
    process=None
    try:
        process=subprocess.Popen([executable,'--node-process-launcher',str(ready_w),str(release_r),
            str(exec_w),executable,'--preflight'],pass_fds=(ready_w,release_r,exec_w),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=tmp_path,start_new_session=True)
        os.close(ready_w);ready_w=None;os.close(release_r);release_r=None;os.close(exec_w);exec_w=None
        assert select.select([ready_r],[],[],10)[0], 'packaged_launcher_handshake_missing'
        assert os.read(ready_r,1)==b'R', 'packaged_launcher_handshake_invalid'
        os.write(release_w,b'G')
        out,err=process.communicate(timeout=15)
        assert process.returncode==0,(out,err)
        assert json.loads(out)['platform']=='macos'
        assert os.read(exec_r,1)==b''
    finally:
        if process is not None and process.poll() is None:process.kill();process.wait(timeout=10)
        for fd in [ready_r,ready_w,release_r,release_w,exec_r,exec_w]:
            if fd is not None:os.close(fd)
