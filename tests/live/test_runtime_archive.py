# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise emitted runtime bytes, not checkout imports or --help."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from scripts import build_qwen_live_route as builder
from test_qwen_route_builder import _template

ROOT = Path(__file__).resolve().parents[2]
NAMES = ['physical_inference_node.py','runtime_loader.py','model_adapters.py','numpy_runtime.py','mycelium_router/numpy_runtime.py']


def packed(tmp_path):
    bundle=tmp_path/'bundle'
    template=_template()
    template['controller']['transfer_manifest']={'files':[]}
    builder._copy_runtime_closure(ROOT,bundle,template)
    return bundle


def test_emitted_runtime_fingerprint_is_actual_source_bytes(tmp_path):
    bundle=packed(tmp_path)
    # run_name != __main__ loads the launcher without consuming CLI stdin.
    # The executable bootstrap exposes a scoped extraction context, no sys.path
    # or imports from the checkout escape into the fresh interpreter.
    code='''import runpy,sys,json,importlib
from pathlib import Path
scope=runpy.run_path(sys.argv[1],run_name='archive_probe')
from contextlib import nullcontext
with scope.get('runtime_tree',lambda:nullcontext(Path(sys.argv[1]).with_name('mycelium-runtime.zip')))() as root:
 sys.path.insert(0,str(root))
 import physical_inference_node as node
 print(json.dumps({'digest':node._runtime_build_digest('numpy'),'root':str(root),'module':node.__file__,'version':sys.version}))
'''
    p=subprocess.run([sys.executable,'-I','-c',code,str(bundle/'physical_inference_node.py')],cwd=tmp_path,text=True,capture_output=True,timeout=30)
    assert p.returncode==0,p.stderr
    result=json.loads(p.stdout)
    h=hashlib.sha256(result['version'].encode())
    with zipfile.ZipFile(bundle/'mycelium-runtime.zip') as archive:
        for name in NAMES: h.update(name.encode());h.update(archive.read(name))
    assert result['digest']=='sha256:'+h.hexdigest()
    assert '.zip/' not in result['module']
    assert not Path(result['root']).exists()


def test_missing_source_fingerprint_is_refused(tmp_path, monkeypatch):
    import physical_inference_node as node
    monkeypatch.setattr(node,'__file__',str(tmp_path/'physical_inference_node.py'))
    with pytest.raises(Exception,match='runtime_build_source_unavailable'):
        node._runtime_build_digest('numpy')


def probe(bundle,tmp_path):
    code='''import runpy,sys,json
s=runpy.run_path(sys.argv[1],run_name='probe')
with s['runtime_tree']() as root:
 sys.path.insert(0,str(root))
 import physical_inference_node as node
 print(node._runtime_build_digest('numpy'))
'''
    return subprocess.run([sys.executable,'-I','-c',code,str(bundle/'physical_inference_node.py')],cwd=tmp_path,capture_output=True,text=True,timeout=30)


def test_rebuilt_changed_source_changes_fingerprint_and_resources_resolve(tmp_path):
    import shutil
    repo=tmp_path/'source';repo.mkdir()
    paths=builder.runtime_source_paths(ROOT)
    for name in (*paths,'scripts/runtime_archive_bootstrap.py','mycelium_a13_macos/native/resource_probe.m'):
        target=repo/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
    # Explicit inert executable-resource fixtures exercise actual module locators,
    # not a claim of executing the production probe or bundled sidecar.
    resources=['.build-native/release/mycelium-iroh-sidecar','mycelium_a13_macos/native/resource_probe']
    for name in resources:
        target=repo/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(b'#!/bin/sh\nexit 0\n');target.chmod(0o700)
    template=_template();template['controller']['transfer_manifest']={'files':[{'path':p} for p in resources+['mycelium_a13_macos/native/resource_probe.m']]}
    a=tmp_path/'a';b=tmp_path/'b'
    builder._copy_runtime_closure(repo,a,template)
    with (repo/'runtime_loader.py').open('a') as stream:stream.write('\n# discriminating source-byte mutation\n')
    builder._copy_runtime_closure(repo,b,template)
    before=probe(a,tmp_path);after=probe(b,tmp_path)
    assert before.returncode==after.returncode==0,(before.stderr,after.stderr)
    assert before.stdout!=after.stdout
    code='''import runpy,sys,json
from pathlib import Path
s=runpy.run_path(sys.argv[1],run_name='probe')
with s['runtime_tree']() as root:
 sys.path.insert(0,str(root))
 from mycelium_a13_macos import native_runtime,resources
 sidecar=native_runtime.installed_sidecar()
 candidates=resources._resource_probe_candidates()
 assert sidecar.is_relative_to(root) and sidecar.read_bytes()==b'#!/bin/sh\\nexit 0\\n'
 assert len(candidates)==1 and candidates[0].is_relative_to(root) and resources._is_safe_executable(candidates[0])
 source=Path(resources.__file__).parent/'native/resource_probe.m'
 assert source.is_file() and source.is_relative_to(root)
 sidecar.unlink()
 try:native_runtime.installed_sidecar()
 except FileNotFoundError:pass
 else:raise AssertionError('missing resource accepted')
 print(json.dumps({'sidecar':str(sidecar.relative_to(root)),'probe':str(candidates[0].relative_to(root)),'source':str(source.relative_to(root))}))
'''
    r=subprocess.run([sys.executable,'-I','-c',code,str(a/'physical_inference_node.py')],cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert r.returncode==0,r.stderr
    assert set(json.loads(r.stdout))=={'sidecar','probe','source'}


@pytest.mark.parametrize('attack',['missing','corrupt','member-missing','member-corrupt','traversal','symlink','duplicate'])
def test_invalid_archive_is_refused_before_import(tmp_path,attack):
    bundle=packed(tmp_path);archive=bundle/'mycelium-runtime.zip';launcher=bundle/'physical_inference_node.py'
    if attack=='missing':archive.unlink()
    elif attack=='corrupt':archive.write_bytes(b'not zip')
    else:
        # Update only outer hash for member-integrity controls. Malicious-member
        # controls also update the trusted index to prove path/type validation.
        import runpy
        scope=runpy.run_path(str(launcher),run_name='fixture');members=scope['RUNTIME_MEMBERS'].copy()
        with zipfile.ZipFile(archive) as z:entries=[(i,z.read(i)) for i in z.infolist()]
        changed=[]
        for info,data in entries:
            if info.filename=='runtime_loader.py':
                if attack=='member-missing':continue
                if attack=='member-corrupt':data+=b'\n#corrupt\n'
                if attack=='traversal':
                    members['../escape.py']=members.pop(info.filename);info.filename='../escape.py'
                if attack=='symlink':info.external_attr=(0o120777 << 16)
            changed.append((info,data))
        if attack=='duplicate':changed.append(changed[0])
        with zipfile.ZipFile(archive,'w') as z:
            for info,data in changed:z.writestr(info,data)
        # This is an explicitly adversarial bootstrap fixture, never production
        # authority. The original builder output is tested by the positive arm.
        text=launcher.read_text();text=text[:text.index('\nRUNTIME_ARCHIVE_SHA256 = ')]
        text+='\nRUNTIME_ARCHIVE_SHA256 = '+repr(hashlib.sha256(archive.read_bytes()).hexdigest())
        text+='\nRUNTIME_MEMBERS = '+repr(members)+'\n'
        launcher.write_text(text)
    result=probe(bundle,tmp_path)
    assert result.returncode!=0 and 'runtime_archive_invalid' in result.stderr,result
    assert not (tmp_path/'escape.py').exists()


def test_missing_declared_module_resource_refuses_before_import(tmp_path):
    from scripts.build_qwen_live_route import _copy_runtime_closure
    repo=Path(__file__).resolve().parents[2]
    bundle=tmp_path/'bundle'
    resource='mycelium_a13_macos/native/resource_probe.m'
    _copy_runtime_closure(repo,bundle,dict(controller=dict(transfer_manifest=dict(files=[dict(path=resource)]))))
    archive=bundle/'mycelium-runtime.zip'
    with zipfile.ZipFile(archive) as z:
        entries={i.filename:(i,z.read(i)) for i in z.infolist()}
    assert resource in entries
    del entries[resource]
    with zipfile.ZipFile(archive,'w') as z:
        for info,raw in entries.values():z.writestr(info,raw)
    # Even when only the outer hash is re-pinned, the missing declared
    # resource cannot be supplied from a checkout or sibling fallback.
    launcher=bundle/'physical_inference_node.py'
    text=launcher.read_text();before=text.split('RUNTIME_ARCHIVE_SHA256 = ')[1].splitlines()[0]
    launcher.write_text(text.replace('RUNTIME_ARCHIVE_SHA256 = '+before,
        'RUNTIME_ARCHIVE_SHA256 = '+repr(hashlib.sha256(archive.read_bytes()).hexdigest())))
    result=subprocess.run([sys.executable,'-I',str(launcher),'--help'],capture_output=True,text=True,cwd=tmp_path)
    assert result.returncode and 'runtime_archive_invalid' in result.stderr
