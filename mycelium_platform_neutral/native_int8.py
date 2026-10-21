# SPDX-License-Identifier: AGPL-3.0-only
"""Digest-pinned binding of the native A9 int8 weight-only (W8A32) kernel.

The kernel source is ``native/a9_int8_kernel/mycelium_int8.c``.  On the Windows
gate host it is built with MSVC (``/arch:AVX2 /openmp``) into
``mycelium_int8.dll``; on macOS/Linux it can be built as a shared library for
conformance tests only.  The library is loaded only when its bytes match the
exact declared ``sha256:`` digest, from a regular non-symlink file.

The linear keeps the int8 matrix and its row scales as the persistent packed
state and widens each weight in registers, so decode performs zero full-weight
materializations.  The provider ledger counts calls and proves that the packed
state is unchanged; it is never acceleration or qualification evidence by
itself (``runtime_efficiency`` decides that from a measured benchmark).
"""

from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import stat
import sys
from typing import Any, Mapping

import numpy as np

PROTOCOL = "mycelium.a9_native_int8_provider.v1"
ABI_VERSION = 1
SUPPORTED_DISPATCH = frozenset({"avx2_fma", "scalar"})
MAX_LIBRARY_BYTES = 64 * 1024 * 1024


class NativeInt8Unavailable(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class NativeInt8Library:
    """One digest-pinned, ABI-checked kernel library."""

    def __init__(self, path: str | Path, *, expected_digest: str) -> None:
        location = Path(path)
        if not location.is_absolute():
            raise NativeInt8Unavailable("provider_library_path_not_absolute")
        try:
            info = os.lstat(location)
        except OSError:
            raise NativeInt8Unavailable("provider_library_absent") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise NativeInt8Unavailable("provider_library_absent")
        if info.st_size > MAX_LIBRARY_BYTES:
            raise NativeInt8Unavailable("provider_library_oversized")
        raw = location.read_bytes()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if digest != expected_digest:
            raise NativeInt8Unavailable("provider_library_digest_mismatch")
        loader = getattr(ctypes, "WinDLL", None) if sys.platform == "win32" else ctypes.CDLL
        try:
            handle = loader(str(location))
        except OSError:
            raise NativeInt8Unavailable("provider_library_load_failed") from None
        try:
            handle.mycelium_int8_abi_version.restype = ctypes.c_int
            handle.mycelium_int8_dispatch.restype = ctypes.c_char_p
            handle.mycelium_int8_threading.restype = ctypes.c_char_p
            linear = handle.mycelium_w8a32_linear
        except AttributeError:
            raise NativeInt8Unavailable("provider_library_abi_mismatch") from None
        if handle.mycelium_int8_abi_version() != ABI_VERSION:
            raise NativeInt8Unavailable("provider_library_abi_mismatch")
        dispatch = handle.mycelium_int8_dispatch().decode("ascii")
        if dispatch not in SUPPORTED_DISPATCH:
            raise NativeInt8Unavailable("provider_library_abi_mismatch")
        linear.restype = ctypes.c_int
        linear.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int64,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int64,
            ctypes.c_int64,
            ctypes.c_void_p,
            ctypes.c_int32,
        ]
        self._linear = linear
        self.path_name = location.name
        self.build_digest = digest
        self.dispatch = dispatch
        self.threading = handle.mycelium_int8_threading().decode("ascii")
        self.host_platform = sys.platform

    def linear(
        self, hidden: np.ndarray, values: np.ndarray, scales: np.ndarray, *, threads: int
    ) -> np.ndarray:
        if values.dtype != np.int8 or values.ndim != 2 or scales.dtype != np.float32:
            raise NativeInt8Unavailable("weight_representation_unsupported")
        if not values.flags.c_contiguous or not scales.flags.c_contiguous:
            raise NativeInt8Unavailable("weight_representation_unsupported")
        out_features, in_features = values.shape
        if scales.shape != (out_features,) or hidden.shape[-1] != in_features:
            raise NativeInt8Unavailable("activation_width_mismatch")
        leading = hidden.shape[:-1]
        rows = np.ascontiguousarray(hidden.reshape(-1, in_features), dtype=np.float32)
        result = np.empty((rows.shape[0], out_features), dtype=np.float32)
        code = self._linear(
            rows.ctypes.data,
            rows.shape[0],
            values.ctypes.data,
            scales.ctypes.data,
            out_features,
            in_features,
            result.ctypes.data,
            int(threads),
        )
        if code != 0:
            raise NativeInt8Unavailable("provider_call_failed")
        return result.reshape(*leading, out_features)


class NativeInt8LinearProvider:
    """The ``numpy_runtime`` int8 linear provider backed by one pinned library.

    ``calls`` and ``packed_bytes_seen`` are monotonic; the provider never
    allocates a float copy of a weight matrix, so its full-weight
    materialization counter is structurally zero and is reported as such.
    """

    def __init__(self, library: NativeInt8Library, *, threads: int = 1) -> None:
        if not isinstance(library, NativeInt8Library):
            raise NativeInt8Unavailable("provider_not_bound")
        if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= 256:
            raise NativeInt8Unavailable("provider_threads_invalid")
        self.library = library
        self.threads = threads
        self.calls = 0
        self.full_weight_materializations = 0

    def __call__(self, hidden: np.ndarray, values: np.ndarray, scales: np.ndarray) -> np.ndarray:
        self.calls += 1
        return self.library.linear(hidden, values, scales, threads=self.threads)

    def report(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "build_digest": self.library.build_digest,
            "dispatch": self.library.dispatch,
            "threading": self.library.threading,
            "threads": self.threads,
            "host_platform": self.library.host_platform,
            "calls": self.calls,
            "full_weight_materializations": self.full_weight_materializations,
            "accelerated": False,
            "qualified": False,
            "claim_boundary": (
                "a bound kernel library and call counter; acceleration and "
                "qualification are decided only by the measured A9 benchmark"
            ),
        }


ENV_LIBRARY = "MYCELIUM_A9_INT8_PROVIDER_LIBRARY"
ENV_DIGEST = "MYCELIUM_A9_INT8_PROVIDER_DIGEST"
ENV_THREADS = "MYCELIUM_A9_INT8_PROVIDER_THREADS"
ENV_REPORT = "MYCELIUM_A9_INT8_PROVIDER_REPORT"


def provider_from_environment(environ: Mapping[str, str] | None = None) -> NativeInt8LinearProvider | None:
    """The opt-in provider a gate host selects with environment variables.

    Unset library -> ``None`` (the qualified default path).  A set library
    without a digest, or one that fails pinning, raises: a requested provider is
    never silently replaced by the default path.  With ``ENV_REPORT`` set, the
    provider report (build digest, dispatch, call count) is written there at
    process exit as gate evidence.
    """

    env = os.environ if environ is None else environ
    location = env.get(ENV_LIBRARY)
    if not location:
        return None
    expected = env.get(ENV_DIGEST)
    if not expected:
        raise NativeInt8Unavailable("provider_library_digest_missing")
    try:
        threads = int(env.get(ENV_THREADS, "1"))
    except ValueError:
        raise NativeInt8Unavailable("provider_threads_invalid") from None
    provider = NativeInt8LinearProvider(NativeInt8Library(location, expected_digest=expected), threads=threads)
    report_path = env.get(ENV_REPORT)
    if report_path:
        import atexit
        import json

        def _write_report() -> None:
            target = Path(report_path)
            target.write_text(json.dumps(provider.report(), sort_keys=True) + "\n", encoding="utf-8")

        atexit.register(_write_report)
    return provider


__all__ = [
    "ENV_DIGEST",
    "ENV_LIBRARY",
    "ENV_REPORT",
    "ENV_THREADS",
    "provider_from_environment",
    "NativeInt8Library",
    "NativeInt8LinearProvider",
    "NativeInt8Unavailable",
    "PROTOCOL",
    "file_digest",
]
