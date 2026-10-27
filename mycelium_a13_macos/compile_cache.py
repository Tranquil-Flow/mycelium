# SPDX-License-Identifier: AGPL-3.0-only
"""One in-process compilation template; never a lease, consent or serving proof.

Only immutable model bytes and locally computed expectations survive a run.
Descriptors, observations, assignments, stage packs, offers, and qualification
are generated/verified again for the new run. No live tensors are retained.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat


def cache_key(prepared, members, facts, split, keys, slice_fetch):
    model = prepared.model
    value = {
        'model': {k: getattr(model, k, None) for k in ('model_id', 'resolved_commit',
            'source_representation', 'runtime_dtype', 'runtime_quantization')},
        'manifest_digest': prepared.manifest_digest, 'manifest': prepared.manifest,
        'members': [{k: m[k] for k in ('node_id', 'generation', 'incarnation',
                     'verification_key_digest', 'endpoint_id')}
                    for m in sorted(members, key=lambda m: m['node_id'])],
        # Run-specific staging/source paths are deliberately not reusable authority.
        'facts': {n: {k: v for k, v in f.items() if k not in ('staging_root', 'source_root')}
                  for n, f in sorted(facts.items())},
        'split': split, 'keys': keys, 'slice_fetch': slice_fetch,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def builder_key(args, topology, runtime_dtype, runtime_quantization, ranges):
    """Also bind the preparer's own arguments; never trust a caller key alone."""
    value = [args.compilation_cache_key, args.model_id, args.resolved_commit,
             runtime_dtype, runtime_quantization,
             None if ranges is None else [[r.start, r.stop] for r in ranges],
             [{k: v for k, v in n.items() if k != 'staging_root'} for n in topology],
             bool(getattr(args, 'stage_sharded', False))]
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def remember(cache, key, root, prepared, proofs, challenge, stage_sharding):
    """Replace the only slot after a complete successful build, without model objects."""
    rows = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('compilation_cache_artifact_changed')
        if path.is_file():
            rows.append({'path': path.relative_to(root).as_posix(),
                         'size': path.stat().st_size, 'sha256': _digest(path)})
    entry = {'key': key, 'root': str(root), 'files': rows,
             'manifest': prepared.manifest, 'route': prepared.route, 'reports': prepared.reports,
             'proofs': proofs, 'challenge': challenge, 'stage_sharding': stage_sharding}
    previous = cache.get('entry') or {}
    obsolete = list(cache.get('obsolete_roots', []))
    if previous.get('root') and previous['root'] != str(root):
        obsolete.append(previous['root'])
    cache.clear()
    cache['entry'] = copy.deepcopy(entry)
    cache['obsolete_roots'] = obsolete


def discard_retired_artifacts(app_root, run_id, cache):
    """After proven route teardown, bound weight storage to one cache + current run.

    Keep logs/metadata; never follow links or delete anything outside chain-runs.
    A cleanup failure is surfaced by the caller, not hidden in an endless loop.
    """
    from .managed_control import _segment
    base = Path(app_root) / 'chain-runs'
    root = base / _segment(run_id)
    retained = (cache.get('entry') or {}).get('root')
    roots = [root]
    for old in cache.get('obsolete_roots', []):
        path = Path(old)
        if path.parts[-3:] != ('compiled', 'transfer-bundle', 'deployment'):
            raise ValueError('compilation_cache_cleanup_path_invalid')
        roots.append(path.parents[2])
    for candidate in roots:
        if candidate.parent != base or candidate.resolve() != candidate.absolute():
            raise ValueError('compilation_cache_cleanup_path_invalid')
        for name in ('stage-shards', 'temporary', 'compiled'):
            target = candidate / name
            if name == 'compiled' and retained == str(target / 'transfer-bundle/deployment'):
                continue
            if target.is_symlink():
                raise ValueError('compilation_cache_cleanup_path_invalid')
            if target.is_dir():
                shutil.rmtree(target)
    cache['obsolete_roots'] = []


def materialize(cache, key, destination):
    """Copy-on-write where available; hash the copied bytes before using expectations.

    A mismatched key is a cold miss. A matched but altered artifact is a hard
    refusal, not an opportunity to reuse old proofs or hide cache corruption.
    """
    entry = cache.get('entry')
    if not entry or entry['key'] != key:
        return None
    from .slice_acquisition import _clone_file
    root = Path(entry['root'])
    if root.is_symlink() or root.resolve() != root.absolute():
        raise ValueError('compilation_cache_artifact_changed')
    for row in entry['files']:
        relative = Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('compilation_cache_artifact_changed')
        source, target = root / relative, destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if source.resolve() != source.absolute():
                raise ValueError('compilation_cache_artifact_changed')
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != row['size']:
                    raise ValueError('compilation_cache_artifact_changed')
                if not _clone_file(handle.fileno(), target):
                    with target.open('xb') as out:
                        shutil.copyfileobj(handle, out, length=1024 * 1024)
            target.chmod(0o600)
            if target.stat().st_size != row['size'] or _digest(target) != row['sha256']:
                raise ValueError('compilation_cache_artifact_changed')
        except OSError:
            raise ValueError('compilation_cache_artifact_changed') from None
    return copy.deepcopy(entry)
