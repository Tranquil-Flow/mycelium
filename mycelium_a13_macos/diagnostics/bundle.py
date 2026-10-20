# SPDX-License-Identifier: AGPL-3.0-only
"""One-click support bundle: a zip of the redacted diagnostics, a redacted app log tail and the versions.

Nothing is sent anywhere; the owner downloads the file and decides whom to give it to.
"""
from __future__ import annotations

import io
import json
import os
import platform
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

from .collect import SCHEMA, collect
from .redact import Redactor

LOG_ENV = "MYCELIUM_A13_APP_LOG"
LOG_TAIL_BYTES = 60_000
LOG_MAX_BYTES = 80_000
_README = """Mycelium support bundle
=======================

diagnostics.json   what the app saw: clock offsets, peer liveness, command mailbox health, recent chat error
                   codes (never chat text), relay, timings, host load, and the findings with their fixes
findings.txt       the same findings in plain English
app-log-tail.txt   the last lines of the app log
versions.json      app, Python and macOS versions

Redacted before it was written: keys, grants, tokens and cookies, join links and codes, prompts and chat text,
home folder paths and user names, this Mac's host name, other host names and IP addresses (replaced with
placeholders such as <HOME>, <HOST>, <IP>, <TOKEN>, <JOIN-LINK>), and peer names (shown as "Joined Mac 1").
Only counts, error codes, timings and versions are kept. Check the files before you share them.
"""


def _known_secrets(app: Any) -> list[str]:
    secrets = [getattr(app, name, None) for name in ("native_credential", "session", "launch_code")]
    service = getattr(app, "chat", None)
    try:
        with service._lock:
            for record in service._requests.values():
                secrets.append("".join(record.get("tokens") or []))
                secrets.append(record.get("prompt"))
                secrets.extend(record.get("tokens") or [])
    except Exception:
        pass
    return [s.strip() for s in secrets if isinstance(s, str) and len(s.strip()) >= 6]


def _log_path(app: Any) -> Path | None:
    candidates = [getattr(app, "log_path", None), os.environ.get(LOG_ENV)]
    root = getattr(app, "root", None)
    if root is not None:
        candidates.append(Path(root) / "app.log")
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _log_tail(app: Any, redactor: Redactor) -> bytes:
    path = _log_path(app)
    if path is None:
        return (b"app log not available: this build does not know where the app's output is written.\n"
                b"Start the app with its output redirected to <state folder>/app.log, or set "
                + LOG_ENV.encode() + b" to the log file, and download the bundle again.\n")
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - LOG_TAIL_BYTES))
            raw = stream.read()
        if size > LOG_TAIL_BYTES:
            raw = raw.split(b"\n", 1)[-1]  # drop the partial first line
    except OSError:
        return b"app log not available: it could not be read.\n"
    text = redactor.text(raw.decode("utf-8", "replace"))
    return text.encode("utf-8")[-LOG_MAX_BYTES:]


def versions(app: Any) -> dict[str, Any]:
    try:
        from mycelium_a13_macos import __version__ as app_version
    except Exception:
        app_version = "unknown"
    return {"app_version": app_version, "diagnostics_schema": SCHEMA, "python": platform.python_version(),
            "platform": platform.system(), "macos": platform.mac_ver()[0] or None, "machine": platform.machine(),
            "role": getattr(app, "installation_role", None), "frozen": bool(getattr(sys, "frozen", False))}


def _findings_text(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "No problems found.\n"
    lines = []
    for item in findings:
        lines += [f"[{item['severity'].upper()}] {item['title']}", f"  Why: {item['detail']}",
                  f"  Fix: {item['fix']}", ""]
    return "\n".join(lines)


def file_stamp(now: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def build_bundle(app: Any, *, now: float | None = None) -> bytes:
    now = time.time() if now is None else now
    redactor = Redactor(known_secrets=_known_secrets(app))
    document = redactor.json(collect(app, now=now, anonymize=True))
    files = {
        "diagnostics.json": json.dumps(document, indent=2, sort_keys=True).encode(),
        "findings.txt": redactor.text(_findings_text(document.get("findings") or [])).encode(),
        "app-log-tail.txt": _log_tail(app, redactor),
        "versions.json": json.dumps(redactor.json(versions(app)), indent=2, sort_keys=True).encode(),
        "README.txt": _README.encode(),
    }
    buffer = io.BytesIO()
    stamp = time.gmtime(now)[:6]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, raw)
    return buffer.getvalue()
