# SPDX-License-Identifier: AGPL-3.0-only
"""Shared, platform-neutral A13 build helper (Windows and Linux builders).

* the app-tree guard (ERRATA-6: the repository is the app's source of truth);
* a static, never-executing Python import closure and its byte-exact staging;
* deterministic archives (zip and tar.gz: sorted entries, fixed timestamps, no
  host metadata);
* the payload tree digest the signed manifest binds, which an installer can
  re-verify on the extracted folder;
* loading the pinned Ed25519 release key without ever exposing its bytes.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import zipfile
from typing import Any


class PayloadError(RuntimeError):
    """A payload tree that cannot be digested safely (a link, a special file)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require_app_tree_root(root: Path, markers: tuple[str, ...]) -> dict[str, str]:
    """Fail before packaging if this checkout does not look like the app's own tree."""
    missing = [relative for relative in markers if not (root / relative).is_file()]
    if missing:
        raise SystemExit("build_root_not_the_app_tree:" + ",".join(missing))
    return {"root": str(root)}


def runtime_source_closure(root: Path, seeds: tuple[str, ...], *, label: str = "runtime") -> tuple[str, ...]:
    """Static transitive Python-import closure from ``seeds``; nothing is imported or executed.

    A package import stages the whole package (real ``__init__.py`` semantics),
    a plain top-level module import stages that one file; ``tests`` never enter.
    """
    selected: set[str] = set()
    for seed in seeds:
        path = root / seed
        if path.is_file():
            selected.add(seed)
        elif path.is_dir():
            selected.update(
                item.relative_to(root).as_posix() for item in path.rglob("*.py") if item.is_file()
            )
        else:
            raise SystemExit(f"{label}_runtime_closure_seed_missing:{seed}")
    inspected: set[str] = set()
    while selected - inspected:
        relative = min(selected - inspected)
        source = root / relative
        if not source.is_file() or not source.resolve().is_relative_to(root):
            raise SystemExit(f"{label}_runtime_closure_file_missing:{relative}")
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=relative)
        inspected.add(relative)
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.append(node.module)
        for module in modules:
            head = module.split(".")[0]
            if head == "tests" or head.startswith("test_"):
                continue
            module_path = root / (head + ".py")
            folder = root / head
            if module_path.is_file():
                selected.add(module_path.relative_to(root).as_posix())
            elif folder.is_dir():
                selected.update(
                    item.relative_to(root).as_posix()
                    for item in folder.rglob("*.py")
                    if item.is_file() and "__pycache__" not in item.parts
                )
    return tuple(sorted(selected))


def stage_files(root: Path, destination: Path, files: tuple[str, ...]) -> dict[str, Any]:
    """Copy ``files`` (root-relative) byte-exact; never a symlink, never outside root."""
    destination.mkdir(parents=True, exist_ok=False)
    digests: dict[str, str] = {}
    for relative in files:
        source = root / relative
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise SystemExit("runtime_source_unsafe")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = source.read_bytes()
        target.write_bytes(raw)
        digests[relative] = hashlib.sha256(raw).hexdigest()
    return {"files": len(files), "sha256": digests}


def stage_data_tree(root: Path, relative_dir: str, destination: Path, digests: dict[str, str]) -> None:
    """Add a non-Python data folder (web assets) to a staged source, recording digests."""
    for path in sorted((root / relative_dir).rglob("*")):
        if path.is_symlink():
            raise SystemExit("runtime_source_unsafe")
        if path.is_file():
            target = destination / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = path.read_bytes()
            target.write_bytes(raw)
            digests[path.relative_to(root).as_posix()] = hashlib.sha256(raw).hexdigest()


def git_head(root: Path) -> str | None:
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    head = result.stdout.strip()
    return head or None


def deterministic_zip(source_dir: Path, zip_path: Path) -> bytes:
    """Sorted entries, fixed timestamp, no host-dependent metadata."""
    files = sorted(p for p in source_dir.rglob("*") if p.is_file())
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(
                str(path.relative_to(source_dir)).replace("\\", "/"), date_time=(1980, 1, 1, 0, 0, 0)
            )
            info.external_attr = 0o600 << 16
            archive.writestr(info, path.read_bytes())
    return zip_path.read_bytes()


def deterministic_zip_with_links(source_dir: Path, zip_path: Path, *, prefix: str) -> bytes:
    """A deterministic zip that keeps symbolic links and executable bits (macOS bundles).

    Links are stored as Unix ``S_IFLNK`` entries whose data is the target, which
    ``ditto -x -k`` and Archive Utility restore as links; regular files keep
    0755/0644 by their executable bit; fixed timestamps, sorted entries.
    """
    import stat as _stat

    entries = sorted(source_dir.rglob("*"))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in entries:
            name = f"{prefix}/{path.relative_to(source_dir).as_posix()}"
            if path.is_symlink():
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (_stat.S_IFLNK | 0o755) << 16
                archive.writestr(info, os.readlink(path))
            elif path.is_dir():
                info = zipfile.ZipInfo(name + "/", date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (_stat.S_IFDIR | 0o755) << 16
                archive.writestr(info, b"")
            elif path.is_file():
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                mode = 0o755 if os.access(path, os.X_OK) else 0o644
                info.external_attr = (_stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
            else:
                raise PayloadError("payload_special_file")
    return zip_path.read_bytes()


def deterministic_tar_gz(source_dir: Path, tar_path: Path, *, prefix: str) -> bytes:
    """tar.gz with sorted entries under ``prefix/``, mtime 0, uid/gid 0, no names.

    Executable bits are kept (0755 for files that were executable, 0644
    otherwise, 0755 directories) so the extracted app and ``install.sh`` run.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        entries = sorted(source_dir.rglob("*"))
        for path in [source_dir, *entries]:
            if path.is_symlink():
                raise PayloadError("payload_link_refused")
            relative = path.relative_to(source_dir).as_posix()
            name = prefix if relative == "." else f"{prefix}/{relative}"
            info = tarfile.TarInfo(name)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            else:
                raw = path.read_bytes()
                info.size = len(raw)
                info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                archive.addfile(info, io.BytesIO(raw))
    raw_tar = buffer.getvalue()
    compressed = gzip.compress(raw_tar, compresslevel=9, mtime=0)
    tar_path.write_bytes(compressed)
    return compressed


def payload_tree(root: Path) -> dict[str, str]:
    """``{posix relative path: sha256}`` of every regular file; links are refused."""
    tree: dict[str, str] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise PayloadError("payload_link_refused")
        for name in files:
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                raise PayloadError("payload_link_refused")
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                while block := handle.read(1 << 20):
                    digest.update(block)
            tree[path.relative_to(root).as_posix()] = digest.hexdigest()
    return dict(sorted(tree.items()))


def payload_digest(root: Path) -> str:
    """The digest a manifest's ``package_digest`` binds: canonical JSON of the tree."""
    raw = json.dumps(payload_tree(root), sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def bundle_tree(root: Path) -> dict[str, str]:
    """Link-aware tree for a macOS ``.app``: ``sha256:<hex>`` per regular file,
    ``link:<target>`` per symbolic link.

    A link must be relative and must resolve inside the bundle; an absolute
    target or one that escapes the bundle (directly or through another link)
    is refused, as is any special file (fifo, socket, device).
    """
    base = root.resolve(strict=True)
    tree: dict[str, str] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        for name in [*directories, *files]:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                if os.path.isabs(target):
                    raise PayloadError("payload_link_absolute")
                lexical = Path(os.path.normpath(base / path.parent.relative_to(root) / target))
                if lexical != base and not lexical.is_relative_to(base):
                    raise PayloadError("payload_link_escapes_bundle")  # even if it points nowhere
                try:
                    resolved = path.resolve(strict=True)
                except (OSError, RuntimeError):
                    raise PayloadError("payload_link_dangling") from None
                if resolved != base and not resolved.is_relative_to(base):
                    raise PayloadError("payload_link_escapes_bundle")
                tree[relative] = "link:" + target
            elif name in files:
                if not path.is_file():
                    raise PayloadError("payload_special_file")
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    while block := handle.read(1 << 20):
                        digest.update(block)
                tree[relative] = "sha256:" + digest.hexdigest()
            elif not path.is_dir():
                raise PayloadError("payload_special_file")
    return dict(sorted(tree.items()))


def bundle_digest(root: Path) -> str:
    """The digest a macOS manifest's ``package_digest`` binds (links included)."""
    raw = json.dumps(bundle_tree(root), sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def load_pinned_signer(key_path: Path, *, endpoint_id: str):
    """Load the pinned Ed25519 release key (exactly 32 raw private bytes).

    The raw bytes are never logged, printed, or placed in any returned/written
    structure -- only the signer (whose public API exposes no private-key
    material) is returned.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from mycelium_qualification.signing import Ed25519EvidenceSigner

    raw = key_path.read_bytes()
    try:
        if len(raw) != 32:
            raise SystemExit("signing_key_file_must_hold_32_raw_private_bytes")
        private_key = Ed25519PrivateKey.from_private_bytes(raw)
        public_key_bytes = private_key.public_key().public_bytes_raw()
    finally:
        raw = b"\x00" * 32  # best-effort: drop the reference to the real secret
    return Ed25519EvidenceSigner(
        endpoint_id=endpoint_id, _private_key=private_key, _public_key_bytes=public_key_bytes
    )


__all__ = [
    "PayloadError",
    "bundle_digest",
    "bundle_tree",
    "deterministic_tar_gz",
    "deterministic_zip",
    "deterministic_zip_with_links",
    "git_head",
    "load_pinned_signer",
    "payload_digest",
    "payload_tree",
    "require_app_tree_root",
    "runtime_source_closure",
    "stage_data_tree",
    "stage_files",
]
