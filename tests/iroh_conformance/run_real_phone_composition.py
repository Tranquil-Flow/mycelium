#!/usr/bin/env python3
"""Invoke ONLY through lead/run_bounded.py: shares its lock, nice and timeout.
Source-aware offline/locked Cargo builds precede the software-only composition.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--evidence', type=Path, required=True)
    a = p.parse_args()
    a.evidence.mkdir(parents=True,exist_ok=False)
    assert not a.evidence.resolve().is_relative_to(ROOT), 'evidence outside source tree'
    sources = ['native/mobile_core/src/node.rs','native/mobile_core/src/node_real_composition.rs',
               'tests/iroh_conformance/real_phone_composition_client.py',
               'tests/iroh_conformance/run_real_phone_composition.py']
    source_hashes = {}
    for relative in sources:
        source = ROOT / relative
        destination = a.evidence / 'source' / relative
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes(source.read_bytes())
        source_hashes[relative] = digest(source)
    target = Path(os.environ['CARGO_TARGET_DIR'])
    locks = [ROOT/'native'/x/'Cargo.lock' for x in ('iroh_transport','mobile_core')]
    before = {str(p.relative_to(ROOT)):digest(p) for p in locks}
    env = dict(os.environ,PHONE_COMPOSITION_EVIDENCE=str(a.evidence.resolve()),PYTHONDONTWRITEBYTECODE='1')
    commands = [
        ['cargo','build','--manifest-path','native/iroh_transport/Cargo.toml','--offline','--locked','--bin','mycelium-iroh-sidecar'],
        ['cargo','test','--manifest-path','native/mobile_core/Cargo.toml','--offline','--locked','--features','net','--lib','real_phone_composition','--','--nocapture','--test-threads=1'],
    ]
    result = {'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'synthetic_cpu_only':True,'physical_devices':False,'commands':[], 'locks_before':before,
              'harness_sha256':source_hashes}
    rc = 1
    try:
        for command in commands:
            print('RUN',json.dumps(command),flush=True)
            rc = subprocess.run(command,cwd=ROOT,env=env,timeout=700).returncode
            result['commands'].append({'argv':command,'exit_code':rc})
            if command[1] == 'build' and rc == 0:
                result['sidecar_sha256'] = digest(target/'debug/mycelium-iroh-sidecar')
            if rc: break
    finally:
        result['locks_after']={str(p.relative_to(ROOT)):digest(p) for p in locks}
        assert result['locks_after'] == before, 'Cargo lockfile changed'
        result['exit_code']=rc
        (a.evidence/'execution.json').write_text(json.dumps(result,indent=2)+'\n')
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
