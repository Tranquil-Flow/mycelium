# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build explicitly unsigned/ad-hoc macOS demo; never submit for signing."""
from pathlib import Path
import json
import os
import platform
import subprocess
import sys
import hashlib
import argparse
import shutil

ROOT=Path(__file__).resolve().parents[1]

def require_compatible_mlx_wheels():
    """Fail before packaging if host-selected wheels exclude the minimum OS."""
    import importlib.metadata
    result={}
    for name in ('mlx','mlx-metal'):
        dist=importlib.metadata.distribution(name)
        tags=[line.split(':',1)[1].strip() for line in (dist.read_text('WHEEL') or '').splitlines() if line.startswith('Tag:')]
        if dist.version!='0.31.1' or not any(tag.endswith('-macosx_14_0_arm64') for tag in tags):
            raise SystemExit('compatible_mlx_wheel_required:'+name)
        result[name]={'version':dist.version,'wheel_tags':tags}
    return result


def vendor_expat(app):
    """Keep Python's XML extension independent of newer macOS system Expat."""
    import shutil
    source=Path('/opt/homebrew/opt/expat/lib/libexpat.1.dylib')
    if not source.is_file():raise SystemExit('expat_build_dependency_required')
    license_source=source.resolve().parents[1]/'COPYING'
    if not license_source.is_file():raise SystemExit('expat_license_required')
    symbols=subprocess.run(['/usr/bin/nm','-gU',str(source)],capture_output=True,text=True,check=True).stdout
    if '_XML_SetAllocTrackerActivationThreshold' not in symbols:
        raise SystemExit('expat_build_dependency_too_old')
    framework=app/'Contents/Frameworks'
    extensions=[p for p in framework.rglob('pyexpat*.so') if p.is_file() and not p.is_symlink()]
    if len(extensions)!=1:raise SystemExit('packaged_pyexpat_missing_or_ambiguous')
    library=framework/'libexpat.1.dylib'
    shutil.copyfile(source,library)
    library.chmod(0o755)
    license_file=app/'Contents/Resources/third_party/expat-COPYING.txt'
    license_file.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(license_source,license_file)
    subprocess.run(['/usr/bin/install_name_tool','-id','@rpath/libexpat.1.dylib',str(library)],check=True)
    extension=extensions[0]
    target='@loader_path/'+os.path.relpath(library,extension.parent)
    subprocess.run(['/usr/bin/install_name_tool','-change','/usr/lib/libexpat.1.dylib',target,str(extension)],check=True)
    for binary in (library,extension):
        subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(binary)],check=True)
    subprocess.run(['/usr/bin/codesign','--force','--deep','--sign','-',str(app)],check=True)
    return {'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'bundled_sha256':hashlib.sha256(library.read_bytes()).hexdigest(),
        'version':source.resolve().parents[1].name,'license':'Expat/MIT (COPYING bundled)'}


def stage_compiler_sources(destination):
    """Ship source data the canonical compiler stages; never developer state."""
    import shutil
    from scripts.build_qwen_live_route import runtime_source_paths
    files=set(runtime_source_paths(ROOT))
    files.add('scripts/build_qwen_live_route.py')
    for package in ('mycelium_a13_macos',):
        files.update(str(p.relative_to(ROOT)) for p in (ROOT/package).rglob('*.py') if p.is_file())
    destination.mkdir(parents=True,exist_ok=False)
    for relative in sorted(files):
        source=ROOT/relative
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT):
            raise SystemExit('compiler_source_unsafe')
        target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,target)
    return {'files':len(files),'sha256':{relative:hashlib.sha256((destination/relative).read_bytes()).hexdigest() for relative in sorted(files)}}


def main():
    if platform.system()!='Darwin' or platform.machine()!='arm64':
        raise SystemExit('macos_arm64_build_required')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root',type=Path,default=ROOT/'dist/0.1.0-preview.2')
    args=parser.parse_args()
    output=args.output_root.expanduser().absolute()
    # Historical preview artifacts are immutable. Never rebuild over dist root.
    if output==ROOT/'dist' or output.exists():
        raise SystemExit('fresh_output_root_required')
    runtime_wheels=require_compatible_mlx_wheels()
    output.mkdir(parents=True,mode=0o700)
    work=ROOT/'build'/output.name
    work.mkdir(parents=True,exist_ok=True)
    sys.path.insert(0,str(ROOT))
    compiler_sources=work/'compiler-sources'
    compiler_identity=stage_compiler_sources(compiler_sources)
    relay_client=shutil.which('cloudflared')
    relay_license=ROOT/'.build-native/cloudflared-LICENSE'
    if relay_client is None or not relay_license.is_file():
        raise SystemExit('cloudflared_build_dependency_and_license_required')

    from mycelium_a13_macos.resources import build_policy_probe
    probe=build_policy_probe(work/'resource_probe')
    env=dict(os.environ)
    for name in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV'): env.pop(name,None)
    # Build the locked lane-owned native dependency; never borrow fleet artifacts.
    native_target=ROOT/'.build-native'
    native_env={**env,'CARGO_TARGET_DIR':str(native_target)}
    subprocess.run(['cargo','build','--manifest-path',str(ROOT/'native/iroh_transport/Cargo.toml'),
        '--offline','--locked','--release','--bin','mycelium-iroh-sidecar','--jobs','2'],
        cwd=ROOT,env=native_env,check=True)
    sidecar=native_target/'release/mycelium-iroh-sidecar'
    smoke=subprocess.run([str(sidecar),'--help'],capture_output=True,text=True,check=True,timeout=10)
    if '--bootstrap-fd' not in smoke.stdout or '--uds' not in smoke.stdout or smoke.stderr:
        raise SystemExit('native_sidecar_contract_invalid')
    args=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--windowed',
          '--name','Mycelium A13 Demo','--target-architecture','arm64',
          '--osx-bundle-identifier','org.mycelium.a13-macos-demo',
          '--paths',str(ROOT),'--collect-all','mlx','--copy-metadata','mlx-metal',
          '--hidden-import','mycelium_a13_macos.serving',
          '--hidden-import','mycelium_a13_macos.workflow',
          '--hidden-import','scripts.build_qwen_live_route',
          '--add-data',f'{compiler_sources}:a13-runtime-source',
          '--hidden-import','mycelium_a13_macos.managed_observations',
          '--distpath',str(output),'--workpath',str(work),'--specpath',str(work),
          '--add-binary',f'{probe}:mycelium_a13_macos/native',
          '--add-binary',f'{sidecar}:mycelium_a13_macos/native',
          '--add-binary',f'{relay_client}:mycelium_a13_macos/native',
          '--add-data',f'{ROOT / "mycelium_a13_macos/web"}:mycelium_a13_macos/web',
          str(ROOT/'mycelium_a13_macos/__main__.py')]
    subprocess.run(args,cwd=ROOT,env=env,check=True)
    provenance=output/'Mycelium A13 Demo.app/Contents/Resources/third_party/mlx-wheel-provenance.json'
    provenance.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(relay_license,provenance.parent/'cloudflared-LICENSE')
    provenance.write_text(json.dumps(runtime_wheels,indent=2)+'\n')
    (provenance.parent/'compiler-source-identity.json').write_text(json.dumps(compiler_identity,sort_keys=True,indent=2)+'\n')
    expat=vendor_expat(output/'Mycelium A13 Demo.app')
    executable=output/'Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo'
    preflight=subprocess.run([str(executable),'--preflight'],capture_output=True,text=True,check=True)
    report={'platform':'macos-arm64','signing':'ad-hoc-only, no publisher certificate; NOT notarized',
            'executable_sha256':hashlib.sha256(executable.read_bytes()).hexdigest(),
            'preflight':json.loads(preflight.stdout),
            'resource_probe_sha256':hashlib.sha256(probe.read_bytes()).hexdigest(),
            'sidecar_source_sha256':hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            'version':output.name,'vendored_expat':expat}
    (output/'artifact-identity.json').write_text(json.dumps(report,indent=2)+'\n')
    launcher=output/'Open Mycelium.command'
    launcher.write_text('#!/bin/sh\nset -eu\nHERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\nexec "$HERE/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo" "$@"\n')
    launcher.chmod(0o755)
    print(json.dumps(report))

if __name__=='__main__': main()
