"""Bind managed staging to matching, target-acquired HF weights.

This is a pure plan transformation before owner binding/publication. It does not
fetch files, authorize a load, write a plan or claim remote cache readiness.
Targets independently confine, hash and authorize every local copy. Only the
unchanged pinned BF16 source is supported; transformed/sharded sources need a
separate explicit representation contract rather than an implicit fallback.
"""
from __future__ import annotations
import json
from pathlib import PurePosixPath

from .serving import CANDIDATE_MODEL, PreparedSource


def bind_hf_prepositions(controller, *, prepared, target_source_roots):
    """Return a copy with model bytes removed from per-node control archives.

    ``controller`` is the controller section of a canonical builder plan.
    ``target_source_roots`` are snapshot paths supplied by the contributing
    applications, not executable paths. Canonical transfer validation remains
    required before signing/publishing and at the receiving target.
    """
    import model_manifest as mm
    try:
        if (not isinstance(prepared, PreparedSource) or prepared.model != CANDIDATE_MODEL
                or not mm.verify_manifest_digest(prepared.manifest)
                or mm.manifest_digest_ref(prepared.manifest) != prepared.manifest_digest
                or prepared.manifest['model_id'] != CANDIDATE_MODEL.model_id
                or prepared.manifest['resolved_commit'] != CANDIDATE_MODEL.resolved_commit):
            raise ValueError()
        result = json.loads(json.dumps(controller, allow_nan=False))
        peers = result['peers']
        nodes = [peer['node_id'] for peer in peers]
        if (not nodes or len(nodes) != len(set(nodes))
                or any(peer.get('process_transport') != 'managed_https' for peer in peers)
                or set(target_source_roots) != set(nodes)
                or 'prepositioned_artifacts' in result):
            raise ValueError()
        suffix = ('models--'+CANDIDATE_MODEL.model_id.replace('/', '--'),
                  'snapshots', CANDIDATE_MODEL.resolved_commit)
        roots = {}
        for node in nodes:
            value = target_source_roots[node]
            if not isinstance(value, str):
                raise ValueError()
            root = PurePosixPath(value)
            if (not root.is_absolute() or str(root) != value or '..' in root.parts
                    or root.parts[-3:] != suffix or len(value) > 1800):
                raise ValueError()
            roots[node] = root

        weights = {}
        for row in prepared.manifest['files']:
            path = PurePosixPath(row['path'])
            if path.is_absolute() or '..' in path.parts or str(path) != row['path']:
                raise ValueError()
            if path.suffix == '.safetensors':
                digest = row['content_digest']
                if digest['algorithm'] != 'sha256':
                    raise ValueError()
                weights.setdefault((row['size_bytes'], 'sha256:'+digest['value']), str(path))
        if not weights:
            raise ValueError()
        manifest = result['transfer_manifest']
        if manifest['protocol'] != 'mycelium.controller_transfer_manifest.v1':
            raise ValueError()
        records = manifest['files']
        base = {row['path']: row for row in records}
        if not 0 < len(records) <= 256 or len(base) != len(records):
            raise ValueError()
        source_for = {}
        for row in records:
            if PurePosixPath(row['path']).suffix == '.safetensors':
                if type(row['size_bytes']) is not int:
                    raise ValueError()
                source_for[row['path']] = weights[(row['size_bytes'], row['content_digest'])]
        if not source_for:
            raise ValueError()
        node_document = result['node_transfer_manifests']
        if (node_document['protocol'] != 'mycelium.controller_node_transfer_manifests.v1'
                or set(node_document['manifests']) != set(nodes)):
            raise ValueError()
        members = {}
        for node in nodes:
            transfer = node_document['manifests'][node]
            if transfer['protocol'] != 'mycelium.controller_transfer_manifest.v1':
                raise ValueError()
            retained, local = [], []
            for row in transfer['files']:
                if row != base[row['path']]:
                    raise ValueError()
                if row['path'] not in source_for:
                    retained.append(row)
                else:
                    local.append({'destination_path': row['path'],
                                  'source_path': str(roots[node]/source_for[row['path']]),
                                  'size_bytes': row['size_bytes'],
                                  'content_digest': row['content_digest']})
            transfer['files'] = retained
            members[node] = sorted(local, key=lambda row: row['destination_path'])
        result['prepositioned_artifacts'] = {
            'protocol': 'mycelium.controller_prepositioned_artifacts.v1', 'members': members}
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('managed_hf_transfer_binding_invalid') from None
