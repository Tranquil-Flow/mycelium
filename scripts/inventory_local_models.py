#!/usr/bin/env python3
"""Generate a privacy-reduced M17 catalog from an existing local HF cache only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mycelium_model_catalog import catalog_document, scan_huggingface_cache  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hf-cache", type=Path, required=True)
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    entries = scan_huggingface_cache(args.hf_cache)
    document = catalog_document(entries, generation=args.generation)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "protocol": document["protocol"],
        "entries": len(document["entries"]),
        "output": str(args.output),
        "network_access": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
