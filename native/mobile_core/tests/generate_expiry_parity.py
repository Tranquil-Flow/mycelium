# SPDX-License-Identifier: AGPL-3.0-only
"""Generate native expiry parity from the current Python relay (no model).
Usage: python generate_expiry_parity.py --python-root <pathowner tree>
The source root is explicit: the native branch's older Python relay has no expiry.
"""
import argparse
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

p = argparse.ArgumentParser()
p.add_argument('--python-root', type=Path, required=True)
p.add_argument('--output', type=Path)
p.add_argument('--python-revision', required=True)
a = p.parse_args()
root = a.python_root.resolve()
import subprocess
revision = subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
expected = subprocess.check_output(['git','-C',str(root),'rev-parse',a.python_revision],text=True).strip()
assert revision == expected, (revision, expected)
assert not subprocess.check_output(['git','-C',str(root),'status','--porcelain'],text=True).strip(), 'oracle tree must be clean'
# Every Python dependency, including generator and contracts, comes from ONE
# complete committed tree. Never splice a relay into an older import graph.
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location('expiry_oracle', root/'scripts/generate_phone_stage_node_golden.py')
assert spec and spec.loader
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
from mycelium_router import relay
assert relay._LIVE_PATH_IDLE_SECONDS == 1200
assert relay._LIVE_PATH_MAX_LIFETIME_SECONDS == 4800
frames, cases = {}, []
for kind in ['wrong_source', 'stale_attempt', 'payload_conflict', 'admitted_duplicate', 'continuous_birth']:
    s = g.Scenario(kind, frames)
    s.manifest_locked(g.ENTRY)
    if kind == 'payload_conflict':
        s.advance(1100)
        s.hop(g.ENTRY, 'PREFILL', -1)
    if kind == 'continuous_birth':
        for _ in range(4):
            s.advance(1000)
            s.manifest_locked(g.ENTRY)
        newer = replace(s.manifest, path_attempt=s.manifest.path_attempt+1)
        s.manifest_locked(g.ENTRY, manifest=newer)
        s.advance(800)
        s.manifest_locked(g.ENTRY, manifest=newer)
        s.manifest_locked(g.ENTRY, manifest=replace(newer, path_attempt=newer.path_attempt+1))
    else:
        s.advance(99 if kind == 'payload_conflict' else 1199)
        if kind == 'wrong_source': s.hop(g.STRANGER, 'PREFILL', -1)
        elif kind == 'stale_attempt': s.hop(g.ENTRY, 'PREFILL', -1, attempt=1)
        elif kind == 'payload_conflict': s.hop(g.ENTRY, 'PREFILL', -1, payload_scale=2.0)
        else: s.manifest_locked(g.ENTRY)
        s.advance(1101 if kind == 'payload_conflict' else 1)
        s.hop(g.ENTRY, 'PREFILL', -1)
        if kind == 'admitted_duplicate':
            s.advance(1200)
            s.hop(g.ENTRY, 'PREFILL', -1)
    cases.append(s.record())
for case in cases:
    for step in case['steps']:
        step['frame_hex'] = frames[step.pop('frame')].hex()
        for send in step['sends']:
            send['frame_hex'] = frames[send.pop('frame')].hex()
files = ['mycelium_router/relay.py', 'scripts/generate_phone_stage_node_golden.py',
         'tests/path_cancellation_adversarial/test_path_admission_expiry.py']
for name, module in list(sys.modules.items()):
    if name.startswith('mycelium') and getattr(module, '__file__', None):
        source = Path(module.__file__).resolve()
        assert source.is_relative_to(root), (name, source)
        if source.suffix == '.py': files.append(str(source.relative_to(root)))
result = {'python_root': root.name, 'python_revision':revision,
          'python_tree':subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD^{tree}'],text=True).strip(),
          'source_sha256': {f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in sorted(set(files))}, 'cases':cases}
out = a.output or Path(__file__).with_name('expiry_parity.json')
out.write_text(json.dumps(result, sort_keys=True, indent=2)+'\n')
print(out, len(cases), sum(len(c['steps']) for c in cases))
