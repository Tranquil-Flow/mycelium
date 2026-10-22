# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare (never apply) a site deployment from an exact, clean detached checkout.

Run after the final pin commit. Reads the two current LaunchAgent plists; writes
only a NEW state directory. Existing stages are never overwritten.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--release', required=True, type=Path)
    parser.add_argument('--stage', required=True, type=Path)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('site_switch', Path(__file__).with_name('switch.py'))
    assert spec and spec.loader
    switch = importlib.util.module_from_spec(spec); spec.loader.exec_module(switch)
    release, stage, home = args.release.resolve(), args.stage.resolve(), Path.home()
    if Path(__file__).resolve().parent != release/'docs/design/site-v2-p3/deploy-tools':
        raise ValueError('Prepare must run from the exact release checkout')
    switch.unquarantined('/usr/bin/git', subprocess.run)
    head = switch.checked(['/usr/bin/git','-C',str(release),'rev-parse','HEAD'], subprocess.run).strip()
    if switch.checked(['/usr/bin/git','-C',str(release),'status','--porcelain'], subprocess.run).strip():
        raise ValueError('Candidate checkout is dirty')
    if subprocess.run(['/usr/bin/git','-C',str(release),'symbolic-ref','-q','HEAD'], capture_output=True).returncode != 1:
        raise ValueError('Candidate must be detached')
    if release != home/'mycelium-site-deploy'/head[:8]:
        raise ValueError('Unexpected release path')
    if stage != home/'mycelium-hosted-demo/state'/('site-deploy-'+head[:8]+'-20261022b'):
        raise ValueError('Unexpected state path')
    docs = {}
    for label in switch.LABELS:
        raw = (home/'Library/LaunchAgents'/(label+'.plist')).read_bytes()
        docs[label] = (raw, switch.load_plist(raw))
    rollback_paths = {doc['WorkingDirectory'] for _,doc in docs.values()}
    if len(rollback_paths) != 1:
        raise ValueError('Site jobs do not share the rollback checkout')
    rollback = rollback_paths.pop()
    old_head = switch.checked(['/usr/bin/git','-C',rollback,'rev-parse','HEAD'], subprocess.run).strip()
    if old_head != '790d3caa9a3333284df02c6c9e4766d84026ae73':
        raise ValueError('Live rollback base changed; require a new review')
    # No writes to LaunchAgents, database, or any process control path.
    stage.mkdir(mode=0o700)
    config = {'candidate':{'path':str(release),'sha':head},
              'rollback':{'path':rollback,'sha':old_head},'staged_sha256':{},'live_plist_sha256':{}}
    for label,(raw,doc) in docs.items():
        config['live_plist_sha256'][label] = hashlib.sha256(raw).hexdigest()
        (stage/(label+'.original.plist')).write_bytes(raw)
        (stage/(label+'.rollback.plist')).write_bytes(plistlib.dumps(doc))
        (stage/(label+'.plist')).write_bytes(plistlib.dumps(switch.candidate_doc(doc,release)))
    for name in ('switch.py','http_verify.py','rehearse.py'):
        shutil.copyfile(Path(__file__).with_name(name),stage/name)
    for path in stage.iterdir():
        config['staged_sha256'][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (stage/'deployment.json').write_text(json.dumps(config,indent=2)+'\n')
    result = switch.switch(stage,home,'deploy')
    (stage/'prepare-dry-run.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'stage':str(stage),'candidate':head,'dry_run':result},indent=2))


if __name__ == '__main__':
    main()
