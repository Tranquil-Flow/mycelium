#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Generate labelled local input through the ordinary product projector."""
import json
from pathlib import Path
import runpy
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
fixture = runpy.run_path(str(ROOT/'tests/a14_acceptance/conformance.py'))['conformance_snapshot']()
encoded = json.dumps(fixture, sort_keys=True, indent=2) + '\n'
path = ROOT/'ui/web/src/test/a14Conformance.json'
if '--check' in sys.argv:
    if path.read_text() != encoded:
        raise SystemExit('A14 conformance fixture drift')
else:
    path.write_text(encoded)
print('A14 local conformance fixture verified; physical_devices=0')
