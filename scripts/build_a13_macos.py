# SPDX-License-Identifier: AGPL-3.0-only
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

# THIS REPOSITORY IS THE APP'S SOURCE OF TRUTH. A second checkout (mycelium-a13-macos-demo) only ever held
# a stale copy of it, and the committed PyInstaller spec pointed THERE -- so a build driven by that spec
# would have produced an app from the wrong sources while every source check, digest compare and deploy
# receipt still looked clean (measured 2026-09-19; the program's own ERRATA-6). Two guards follow: this tree
# must look like the app tree, and the identity artefact records WHERE the sources came from as well as what
# they hash to.
_APP_TREE_MARKERS=('mycelium_a13_macos/__main__.py','mycelium_a13_macos/workflow.py',
    'mycelium_seed/plan_binding.py','mycelium_live/supervisor.py','mycelium_live/route.py')

# Compiled into the PYZ but NOT shipped as a13-runtime-source, so nothing recorded their provenance -- and
# they own the route/serving state machine, the region this program has spent the most rounds on.
_COMPILED_NOT_SHIPPED=('mycelium_live','mycelium_node','mycelium_router','mycelium_request_gateway',
    'mycelium_physical_runner','mycelium_qualification','mycelium_membership','mycelium_mobile')


def require_app_tree_root():
    """Fail before packaging if this checkout does not look like the app's own tree."""
    missing=[relative for relative in _APP_TREE_MARKERS if not (ROOT/relative).is_file()]
    if missing:raise SystemExit('build_root_not_the_app_tree:'+','.join(missing))
    return {'root':str(ROOT)}


def identity_of_compiled_modules():
    """Digests of the app-side modules PyInstaller compiles but does not ship as source."""
    digests={}
    for package in _COMPILED_NOT_SHIPPED:
        for path in sorted((ROOT/package).rglob('*.py')):
            if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts:
                digests[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def build_identity(compiler_identity):
    """Provenance of the build's INPUTS: where they came from, and what they hash to.

    Bytecode-level proof that a change actually ships is the program's tools/pyz_ledger.py; this artefact is
    the build's own statement about its inputs, and the two are read together. A digest match between local
    and deployed bundles proves the copy, never the content.
    """
    def git(*arguments):
        result=subprocess.run(['git','-C',str(ROOT),*arguments],capture_output=True,text=True)
        return result.stdout.strip() or None
    return {'root':str(ROOT),'head':git('rev-parse','HEAD'),
        'branch':git('rev-parse','--abbrev-ref','HEAD'),'worktree_dirty':bool(git('status','--porcelain')),
        'compiled_not_shipped':identity_of_compiled_modules(),**compiler_identity}

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


def mlx_pyinstaller_arguments():
    """Collect only RECORD-owned MLX bytes, never all namespace locations.

    ``mlx`` is a namespace shared by mlx and mlx-metal. In a system-site
    venv, PyInstaller's collect_all merges it with Homebrew's second root:
    the extension can come from the admitted wheel while libmlx/metallib
    come from a newer-OS wheel. Metadata alone does not authenticate those
    inputs. Enumerating RECORD also avoids importing native code to discover
    submodules.
    """
    import base64
    import importlib.machinery
    import importlib.metadata
    import importlib.util

    require_compatible_mlx_wheels()
    distributions = [importlib.metadata.distribution(name) for name in ('mlx', 'mlx-metal')]
    roots = {Path(dist.locate_file('mlx')).resolve() for dist in distributions}
    spec = importlib.util.find_spec('mlx')
    locations = list(spec.submodule_search_locations or ()) if spec else []
    if len(roots) != 1 or not locations or Path(locations[0]).resolve() not in roots:
        raise SystemExit('mlx_import_root_mismatch')
    root = roots.pop()
    files = {}
    for dist in distributions:
        owned = 0
        for entry in dist.files or ():
            relative = Path(str(entry))
            if relative.parts[:1] != ('mlx',) or '__pycache__' in relative.parts:
                continue
            path = Path(dist.locate_file(entry))
            digest = entry.hash
            if (relative.is_absolute() or '..' in relative.parts or
                    not path.resolve().is_relative_to(root) or not path.is_file() or
                    digest is None or digest.mode != 'sha256' or entry.size is None):
                raise SystemExit('mlx_wheel_content_invalid:'+str(relative))
            with path.open('rb') as stream:
                actual = base64.urlsafe_b64encode(hashlib.file_digest(stream, 'sha256').digest()).decode().rstrip('=')
            if actual != digest.value or path.stat().st_size != entry.size:
                raise SystemExit('mlx_wheel_content_invalid:'+str(relative))
            if relative in files and files[relative] != path:
                raise SystemExit('mlx_wheel_content_invalid:'+str(relative))
            files[relative] = path
            owned += 1
        if not owned:
            raise SystemExit('mlx_wheel_content_invalid:empty_record')
    required = {'mlx/lib/libmlx.dylib', 'mlx/lib/mlx.metallib'}
    if not required.issubset({str(path) for path in files}):
        raise SystemExit('mlx_wheel_content_invalid:missing_runtime')
    args = ['--copy-metadata', 'mlx', '--copy-metadata', 'mlx-metal']
    modules = {'mlx'}
    for relative, path in sorted(files.items()):
        extension = next((suffix for suffix in importlib.machinery.EXTENSION_SUFFIXES
                          if relative.name.endswith(suffix)), None)
        if extension:
            modules.add('.'.join((*relative.parts[:-1], relative.name[:-len(extension)])))
        elif relative.suffix == '.py':
            parts = relative.with_suffix('').parts
            modules.add('.'.join(parts[:-1] if parts[-1] == '__init__' else parts))
        else:
            option = '--add-binary' if relative.suffix == '.dylib' else '--add-data'
            args.extend((option, f'{path}:{relative.parent}'))
    if 'mlx.core' not in modules:
        raise SystemExit('mlx_wheel_content_invalid:missing_core')
    for module in sorted(modules):
        args.extend(('--hidden-import', module))
    return args


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


def macho_minimum_version(path):
    """The macOS minos of one Mach-O file (LC_BUILD_VERSION / LC_VERSION_MIN_MACOSX), or None."""
    try:
        with open(path,'rb') as f:magic=f.read(4)
    except OSError:return None
    if magic not in (b'\xcf\xfa\xed\xfe',b'\xce\xfa\xed\xfe',b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca'):
        return None
    out=subprocess.run(['/usr/bin/otool','-l',str(path)],capture_output=True,text=True).stdout
    versions=[]
    lines=out.splitlines()
    for i,line in enumerate(lines):
        text=line.strip()
        if text=='cmd LC_BUILD_VERSION':
            for follow in lines[i+1:i+6]:
                if follow.strip().startswith('minos '):versions.append(follow.split()[1])
        elif text=='cmd LC_VERSION_MIN_MACOSX':
            for follow in lines[i+1:i+4]:
                if follow.strip().startswith('version '):versions.append(follow.split()[1])
    if not versions:return None
    return max(versions,key=_version_key)


def _version_key(text):
    return tuple(int(p) for p in text.split('.'))


def bundle_macho_floor(app):
    """Highest Mach-O minos in the bundle: the oldest macOS that can load all of it."""
    floor=None
    for path in sorted(Path(app).rglob('*')):
        if path.is_symlink() or not path.is_file():continue
        version=macho_minimum_version(path)
        if version is not None and (floor is None or _version_key(version)>_version_key(floor)):
            floor=version
    return floor


def set_bundle_minimum_system_version(app,*,admitted_versions=None):
    """Write Info.plist LSMinimumSystemVersion so Finder refuses older macOS cleanly.

    The floor is the lower of the bundle's highest Mach-O minos and the lowest
    macOS the product admits (package_manifest.ADMITTED_VERSIONS): it never sits
    above an admitted release, or Finder would refuse the app on a Mac the
    preflight accepts. Homebrew-built runtimes carry their build host's minos
    (26.0) without needing it; every such stamp above the declared floor is
    listed so the package records it rather than hiding it.
    """
    import plistlib
    if admitted_versions is None:
        from mycelium_a13_macos.package_manifest import ADMITTED_VERSIONS as admitted_versions
    if not admitted_versions:raise SystemExit('admitted_macos_versions_missing')
    admitted_floor=min(admitted_versions,key=_version_key)
    floor=bundle_macho_floor(app)
    if floor is None:raise SystemExit('bundle_macho_floor_unknown')
    declared=min(floor,admitted_floor,key=_version_key)
    plist=Path(app)/'Contents/Info.plist'
    with plist.open('rb') as f:info=plistlib.load(f)
    info['LSMinimumSystemVersion']=declared
    with plist.open('wb') as f:plistlib.dump(info,f)
    return {'declared':declared,'macho_minos_max':floor,'admitted_floor':admitted_floor,
            'macho_minos_above_declared':macho_minos_above(app,declared)}


def macho_minos_above(app,declared):
    """Bundle-relative paths of Mach-O files stamped with a minos above ``declared``."""
    above=[]
    for path in sorted(Path(app).rglob('*')):
        if path.is_symlink() or not path.is_file():continue
        version=macho_minimum_version(path)
        if version is not None and _version_key(version)>_version_key(declared):
            above.append(str(path.relative_to(app)))
    return above


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
    require_app_tree_root()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root',type=Path,default=ROOT/'dist/0.1.0-preview.2')
    args=parser.parse_args()
    output=args.output_root.expanduser().absolute()
    # Historical preview artifacts are immutable. Never rebuild over dist root.
    if output==ROOT/'dist' or output.exists():
        raise SystemExit('fresh_output_root_required')
    runtime_wheels=require_compatible_mlx_wheels()
    mlx_arguments=mlx_pyinstaller_arguments()
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
          '--paths',str(ROOT),*mlx_arguments,
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
    (provenance.parent/'compiler-source-identity.json').write_text(
        json.dumps(build_identity(compiler_identity),sort_keys=True,indent=2)+'\n')
    floor=set_bundle_minimum_system_version(output/'Mycelium A13 Demo.app')
    macos_floor=floor['declared']
    expat=vendor_expat(output/'Mycelium A13 Demo.app')
    # Vendored Expat lands after the plist edit (its codesign must cover it): re-list the stamps.
    floor['macho_minos_max']=bundle_macho_floor(output/'Mycelium A13 Demo.app')
    floor['macho_minos_above_declared']=macho_minos_above(output/'Mycelium A13 Demo.app',macos_floor)
    executable=output/'Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo'
    preflight=subprocess.run([str(executable),'--preflight'],capture_output=True,text=True,check=True)
    report={'platform':'macos-arm64','signing':'ad-hoc-only, no publisher certificate; NOT notarized',
            'executable_sha256':hashlib.sha256(executable.read_bytes()).hexdigest(),
            'preflight':json.loads(preflight.stdout),
            'resource_probe_sha256':hashlib.sha256(probe.read_bytes()).hexdigest(),
            'sidecar_source_sha256':hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            'version':output.name,'vendored_expat':expat,'macos_floor':macos_floor,
            'macos_floor_derivation':floor}
    (output/'artifact-identity.json').write_text(json.dumps(report,indent=2)+'\n')
    launcher=output/'Open Mycelium.command'
    launcher.write_text('#!/bin/sh\nset -eu\nHERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\nexec "$HERE/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo" "$@"\n')
    launcher.chmod(0o755)
    print(json.dumps(report))

if __name__=='__main__': main()
