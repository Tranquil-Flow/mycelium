#!/usr/bin/env python3
"""Generate or verify the M22 source/binary/model SBOM without network access."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_m22_sbom import (  # noqa: E402
    build_sbom,
    file_component,
    validate_sbom,
    version_component,
)
from mycelium_qualification.evidence import canonical_json_bytes  # noqa: E402

PYTHON_PACKAGES = ("cryptography", "huggingface_hub", "mlx", "numpy", "pytest", "tokenizers", "typing_extensions", "zstandard")


def _revision() -> str:
    completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True)
    return completed.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-root", type=Path, action="append", default=[])
    parser.add_argument("--binary", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        try:
            validate_sbom(json.loads(args.output.read_text("utf-8")))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print("m22 sbom verified")
        return 0
    components = [
        version_component(kind="runtime", name="python", version=platform.python_version()),
        file_component(kind="python-lock", name="python-requirements.lock", path=ROOT / "release" / "python-requirements.lock"),
        file_component(kind="node-lock", name="ui-web-package-lock", path=ROOT / "ui" / "web" / "package-lock.json"),
        file_component(kind="rust-lock", name="iroh-transport-cargo-lock", path=ROOT / "native" / "iroh_transport" / "Cargo.lock"),
        file_component(kind="contract", name="contract-manifest-v1", path=ROOT / "contracts" / "contract-manifest.v1.json"),
    ]
    for package in PYTHON_PACKAGES:
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            if package == "mlx" and sys.platform != "darwin":
                continue
            raise SystemExit(f"m22_python_dependency_missing:{package}")
        components.append(version_component(kind="python-package", name=package, version=version))
    for index, binary in enumerate(args.binary):
        components.append(file_component(kind="binary", name=f"sidecar-{index + 1}", path=binary))
    for root in args.model_root:
        if not root.is_dir() or root.is_symlink():
            raise SystemExit("m22_model_root_invalid")
        model_name = root.parent.parent.name.removeprefix("models--").replace("--", "/")
        revision = root.name
        components.append(version_component(kind="model-revision", name=model_name, version=revision))
        for path in sorted(root.iterdir()):
            if path.is_file() and (path.suffix in {".json", ".safetensors"} or path.name in {"merges.txt", "vocab.json"}):
                components.append(
                    file_component(
                        kind="model-artifact",
                        name=f"{model_name}@{revision}:{path.name}",
                        path=path.resolve(strict=True),
                    )
                )
    document = build_sbom(revision=_revision(), components=components)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(document))
    print(json.dumps({"protocol": document["protocol"], "components": len(document["components"]), "sbom_digest": document["sbom_digest"], "network_download_performed": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
