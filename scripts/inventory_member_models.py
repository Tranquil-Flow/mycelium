#!/usr/bin/env python3
"""Publish a signed model inventory from one member's local read-only cache."""
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_member_models import (
    create_member_model_inventory,
    inventory_entry_from_catalog_projection,
)
from mycelium_model_catalog import scan_huggingface_cache
from mycelium_node.identity import load_node_signer


def _private_parent(path: Path) -> None:
    parent = path.parent.resolve(strict=True)
    metadata = parent.lstat()
    if (
        not path.is_absolute()
        or parent != path.parent
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise RuntimeError("member_model_inventory_output_unsafe")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--member-id", required=True)
    parser.add_argument("--membership-generation", type=int, required=True)
    parser.add_argument("--identity-key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ttl-seconds", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.ttl_seconds <= 86_400:
        raise RuntimeError("member_model_inventory_ttl_invalid")
    cache = args.cache_root.resolve(strict=True)
    output = args.output
    _private_parent(output)
    signer = load_node_signer(
        args.identity_key_file,
        endpoint_id=f"member-model-{args.member_id}",
    )
    observed = int(time.time() * 1_000)
    entries = [
        inventory_entry_from_catalog_projection(entry.projection())
        for entry in scan_huggingface_cache(cache)
    ]
    bundle = create_member_model_inventory(
        member_id=args.member_id,
        membership_generation=args.membership_generation,
        entries=entries,
        observed_at_unix_ms=observed,
        valid_until_unix_ms=observed + args.ttl_seconds * 1_000,
        signer=signer,
    )
    encoded = (
        json.dumps(bundle, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode()
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "protocol": "mycelium.member_model_inventory_publish.v1",
                "member_id": args.member_id,
                "membership_generation": args.membership_generation,
                "entry_count": len(entries),
                "valid_until_unix_ms": bundle["statement"]["valid_until_unix_ms"],
                "download_authorized": False,
                "route_ready": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
