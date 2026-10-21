# SPDX-License-Identifier: AGPL-3.0-only
"""Run under shared run_bounded.py only. Tiny local fixtures, offline Cargo.

The exported native source is a separate disposable Git fixture, NOT another
worker lane. Its exact committed source identity is compiled into the witness.
No real APK, device, installed application, seed HTTP or onboarding is exercised.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[2]
BASE='359ee19e788e532f60f6aaf081d713b553084587'
COMPOSITION='2fd3cff'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=False)
    assert not out.is_relative_to(ROOT)
    fixture=out/'native-source';fixture.mkdir()
    # Native-only lane has no host ControlCall carrier. Compose the committed
    # carrier with the latest mobile core only in this owned fixture export.
    for commit,paths in [(COMPOSITION,['native','contracts']),(BASE,['native/mobile_core'])]:
        if commit==BASE: shutil.rmtree(fixture/'native/mobile_core')
        raw=subprocess.check_output(['git','archive',commit,*paths],cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(raw)) as t:t.extractall(fixture,filter='data')
    origin=subprocess.check_output(['git','show',COMPOSITION+':native/mobile_core/src/node_real_composition.rs'],cwd=ROOT)
    (out/'composition-original.rs').write_bytes(origin)
    node=fixture/'native/mobile_core/src/node.rs'
    raw=node.read_text();needle='mod tests {\n    use super::*;'
    assert raw.count(needle)==1
    node.write_text(raw.replace(needle,needle+'\n    mod builder_review { include!(concat!(env!("CARGO_MANIFEST_DIR"), "/src/phone_builder_native.rs")); }'))
    (fixture/'native/mobile_core/src/phone_builder_native.rs').write_bytes((ROOT/'tests/iroh_conformance/phone_builder_native.rs').read_bytes())
    hashes={str(p.relative_to(fixture)):sha(p) for p in fixture.rglob('*') if p.is_file()}
    for args in [('init',),('add','native','contracts'),('-c','user.name=Tranquil-Flow',
        '-c','user.email=tranquil_flow@protonmail.com','-c','core.hooksPath=/dev/null','commit','-m','test: pin exact disposable native builder fixture')]:
        subprocess.run(['git','-C',str(fixture),*args],check=True,capture_output=True)
    commit=subprocess.check_output(['git','-C',str(fixture),'rev-parse','HEAD'],text=True).strip()
    identity=dict(protocol='mycelium.mobile_runtime_build.v1',source_commit=commit,mobile_core_version='0.1.0')
    digest='sha256:'+hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    sidecar=Path(os.environ['CARGO_TARGET_DIR'])/'debug/mycelium-iroh-sidecar'
    env=dict(os.environ,MYCELIUM_NODE_BUILD_DIGEST=digest,BUILDER_REVIEW_ROOT=str(ROOT),
        BUILDER_REVIEW_SOURCE=str(fixture),BUILDER_REVIEW_EVIDENCE=str(out),BUILDER_REVIEW_SIDECAR=str(sidecar))
    commands=[['cargo','build','--manifest-path',str(fixture/'native/iroh_transport/Cargo.toml'),'--offline','--locked','--bin','mycelium-iroh-sidecar'],
        ['cargo','test','--manifest-path',str(fixture/'native/mobile_core/Cargo.toml'),'--offline','--locked','--features','net','--lib','builder_review_native_configure','--','--nocapture','--test-threads=1']]
    locks={str(p.relative_to(fixture)):sha(p) for p in fixture.rglob('Cargo.lock')}
    result=dict(native_commit=BASE,composition_commit=subprocess.check_output(['git','rev-parse',COMPOSITION],cwd=ROOT,text=True).strip(),
        fixture_commit=commit,source_hashes=hashes,commands=[],locks_before=locks,
        receipt_boundary='real composed fixture source identity; synthetic APK receipt, NOT AN APK or physical proof',build_digest=digest)
    rc=1
    try:
        for cmd in commands:
            print('RUN',json.dumps(cmd),flush=True)
            rc=subprocess.run(cmd,cwd=ROOT,env=env,timeout=700).returncode
            result['commands'].append(dict(command=cmd,exit_code=rc))
            if rc:break
        if sidecar.exists():result['sidecar_sha256']=sha(sidecar)
    finally:
        result['locks_after']={str(p.relative_to(fixture)):sha(p) for p in fixture.rglob('Cargo.lock')}
        result['source_unchanged']=all(sha(fixture/p)==h for p,h in hashes.items())
        result['exit_code']=rc
        (out/'execution.json').write_text(json.dumps(result,indent=2)+'\n')
    assert result['source_unchanged'] and result['locks_after']==locks
    return rc
if __name__=='__main__':raise SystemExit(main())
