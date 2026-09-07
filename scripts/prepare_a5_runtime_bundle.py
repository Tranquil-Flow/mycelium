#!/usr/bin/env python3
"""Assemble local A5 runtime bytes; never stage, load or grant admission.

The trusted controller stays in its attested Git checkout. Transfer archives
are payloads, not alternate Git roots. Model preparation remains the existing
build_qwen_live_route producer and requires separate exact-model authority.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from physical_inference_qualification import build_transfer_archive


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while block := f.read(1024*1024):
            h.update(block)
    return 'sha256:' + h.hexdigest()


def regular(path: Path) -> None:
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('unsafe_artifact')
    if any(p.is_symlink() for p in path.parents):
        raise ValueError('unsafe_artifact')


def source_identity(root: Path) -> dict:
    if root.is_symlink() or not (root/'.git').exists():
        raise ValueError('source_git_root_invalid')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.PIPE).strip()
    if Path(git('rev-parse', '--show-toplevel')).resolve() != root.resolve():
        raise ValueError('source_git_root_invalid')
    if git('status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('source_not_clean')
    return {'git_root': str(root.resolve()), 'commit': git('rev-parse','HEAD'), 'tree': git('rev-parse','HEAD^{tree}')}


def native_identity(path: Path) -> dict:
    regular(path)
    description = subprocess.check_output(['/usr/bin/file', '-b', str(path)], text=True).strip()
    expected = 'Mach-O 64-bit executable arm64'
    if platform.system() != 'Darwin' or platform.machine() != 'arm64' or expected not in description:
        raise ValueError('native_architecture_invalid')
    if not os.access(path, os.X_OK):
        raise ValueError('native_not_executable')
    version = subprocess.check_output([str(path),'--version'], text=True, timeout=5).strip()
    if not version.startswith('mycelium-iroh-sidecar '):
        raise ValueError('native_identity_invalid')
    return {'sha256': digest(path), 'size_bytes': path.stat().st_size,
            'mode': oct(stat.S_IMODE(path.stat().st_mode)), 'architecture': description,
            'version': version, 'platform_scope': 'aarch64-apple-darwin-only'}


def transfer_manifest(root: Path) -> dict:
    records = []
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            raise ValueError('unsafe_artifact')
        if p.is_dir():
            continue
        regular(p)
        records.append({'path': p.relative_to(root).as_posix(), 'size_bytes': p.stat().st_size, 'content_digest': digest(p)})
    result = {'protocol': 'mycelium.controller_transfer_manifest.v1', 'files': records}
    # Same validation and archive path as physical controller, without its runner.
    build_transfer_archive(root, result)
    return result


def write_json(path: Path, value: object) -> None:
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(raw); f.flush(); os.fsync(f.fileno())


def prepare(repo: Path, sidecar: Path, ui: Path, output: Path, paths: list[str]) -> dict:
    source = source_identity(repo)
    native = native_identity(sidecar)
    if not (ui/'index.html').is_file() or ui.is_symlink():
        raise ValueError('ui_distribution_missing')
    if output.exists() or output.is_symlink():
        raise ValueError('output_exists')
    if len(set(paths)) != len(paths) or not paths:
        raise ValueError('transfer_subset_invalid')
    tracked = set(subprocess.check_output(['git','-C',str(repo),'ls-files'],text=True).splitlines())
    for relative in paths:
        if relative not in tracked or Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ValueError('transfer_subset_invalid')
        regular(repo/relative)
    output.mkdir(mode=0o700)
    payload = output/'payload'; payload.mkdir(mode=0o700)
    for relative in sorted(paths):
        target = payload/relative; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo/relative, target)
    (payload/'native').mkdir(exist_ok=True)
    target = payload/'native/mycelium-iroh-sidecar'
    shutil.copyfile(sidecar, target); target.chmod(0o700)
    ui_files = []
    for p in sorted(ui.rglob('*')):
        if p.is_symlink():
            raise ValueError('unsafe_artifact')
        if p.is_dir():
            continue
        regular(p)
        target = output/'ui'/p.relative_to(ui)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)
        ui_files.append({'path': p.relative_to(ui).as_posix(), 'sha256': digest(target), 'size_bytes': target.stat().st_size})
    manifest = transfer_manifest(payload)
    write_json(output/'transfer-manifest.json', manifest)
    archive = build_transfer_archive(payload, manifest)
    fd = os.open(output/'runtime.tar', os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(fd,'wb') as f:
        f.write(archive); f.flush(); os.fsync(f.fileno())
    if source != source_identity(repo):
        raise ValueError('source_changed')
    identity = {'protocol': 'mycelium.a5_local_runtime_preparation.v1',
        'source': source, 'native': native, 'ui_files': ui_files,
        'python': {'executable': sys.executable, 'version': sys.version,
                   'dependency_lock_sha256': digest(repo/'release/python-requirements.lock')},
        'transfer_manifest_sha256': digest(output/'transfer-manifest.json'),
        'archive_sha256': digest(output/'runtime.tar'),
        'transfer_scope': 'explicit runtime subset, not a model or complete placement',
        'model_identity': None, 'target_native_identity': None,
        'admission': False, 'qualification_claim': False, 'promotion_authorized': False,
        'remaining_bindings': ['exact_model_revision_representation_stage_packs',
            'target_native_binaries', 'fresh_host_membership_capacity_load_authority',
            'per_host_model_transfer_subsets', 'exclusive_fleet_grant']}
    write_json(output/'runtime-preparation.json', identity)
    return identity


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument('--repo', type=Path, required=True)
    p.add_argument('--sidecar', type=Path, required=True)
    p.add_argument('--ui-root', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--transfer-path', action='append', required=True)
    a = p.parse_args()
    print(json.dumps(prepare(a.repo,a.sidecar,a.ui_root,a.output,a.transfer_path), sort_keys=True))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
