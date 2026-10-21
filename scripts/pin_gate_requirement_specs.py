#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Pin (or ``--check``) the digests of the shipped gate requirement specs.

Every ``mycelium_gate_requirements/specs/<gate>.v1.json`` is validated and its
canonical digest is written to ``specs/index.v1.json``.  ``--check`` exits 1
when any pinned digest drifted, without writing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_gate_requirements.spec import (  # noqa: E402
    INDEX_FILE,
    INDEX_PROTOCOL,
    SPEC_DIR,
    load_spec,
)


def index_document(spec_dir: Path = SPEC_DIR) -> dict:
    specs = {}
    for path in sorted(spec_dir.glob("*.v1.json")):
        if path.name == INDEX_FILE:
            continue
        spec = load_spec(path)
        if spec.gate_id in specs:
            raise SystemExit(f"duplicate gate spec: {spec.gate_id}")
        specs[spec.gate_id] = {"file": path.name, "spec_digest": spec.digest}
    return {"protocol": INDEX_PROTOCOL, "specs": dict(sorted(specs.items()))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    expected = json.dumps(index_document(), indent=2) + "\n"
    target = SPEC_DIR / INDEX_FILE
    if args.check:
        current = target.read_text("utf-8") if target.exists() else ""
        if current != expected:
            print("gate requirement spec index drift", file=sys.stderr)
            return 1
        return 0
    target.write_text(expected, "utf-8")
    print(expected, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
