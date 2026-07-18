from __future__ import annotations

import sys
from pathlib import Path

from .validator import (
    PreflightValidationError,
    canonical_error_bytes,
    canonical_json_bytes,
    read_plan_file,
    validate_and_generate,
)


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise PreflightValidationError("invalid_arguments", "/")
        encoded = read_plan_file(Path(sys.argv[1]))
        source_tree_root = Path(__file__).resolve().parents[1]
        generated = validate_and_generate(encoded, source_tree_root=source_tree_root)
        output = canonical_json_bytes(generated)
        status = 0
    except PreflightValidationError as error:
        output = canonical_error_bytes(error)
        status = 2
    except Exception:
        output = canonical_error_bytes(PreflightValidationError("internal_error", "/"))
        status = 2
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
