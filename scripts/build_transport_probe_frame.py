#!/usr/bin/env python3
"""Build one valid bounded Router frame for physical transport probing."""
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_router.contracts import TokenEvent
from mycelium_router.wire import encode_frame


def build_probe_frame() -> bytes:
    """Return the fixed valid frame used by directed transport probes."""

    return encode_frame(
        TokenEvent(
            request_id="a2-fresh-member-transport-probe",
            path_id="a2-probe-path",
            path_attempt=0,
            token_index=0,
            token_id=1,
            sampling_counter=1,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_bytes(build_probe_frame())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
