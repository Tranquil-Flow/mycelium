from __future__ import annotations

import ast
import builtins
import inspect

import pytest

import mycelium_qualification_diff.inspector as inspector_module
from mycelium_qualification_diff import EvidenceDiffError, inspect_evidence_diff

from .conftest import make_bundle, manifest_bytes


def test_inspector_imports_no_runtime_io_network_clock_or_worker_modules() -> None:
    tree = ast.parse(inspect.getsource(inspector_module))
    imported_roots: set[str] = set()
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)

    assert not imported_roots.intersection(
        {
            "asyncio",
            "concurrent",
            "multiprocessing",
            "mycelium_gossip",
            "mycelium_qualification",
            "mycelium_router",
            "os",
            "pathlib",
            "requests",
            "socket",
            "subprocess",
            "threading",
            "time",
            "urllib",
        }
    )
    assert not called_names.intersection({"eval", "exec", "open"})


def test_inspection_uses_only_explicit_in_memory_files_and_does_not_mutate_inputs(
    monkeypatch,
) -> None:
    baseline_manifest, baseline_files = make_bundle(
        {"run/evidence.json": {"endpoint_id": "private-a"}}
    )
    candidate_manifest, candidate_files = make_bundle(
        {"run/evidence.json": {"endpoint_id": "private-b"}}
    )
    baseline_snapshot = dict(baseline_files)
    candidate_snapshot = dict(candidate_files)

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("filesystem access forbidden")

    monkeypatch.setattr(builtins, "open", forbidden_open)
    output = inspect_evidence_diff(
        baseline_manifest,
        baseline_files,
        candidate_manifest,
        candidate_files,
    )

    assert output
    assert baseline_files == baseline_snapshot
    assert candidate_files == candidate_snapshot
    assert baseline_manifest == bytes(baseline_manifest)
    assert candidate_manifest == bytes(candidate_manifest)


def test_errors_never_echo_secret_bytes_or_unsafe_paths() -> None:
    secret_path = "../PRIVATE-PATH-CANARY.json"
    files = {secret_path: b'{"secret":"PRIVATE-BYTE-CANARY"}'}
    manifest = manifest_bytes(files)

    with pytest.raises(EvidenceDiffError) as captured:
        inspect_evidence_diff(manifest, files, manifest, files)

    rendered = str(captured.value)
    assert captured.value.code == "unsafe_evidence_path"
    assert "PRIVATE-PATH-CANARY" not in rendered
    assert "PRIVATE-BYTE-CANARY" not in rendered
