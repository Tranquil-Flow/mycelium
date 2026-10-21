# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
IROH_CRATE = ROOT / "native" / "iroh_transport"
IROH_TARGET = Path(os.environ.get("CARGO_TARGET_DIR", str(IROH_CRATE / "target")))
if not IROH_TARGET.is_absolute():
    IROH_TARGET = IROH_CRATE / IROH_TARGET
IROH_BINARY = IROH_TARGET / "debug" / (
    "mycelium-iroh-sidecar.exe" if sys.platform == "win32" else "mycelium-iroh-sidecar"
)
#: The untracked vendored-source config a Windows host needs for an offline build
#: (see the A9 runday sidecar step); absent from a plain git-archive checkout.
IROH_OFFLINE_CONFIG = IROH_CRATE / ".cargo" / "config.toml"
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
    # Windows: the MSVC toolchain and cargo home need these (absent elsewhere).
    "SYSTEMROOT",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "TEMP",
    "TMP",
    "INCLUDE",
    "LIB",
    "LIBPATH",
)


@pytest.fixture(scope="session")
def native_iroh_sidecar_binary() -> Path:
    if sys.platform == "win32" and not IROH_OFFLINE_CONFIG.is_file():
        pytest.skip(
            "offline sidecar build on Windows needs native/iroh_transport/.cargo/config.toml "
            "pointing at the vendored crates"
        )
    environment = {
        key: os.environ[key]
        for key in _BUILD_ENVIRONMENT_KEYS
        if key in os.environ
    }
    environment["CARGO_NET_OFFLINE"] = "true"
    # Use one target identity for the checked build and returned executable,
    # even if a test or plugin changed the environment after module import.
    environment["CARGO_TARGET_DIR"] = str(IROH_TARGET)
    subprocess.run(
        ["cargo", "build", "--locked", "--bin", "mycelium-iroh-sidecar"],
        cwd=IROH_CRATE,
        env=environment,
        check=True,
    )
    assert IROH_BINARY.is_file()
    return IROH_BINARY
