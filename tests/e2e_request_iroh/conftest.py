from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
IROH_CRATE = ROOT / "native" / "iroh_transport"
IROH_TARGET = Path(os.environ.get("CARGO_TARGET_DIR", str(IROH_CRATE / "target")))
if not IROH_TARGET.is_absolute():
    IROH_TARGET = IROH_CRATE / IROH_TARGET
IROH_BINARY = IROH_TARGET / "debug" / "mycelium-iroh-sidecar"
_BUILD_ENVIRONMENT_KEYS = (
    "CARGO_TARGET_DIR",
    "CARGO_INCREMENTAL",
    "CARGO_BUILD_JOBS",
    "CARGO_HOME",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "RUSTUP_HOME",
    "TMPDIR",
)


@pytest.fixture(scope="session")
def native_iroh_sidecar_binary() -> Path:
    environment = {
        key: os.environ[key]
        for key in _BUILD_ENVIRONMENT_KEYS
        if key in os.environ
    }
    environment["CARGO_NET_OFFLINE"] = "true"
    subprocess.run(
        ["cargo", "build", "--locked", "--bin", "mycelium-iroh-sidecar"],
        cwd=IROH_CRATE,
        env=environment,
        check=True,
    )
    assert IROH_BINARY.is_file()
    return IROH_BINARY
