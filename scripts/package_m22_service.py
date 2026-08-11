#!/usr/bin/env python3
"""Build one owner-only durable Mycelium launchd/systemd service package."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_service_package import ServicePackageError, write_service_package  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        value = json.loads(args.config.read_text("utf-8"))
        if not isinstance(value, dict):
            raise ServicePackageError("service_config_shape_invalid")
        result = write_service_package(value, args.output_root)
    except (OSError, json.JSONDecodeError, ServicePackageError) as exc:
        print(getattr(exc, "args", ["service_package_failed"])[0], file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
