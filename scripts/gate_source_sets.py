#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Per-gate pinned source sets, derived from the real import/exec closure.

Owner decision D2 (2026-10-13): a physical A-gate's GREEN evidence obtained on
one candidate stands on the final candidate when the gate's pinned source set
is untouched by the diff between the two candidates; any change in a pinned
file forces a rerun.  This module computes that pinned set mechanically, for a
given *commit* (read straight from the Git object store, never from a working
tree), so two candidates can be compared without checking either out.

How the closure is derived (conservative: when unsure, include):

* Python: every ``import``/``from ... import`` (absolute and relative, inside
  ``try``/``TYPE_CHECKING`` blocks too) resolved against the repository root,
  the importing file's directory (script semantics) and any directory the file
  adds to ``sys.path`` with literal path parts.  Ancestor ``__init__.py``
  files are included because importing ``a.b.c`` executes them.
  ``python -m pkg`` adds ``pkg/__main__.py``.  Literal module names passed to
  ``importlib.import_module``/``__import__``/``runpy.run_module``/``-m`` and
  any dotted string that names a repository module are followed.  A file with
  a *non-literal* dynamic import additionally follows every single-word string
  in it that names a repository module.
* Data/contract files: string literals and literal path joins
  (``ROOT / "contracts" / "x.json"``, ``os.path.join``, ``Path(...)``) that
  resolve to a repository file (relative to the root, the file's directory or
  the package root).  Only the whole literal run of a path join counts (not
  its parts), and a path-shaped literal naming a directory pins the
  directory's non-code files (data read by listing).  Pinned data, directory
  expansions and native build inputs are not followed as code.
* JavaScript (``.mjs``/``.ts``/...): relative ``import``/``export``/
  ``import()``/``require()``/``new URL(.., import.meta.url)`` specifiers, plus
  the same literal rules.  A web build unit (``npm run build[:live]``) pins
  the bundle's import closure from ``index.html`` and its build configuration.
* Native: a Cargo crate (or a ``native/<dir>`` C unit) is pinned in full
  (``Cargo.toml``, ``Cargo.lock``, ``build.rs``, ``src/**``...) when any pinned
  executable text (Python/JS/shell, not data) names it (crate directory,
  package/bin/lib name), together with its Cargo ``path =`` dependencies.
* Out-of-repo run-day drivers (``runs/v2/runday`` etc.) are recorded with
  their sha256 and flagged ``versioned_in_git: false``; their text is scanned
  for repository entry points (``scripts/x.py``, ``-m module``, ``npm run``)
  and their Python imports are resolved against the candidate.

The per-gate *entry points* are the only hand-written input
(:data:`GATE_DEFINITIONS`); everything else is derived.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import posixpath
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROTOCOL = "mycelium.gate_source_set.v1"
DEFINITIONS_VERSION = "2026-10-13.1"
DEFAULT_RUNDAY_ROOT = os.path.expanduser("~/Documents/mycelium-core-program/runs/v2")
RUNDAY_ROOT_ENV = "MYCELIUM_RUNDAY_ROOT"
IGNORED_PARTS = frozenset(
    {"__pycache__", "node_modules", "target", "dist", ".build-native", ".git", ".pytest_cache"}
)
CODE_SUFFIXES = frozenset(
    {".py", ".pyi", ".ts", ".tsx", ".mts", ".js", ".mjs", ".cjs", ".jsx", ".rs", ".c", ".h", ".kt", ".java", ".swift"}
)
JS_SUFFIXES = (".mjs", ".js", ".cjs", ".ts", ".tsx", ".mts", ".jsx")
JS_RESOLVE_SUFFIXES = (".ts", ".tsx", ".mts", ".js", ".mjs", ".cjs", ".jsx", ".json", ".css")
# Executable text scanned for native unit names (data files such as path
# manifests are pinned but would name every unit they list).
TEXT_SCAN_SUFFIXES = frozenset(
    {".py", ".mjs", ".js", ".cjs", ".ts", ".tsx", ".mts", ".jsx", ".sh", ".ps1", ".cmd", ".spec"}
)
# Generated pin files: they change whenever any file they pin changes.  They
# stay pinned (a gate reading them may verify them) but are labelled so the
# owner can see when a RERUN is driven only by them.
GENERATED_PIN_FILES = frozenset(
    {"artifacts/a6/source-manifest-v1.json", "contracts/contract-manifest.v1.json"}
)
DIRECTORY_EXPANSION_LIMIT = 400
_MAX_LITERAL = 260
_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH_TOKEN = re.compile(
    r"(?:[A-Za-z0-9_.@+-]+/)*[A-Za-z0-9_.@+-]+\.(?:py|mjs|cjs|js|ts|tsx|json|toml|sh|ps1|cmd|rs|c|h|txt|yaml|yml|html|css|spec|kts|xml|lock)(?![A-Za-z0-9_])"
)
_DASH_M = re.compile(r"(?<![\w-])-m\s+([A-Za-z_][A-Za-z0-9_.]*)")
_NPM = re.compile(r"\bnpm\s+(?:run\s+)?([A-Za-z0-9:._-]+)")
_JS_IMPORT = re.compile(
    r"""(?:\bimport\s*(?:[\w*{}\s,$]+\s*from\s*)?|\bexport\s+[\w*{}\s,$]+\s*from\s*|\bimport\s*\(\s*|\brequire\s*\(\s*)(['"])([^'"\n]+)\1"""
)
_JS_URL = re.compile(r"""new\s+URL\(\s*(['"])([^'"\n]+)\1\s*,\s*import\.meta\.url""")
_JS_STRING = re.compile(r"""'([^'\\\n]{1,260})'|"([^"\\\n]{1,260})"|`([^`$\\\n]{1,260})`""")
_CSS_REF = re.compile(r"""@import\s+(?:url\()?['"]?([^'")\s;]+)|url\(\s*['"]?([^'")\s]+)""")
_HTML_REF = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']""")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_bytes(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _git_env() -> dict[str, str]:
    drop = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
    return {key: value for key, value in os.environ.items() if key not in drop}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    ).stdout


# --------------------------------------------------------------------------
# Commit-bound file tree
# --------------------------------------------------------------------------


class GitTree:
    """Read-only view of one commit's files via the Git object store."""

    def __init__(self, repo: Path | str, commit: str) -> None:
        self.repo = Path(repo)
        self.commit = _git(self.repo, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()
        self.tree_oid = _git(self.repo, "rev-parse", f"{self.commit}^{{tree}}").strip()
        listing = _git(self.repo, "ls-tree", "-r", "-z", "--full-tree", self.commit)
        self._blobs: dict[str, str] = {}
        for record in listing.split("\0"):
            if not record:
                continue
            meta, path = record.split("\t", 1)
            _mode, kind, oid = meta.split()
            if kind == "blob":
                self._blobs[path] = oid
        self._dirs: set[str] = {""}
        for path in self._blobs:
            parent = posixpath.dirname(path)
            while parent and parent not in self._dirs:
                self._dirs.add(parent)
                parent = posixpath.dirname(parent)
        self._data: dict[str, bytes] = {}
        self._sorted = sorted(self._blobs)

    # -- queries ---------------------------------------------------------
    def is_file(self, path: str) -> bool:
        return path in self._blobs

    def is_dir(self, path: str) -> bool:
        return path in self._dirs

    def files_under(self, directory: str) -> list[str]:
        prefix = directory.rstrip("/") + "/" if directory else ""
        return [path for path in self._sorted if path.startswith(prefix)]

    def all_files(self) -> list[str]:
        return list(self._sorted)

    def oid(self, path: str) -> str | None:
        return self._blobs.get(path)

    def read(self, path: str) -> bytes:
        if path not in self._data:
            self._prefetch([path])
        return self._data[path]

    def text(self, path: str) -> str:
        return self.read(path).decode("utf-8", errors="replace")

    def sha256(self, path: str) -> str:
        return _sha256(self.read(path))

    def prefetch(self, paths: Iterable[str]) -> None:
        self._prefetch([path for path in paths if path in self._blobs and path not in self._data])

    def _prefetch(self, paths: Sequence[str]) -> None:
        if not paths:
            return
        request = "".join(self._blobs[path] + "\n" for path in paths).encode()
        out = subprocess.run(
            ["git", "-C", str(self.repo), "cat-file", "--batch"],
            input=request,
            capture_output=True,
            check=True,
            env=_git_env(),
        ).stdout
        stream = io.BytesIO(out)
        for path in paths:
            header = stream.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise ValueError(f"gate_source_set_blob_unreadable:{path}")
            size = int(header[2])
            self._data[path] = stream.read(size)
            stream.read(1)


# --------------------------------------------------------------------------
# Gate definitions (the only hand-written input: entry points per gate)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDefinition:
    gate: str
    python_entries: tuple[str, ...] = ()  # repo files executed as scripts
    python_modules: tuple[str, ...] = ()  # run with ``python -m``
    js_entries: tuple[str, ...] = ()  # node entry scripts
    web_builds: tuple[str, ...] = ()  # npm package dirs whose bundle the gate serves
    trees: tuple[str, ...] = ()  # whole directories (e.g. an APK build input)
    paths: tuple[str, ...] = ()  # extra repo files (data, tests run on the day)
    native_units: tuple[str, ...] = ()  # native build units used by the gate
    external: tuple[str, ...] = ()  # out-of-repo run-day files (relative to the runday root)
    rationale: str = ""

    def normalized(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "python_entries": sorted(set(self.python_entries)),
            "python_modules": sorted(set(self.python_modules)),
            "js_entries": sorted(set(self.js_entries)),
            "web_builds": sorted(set(self.web_builds)),
            "trees": sorted(set(self.trees)),
            "paths": sorted(set(self.paths)),
            "native_units": sorted(set(self.native_units)),
            "external": sorted(set(self.external)),
            "rationale": self.rationale,
        }


# Shared by every gate that drives the live product serve: the Mac-side
# ``python -m mycelium_demo serve --mode live``, the membership/control node
# ``python -m mycelium_node``, the staged worker ``physical_inference_node.py``
# on every node, the route staging/bind tools, the served web bundle and the
# native iroh sidecar that carries the route.
_LIVE_PY = (
    "physical_inference_node.py",
    "scripts/stage_live_route.py",
    "scripts/build_qwen_live_route.py",
    "scripts/bind_operator_plan_to_seed.py",
)
_LIVE_MODULES = ("mycelium_demo", "mycelium_node", "mycelium_seed.plan_binding")
_ROUTE_BRINGUP = "m1-real-origin/route-bringup"
_PREP_EXTERNAL = (
    "pm-serve/prep.sh",
    f"{_ROUTE_BRINGUP}/40_bind_fresh_plan_r10.py",
    f"{_ROUTE_BRINGUP}/43_refresh_both_r10.py",
    f"{_ROUTE_BRINGUP}/43_refresh_staged_node_bytes.py",
    f"{_ROUTE_BRINGUP}/63_atomic_demo_restore.sh",
    "runday/a7/a7_preposition.py",
    "runday/preflight.sh",
    "runday/restage_remote.sh",
)

GATE_DEFINITIONS: tuple[GateDefinition, ...] = (
    GateDefinition(
        gate="A0",
        python_entries=(
            "scripts/governance_audit.py",
            "scripts/governance_gate.py",
            "scripts/claim_boundary_audit.py",
            "scripts/contract_audit.py",
            "scripts/build_a6_source_manifest.py",
            "scripts/fill_a0_literal_record.py",
            "scripts/mycelium_completion_audit.py",
        ),
        js_entries=("ui/web/scripts/a0-governance-browser-closeout.mjs",),
        web_builds=("ui/web",),
        paths=(
            "tests/governance/test_a0_literal_verification_record.py",
            "tests/governance/test_governance_audit.py",
            "tests/governance/test_governance_gate_negative_copies.py",
        ),
        trees=("tests/live",),
        external=("RUNDAY-A0.md",),
        rationale="A0 literal governance checks (RUNDAY-A0.md); by procedure A0 runs on the final candidate.",
    ),
    GateDefinition(
        gate="A1",
        python_entries=(*_LIVE_PY, "scripts/run_m23_kv_gate.py"),
        python_modules=_LIVE_MODULES,
        js_entries=("ui/web/scripts/a1-evidence-browser-closeout.mjs",),
        web_builds=("ui/web",),
        native_units=("native/iroh_transport",),
        external=(
            "pm-serve/a1_window.sh",
            "pm-serve/agate_serve.sh",
            f"{_ROUTE_BRINGUP}/47_preflight_receipt_plumbing.py",
            *_PREP_EXTERNAL,
        ),
        rationale="pm-serve/a1_window.sh: two M23 capture serves, seal, positive + negative browser closeout.",
    ),
    GateDefinition(
        gate="A5",
        python_entries=(
            *_LIVE_PY,
            "scripts/run_a5_benchmark_gate.py",
            "scripts/run_a5_benchmark_supervisor.py",
            "scripts/run_a5_negative_illegal_gate.py",
            "scripts/run_a5_product_gate.py",
            "scripts/run_a5_replica_loss_gate.py",
            "scripts/qualify_a5_replica_track.py",
            "scripts/qualify_m18_replica_route.py",
            "scripts/build_m18_physical_control_plane.py",
            "scripts/build_a5_replica_pair_graph.py",
            "scripts/reconcile_a5_interrupted_attempt.py",
        ),
        python_modules=_LIVE_MODULES,
        js_entries=("ui/web/scripts/a5-product-browser-e2e.mjs",),
        web_builds=("ui/web",),
        native_units=("native/iroh_transport",),
        external=(
            "runday/a5/a5_runday.sh",
            "runday/a5/sb2_ha_pause.sh",
            "runday/a5-topology.3host.json",
            "runday/m13-measure/produce_measurements.py",
            "runday/RUNDAY-A5.md",
            *_PREP_EXTERNAL,
        ),
        rationale="runday/a5/a5_runday.sh + RUNDAY-A5.md sections 5-6 (replicated stage, benchmark v2).",
    ),
    GateDefinition(
        gate="A6",
        python_entries=(
            *_LIVE_PY,
            "scripts/run_a6_recovery_gate.py",
            "scripts/build_a6_execution_plan.py",
            "scripts/derive_a6_replica_plan.py",
            "scripts/run_a5_product_gate.py",
        ),
        python_modules=_LIVE_MODULES,
        js_entries=(
            "ui/web/scripts/a6-serve-receiver-factory.mjs",
            "ui/web/scripts/a6-product-browser-e2e.mjs",
        ),
        web_builds=("ui/web",),
        native_units=("native/iroh_transport",),
        external=(
            "RUNDAY-A6/a6_window_v2.sh",
            "RUNDAY-A6/drive_a6_cases_v2.py",
            "RUNDAY-A6/drive_a6_cases.py",
            "RUNDAY-A6.md",
            "runday/sb2_ha_pause.sh",
            f"{_ROUTE_BRINGUP}/40c_bind_fresh_plan_r10_3host.py",
            f"{_ROUTE_BRINGUP}/43c_refresh_three_r10.py",
            *_PREP_EXTERNAL,
        ),
        rationale="RUNDAY-A6/a6_window_v2.sh: 16 recovery cases + cleanup + browser matrix.",
    ),
    GateDefinition(
        gate="A7",
        python_entries=(
            *_LIVE_PY,
            "scripts/run_a7_kv_successor_gate.py",
            "scripts/run_a7_kv_successor_case_runner.py",
            "scripts/a7_physical_parity.py",
            "scripts/build_a7_operator_plan.py",
            "mycelium_a7_physical_measurement.py",
            "mycelium_a7_evidence_control.py",
            "mycelium_a7_parity_worker.py",
        ),
        python_modules=_LIVE_MODULES,
        web_builds=("ui/web",),
        native_units=("native/iroh_transport",),
        external=(
            "runday/a7/a7_window.sh",
            "runday/a7/a7_bind_case_plan.sh",
            "runday/a7/a7_case_serve.sh",
            "runday/disk_retention.sh",
            *_PREP_EXTERNAL,
        ),
        rationale="runday/a7/a7_window.sh: per-case fenced KV successor serves + parity.",
    ),
    GateDefinition(
        gate="A9",
        python_entries=("scripts/run_a9_gate.py", "scripts/run_a9_windows_route.py"),
        python_modules=(
            "mycelium_a13_windows.import_model",
            "mycelium_a13_windows.managed_host",
            "mycelium_a13_windows.service_install",
            "mycelium_a13_macos",
            "mycelium_node",
        ),
        trees=("tests/a9_acceptance", "tests/a9_migration_scaffold"),
        paths=("tests/node_agent/test_physical_service_win32.py",),
        native_units=("native/a9_int8_kernel", "native/iroh_transport"),
        external=(
            "runday/a9/a9_runday.sh",
            "runday/a9/a9_windows.ps1",
            "runday/a9/RUNDAY-A9-ROUTE.md",
            "runday/RUNDAY-A9.md",
            "runday/RUNDAY-A9-WINDOWS.md",
        ),
        rationale="RUNDAY-A9-WINDOWS: on-device tests, int8 kernel benchmark, migration, two-host route.",
    ),
    GateDefinition(
        gate="A10",
        python_entries=(
            *_LIVE_PY,
            "scripts/run_a10_physical_gate.py",
            "scripts/a10_batch_policy.py",
            "scripts/build_m18_physical_control_plane.py",
            "scripts/build_a5_replica_pair_graph.py",
            "scripts/build_a3_capacity_evidence.py",
            "physical_inference_qualification.py",
            "mycelium_a10_verify_control.py",
            "mycelium_a10_batch_policy_control.py",
        ),
        python_modules=_LIVE_MODULES,
        web_builds=("ui/web",),
        native_units=("native/iroh_transport",),
        external=(
            "runday/a10/a10_serve.sh",
            "runday/a10/a10_authorization.py",
            "runday/a10/a10_window_calibration.py",
            "runday/a10/a10-topology.2host.json",
            "runday/m13-measure/produce_measurements.py",
            "runday/RUNDAY-A10.md",
            *_PREP_EXTERNAL,
        ),
        rationale="runday/RUNDAY-A10.md: 7B route A/B, window calibration, gates 1-4.",
    ),
    GateDefinition(
        gate="A11",
        python_entries=(
            *_LIVE_PY,
            "scripts/run_a11_route_benchmark.py",
            "scripts/build_a11_operator_plan.py",
        ),
        python_modules=(*_LIVE_MODULES, "mycelium_a11_node_evidence"),
        web_builds=("ui/web",),
        native_units=("native/iroh_transport",),
        external=(
            "runday/a11/a11_window.sh",
            "runday/a11/RUNDAY-A11.md",
            "runday/a11/PREREG-R10.sha256",
            *_PREP_EXTERNAL,
        ),
        rationale="runday/a11/a11_window.sh: target-authoritative speculative route benchmark.",
    ),
    GateDefinition(
        gate="A12",
        python_entries=("scripts/run_a12_mobile_physical_gate.py",),
        python_modules=("mycelium_seed", "mycelium_seed.route_assignment"),
        trees=("apps/node-app",),
        native_units=("native/mobile_core",),
        external=(
            "runday/a12/make_gate_dir.sh",
            "runday/RUNDAY-A12.md",
            "pm-a12-apk/build_apk.sh",
            "pm-a12-apk/build_route_peer.sh",
        ),
        rationale="RUNDAY-A12: signed APK built from apps/node-app + native/mobile_core, seed route assignment.",
    ),
    GateDefinition(
        gate="A13",
        python_entries=(
            "scripts/build_a13_macos.py",
            "scripts/build_a13_linux.py",
            "scripts/build_a13_windows.py",
            "scripts/finalize_a13_macos.py",
        ),
        python_modules=(
            "mycelium_a13_macos",
            "mycelium_a13_linux",
            "mycelium_a13_windows",
            "mycelium_a13_packaging",
            "mycelium_node",
        ),
        trees=("scripts/a13_operator",),
        # The committed PyInstaller spec is inert: build_a13_macos.py writes its own
        # spec into the work dir, and tests/a13_macos/test_build_source_truth.py
        # fails if any script names the committed one. It is not a gate input.
        paths=("requirements-a13-macos-runtime.txt",),
        native_units=("native/iroh_transport",),
        external=(
            "runday/a13/A13-LINUX-PACKAGING.md",
            "runday/a13/A13-MACOS-PACKAGING-ADDENDUM.md",
            "runday/a13/A13-WINDOWS-PACKAGING.md",
            "runday/a13/fetch_linux_packaging_deps.sh",
            "runday/a13/fetch_windows_packaging_deps.sh",
        ),
        rationale="A13 onboarding apps (no run-day window script exists; packaging + operator scripts).",
    ),
    GateDefinition(
        gate="A14",
        python_entries=(*_LIVE_PY, "scripts/run_a5_product_gate.py", "scripts/run_m23_kv_gate.py"),
        python_modules=_LIVE_MODULES,
        js_entries=("ui/web/scripts/a14-live-browser.mjs",),
        web_builds=("ui/web",),
        native_units=("native/iroh_transport",),
        external=(
            "runday/a14/a14_window.sh",
            "runday/a14/drive_a14.py",
            "runday/a14-region-declaration.json",
            "runday/post_restore_inference_check.py",
            "runday/RUNDAY-A14.md",
            *_PREP_EXTERNAL,
        ),
        rationale="runday/a14/a14_window.sh: direct serve, rebind, forced-relay serve, browser engines.",
    ),
    GateDefinition(
        gate="A15",
        python_entries=(
            "mycelium_a15_release_decision.py",
            "mycelium_a15_run_intake.py",
            "scripts/a15_release_decision.py",
            "scripts/a15_run_intake.py",
        ),
        rationale="A15 executed release closure (record intake + decision); no run-day driver.",
    ),
)


def definitions_digest(definitions: Iterable[GateDefinition]) -> str:
    body = sorted((d.normalized() for d in definitions), key=lambda item: item["gate"])
    return _sha256(canonical_bytes({"version": DEFINITIONS_VERSION, "gates": body}))


# --------------------------------------------------------------------------
# Closure engine
# --------------------------------------------------------------------------


def _ignored(path: str) -> bool:
    return any(part in IGNORED_PARTS for part in path.split("/"))


def _norm(path: str) -> str | None:
    if not path:
        return None
    normalized = posixpath.normpath(path)
    if normalized in (".", "") or normalized.startswith("../") or normalized == "..":
        return None
    return normalized


@dataclass
class GateSourceSet:
    gate: str
    files: dict[str, str] = field(default_factory=dict)  # path -> first discovering edge
    missing_entries: list[str] = field(default_factory=list)
    unresolved_python: set[str] = field(default_factory=set)
    unresolved_js: set[str] = field(default_factory=set)
    unexpanded_directories: set[str] = field(default_factory=set)
    externals: list[dict[str, Any]] = field(default_factory=list)
    externals_scanned: bool = False


class _NativeUnit:
    def __init__(self, root: str, keys: set[str], path_deps: set[str]) -> None:
        self.root = root
        self.keys = keys
        self.path_deps = path_deps
        self.pattern = re.compile(
            "|".join(r"(?<![\w-])" + re.escape(key) + r"(?![\w-])" for key in sorted(keys))
        )

    def named_in(self, text: str) -> bool:
        # Cheap substring prefilter before the token-boundary regex.
        return any(key in text for key in self.keys) and bool(self.pattern.search(text))


class _Closure:
    def __init__(self, tree: GitTree, gate: str) -> None:
        self.tree = tree
        self.result = GateSourceSet(gate=gate)
        self.queue: list[tuple[str, str]] = []
        self.native_units = self._discover_native_units()
        self.packages = sorted(
            posixpath.dirname(path)
            for path in tree.all_files()
            if posixpath.basename(path) == "package.json" and not _ignored(path)
        )
        self._web_done: set[str] = set()
        self._native_done: set[str] = set()
        self._npm_depth = 0

    # -- bookkeeping -----------------------------------------------------
    def add(self, path: str, via: str, *, analyze: bool = True) -> bool:
        """Pin ``path``; ``analyze`` follows its own references (executed code)."""
        if _ignored(path) or not self.tree.is_file(path):
            return False
        if path not in self.result.files:
            self.result.files[path] = via
            if analyze:
                self.queue.append((path, via))
        return True

    def run(self) -> None:
        while self.queue:
            batch = sorted(self.queue)
            self.queue = []
            self.tree.prefetch(path for path, _ in batch)
            for path, _via in batch:
                self._analyze(path)

    def _analyze(self, path: str) -> None:
        suffix = posixpath.splitext(path)[1]
        if suffix == ".py":
            self._python(path, self.tree.text(path), external=False)
        elif suffix in JS_SUFFIXES:
            self._javascript(path, self.tree.text(path))
        elif suffix == ".css":
            self._css(path, self.tree.text(path))
        elif suffix in (".sh", ".ps1", ".cmd"):
            self.scan_text(self.tree.text(path), via=f"exec:{path}", base_dirs=(posixpath.dirname(path),))
        if suffix in TEXT_SCAN_SUFFIXES:
            self._native_triggers(path, self.tree.text(path))

    # -- python ----------------------------------------------------------
    def resolve_module(self, name: str, roots: Sequence[str], via: str, *, main: bool = False) -> bool:
        parts = name.split(".")
        found_any = False
        for root in roots:
            hits: list[str] = []
            for depth in range(1, len(parts) + 1):
                base = posixpath.join(root, *parts[:depth]) if root else posixpath.join(*parts[:depth])
                init = base + "/__init__.py"
                module = base + ".py"
                if self.tree.is_file(init):
                    hits.append(init)
                    if depth == len(parts) and main and self.tree.is_file(base + "/__main__.py"):
                        hits.append(base + "/__main__.py")
                elif self.tree.is_file(module):
                    hits.append(module)
                    break
                elif self.tree.is_dir(base):
                    # Namespace package (no __init__.py): resolved, nothing to pin.
                    if depth == len(parts):
                        found_any = True
                        if main and self.tree.is_file(base + "/__main__.py"):
                            hits.append(base + "/__main__.py")
                    continue
                else:
                    break
            if hits:
                found_any = True
                for hit in hits:
                    self.add(hit, via)
        return found_any

    def _python(self, path: str, text: str, *, external: bool) -> None:
        via = f"import:{path}"
        directory = "" if external else posixpath.dirname(path)
        refs = _python_refs(text)
        if refs is None:
            # Unparseable source stays pinned; fall back to a text scan.
            self.scan_text(text, via=f"text:{path}", base_dirs=(directory,))
            return
        roots: list[str] = [""]
        if directory:
            roots.append(directory)
        for extra_parts in refs.sys_path:
            extra = _norm("/".join(extra_parts))
            if extra and self.tree.is_dir(extra) and extra not in roots:
                roots.append(extra)
        package = directory.split("/") if directory else []
        for level, module, names in refs.imports:
            if level:
                if external:
                    continue
                pkg = package[: len(package) - (level - 1)] if level > 1 else package
                base = ".".join([*pkg, module] if module else pkg)
                candidates = [base] if base else []
                candidates += [f"{base}.{name}" if base else name for name in names]
                for candidate in candidates:
                    self.resolve_module(candidate, [""], via)
            elif names is None:  # plain ``import a.b``
                if not self.resolve_module(module, roots, via):
                    self.result.unresolved_python.add(module.split(".")[0])
            else:
                if not self.resolve_module(module, roots, via):
                    self.result.unresolved_python.add(module.split(".")[0])
                for name in names:
                    self.resolve_module(f"{module}.{name}", roots, via)
        for name in refs.modules:
            self.resolve_module(name, roots, f"dynamic:{path}", main=True)
        base_dirs = ("",) if external else (directory,)
        for literal, path_context in refs.literals:
            self.literal(
                literal,
                via=f"literal:{path}",
                base_dirs=base_dirs,
                dynamic=refs.dynamic,
                roots=roots,
                path_context=path_context,
            )

    # -- literals and free text --------------------------------------------
    def literal(
        self,
        value: str,
        *,
        via: str,
        base_dirs: Sequence[str],
        dynamic: bool = False,
        roots: Sequence[str] = ("",),
        path_context: int = 0,
    ) -> None:
        if not value or len(value) > _MAX_LITERAL or "\n" in value or "://" in value:
            return
        stripped = value.strip()
        if any(ch.isspace() for ch in stripped):
            self.scan_text(stripped, via=via, base_dirs=base_dirs)
            return
        if _DOTTED.match(stripped):
            self.resolve_module(stripped, roots, via, main=True)
        elif dynamic and _IDENT.match(stripped):
            self.resolve_module(stripped, roots, via, main=True)
        if stripped.startswith(("./", "../")):
            # Explicitly relative: only the referring file's directory applies.
            candidate, bases = stripped, tuple(base_dirs[:1])
        else:
            candidate, bases = stripped.lstrip("/"), ("", *base_dirs)
        for base in bases:
            resolved = _norm(posixpath.join(base, candidate) if base else candidate)
            if resolved is None:
                continue
            if self.tree.is_file(resolved):
                self.add(resolved, via)
            elif resolved and self.tree.is_dir(resolved) and (
                path_context >= PATH_ANCHORED
                or (path_context >= PATH_LIKE and len([part for part in candidate.split("/") if part not in ("", ".")]) >= 2)
            ):
                # A directory listing read: anchored on the module location,
                # or a multi-component path; a lone "data"/"data/" may be a
                # runtime-relative name.
                self._expand_directory(resolved, via)

    def _expand_directory(self, directory: str, via: str) -> None:
        if _ignored(directory):
            return
        files = [
            path
            for path in self.tree.files_under(directory)
            if not _ignored(path) and posixpath.splitext(path)[1] not in CODE_SUFFIXES
        ]
        if len(files) > DIRECTORY_EXPANSION_LIMIT:
            self.result.unexpanded_directories.add(directory)
            return
        for path in files:
            self.add(path, f"directory:{directory}<-{via}", analyze=False)

    def resolve_token(self, token: str, via: str, base_dirs: Sequence[str]) -> bool:
        parts = token.split("/")
        for start in range(len(parts)):
            candidate = "/".join(parts[start:])
            for base in (*base_dirs, ""):
                resolved = _norm(posixpath.join(base, candidate) if base else candidate)
                if resolved and self.tree.is_file(resolved):
                    self.add(resolved, via)
                    return True
        if "/" in token:
            suffix = "/" + token.lstrip("./")
            matches = [path for path in self.tree.all_files() if path.endswith(suffix) and not _ignored(path)]
            for match in matches:
                self.add(match, via)
            return bool(matches)
        return False

    def scan_text(self, text: str, *, via: str, base_dirs: Sequence[str] = ("",)) -> None:
        for match in _PATH_TOKEN.finditer(text):
            self.resolve_token(match.group(0), via, base_dirs)
        for match in _DASH_M.finditer(text):
            self.resolve_module(match.group(1), [""], via, main=True)
        lines = text.splitlines()
        for index, line in enumerate(lines):
            for match in _NPM.finditer(line):
                # The package is the one named on this line or the three before
                # it (``cd ui/web && npm run build`` / a preceding ``cd``).
                context = "\n".join(lines[max(0, index - 3) : index + 1])
                self._npm(match.group(1), context, via)

    def _npm(self, script: str, line: str, via: str) -> None:
        if self._npm_depth > 4:
            return
        selected = [pkg for pkg in self.packages if pkg and pkg in line]
        candidates = selected or self.packages
        for pkg in candidates:
            manifest_path = posixpath.join(pkg, "package.json") if pkg else "package.json"
            try:
                manifest = json.loads(self.tree.text(manifest_path))
            except (ValueError, KeyError):
                continue
            command = (manifest.get("scripts") or {}).get(script)
            if script in {"ci", "install"}:
                self.add(manifest_path, via)
                continue
            if not isinstance(command, str):
                continue
            self.add(manifest_path, via)
            if re.search(r"\bvite\s+build\b|\btsc\b", command):
                self.web_build(pkg, f"npm:{script}<-{via}")
            self._npm_depth += 1
            try:
                self.scan_text(command.replace("npm run ", f"npm run {pkg} "), via=f"npm:{pkg}:{script}", base_dirs=(pkg,))
            finally:
                self._npm_depth -= 1

    # -- javascript --------------------------------------------------------
    def _package_root(self, path: str) -> str:
        directory = posixpath.dirname(path)
        while True:
            if self.tree.is_file(posixpath.join(directory, "package.json") if directory else "package.json"):
                return directory
            if not directory:
                return ""
            directory = posixpath.dirname(directory)

    def _resolve_js(self, specifier: str, path: str, via: str) -> None:
        spec = specifier.split("?", 1)[0].split("#", 1)[0]
        if not spec:
            return
        if spec.startswith("./") or spec.startswith("../"):
            base = _norm(posixpath.join(posixpath.dirname(path), spec))
        elif spec.startswith("/"):
            base = _norm(posixpath.join(self._package_root(path), spec.lstrip("/")))
        else:
            if not spec.startswith("node:"):
                self.result.unresolved_js.add(spec.split("/")[0] if not spec.startswith("@") else "/".join(spec.split("/")[:2]))
            return
        if base is None:
            return
        candidates = [base] + [base + ext for ext in JS_RESOLVE_SUFFIXES]
        stem, ext = posixpath.splitext(base)
        if ext in (".js", ".mjs", ".jsx"):
            candidates += [stem + alt for alt in (".ts", ".tsx", ".mts")]
        candidates += [posixpath.join(base, "index" + alt) for alt in JS_RESOLVE_SUFFIXES]
        for candidate in candidates:
            if self.add(candidate, via):
                return

    def _javascript(self, path: str, text: str) -> None:
        via = f"import:{path}"
        for match in _JS_IMPORT.finditer(text):
            self._resolve_js(match.group(2), path, via)
        for match in _JS_URL.finditer(text):
            self._resolve_js(match.group(2), path, via)
        bases = (posixpath.dirname(path), self._package_root(path))
        for match in _JS_STRING.finditer(text):
            value = next(group for group in match.groups() if group is not None)
            self.literal(value, via=f"literal:{path}", base_dirs=bases, path_context=PATH_LIKE if "/" in value else PATH_PLAIN)
        for match in re.finditer(r"""(['"])-m\1\s*,\s*(['"])([A-Za-z_][\w.]*)\2""", text):
            self.resolve_module(match.group(3), [""], f"dynamic:{path}", main=True)

    def _css(self, path: str, text: str) -> None:
        for match in _CSS_REF.finditer(text):
            specifier = match.group(1) or match.group(2)
            if specifier and not specifier.startswith(("data:", "http")):
                spec = specifier if specifier.startswith((".", "/")) else "./" + specifier
                self._resolve_js(spec, path, f"import:{path}")

    def web_build(self, package: str, via: str) -> None:
        if package in self._web_done:
            return
        self._web_done.add(package)
        prefix = package + "/" if package else ""
        for path in self.tree.files_under(package):
            rel = path[len(prefix):]
            name = posixpath.basename(rel)
            if _ignored(path):
                continue
            if "/" not in rel and (
                name in {"package.json", "package-lock.json", "index.html", ".npmrc"}
                or name.startswith(("vite.config", "tsconfig", ".env"))
            ):
                self.add(path, f"web_build:{package}")
            elif rel.startswith("public/") or (rel.startswith("src/") and rel.endswith(".d.ts")):
                self.add(path, f"web_build:{package}")
        index = prefix + "index.html"
        if self.tree.is_file(index):
            for match in _HTML_REF.finditer(self.tree.text(index)):
                ref = match.group(1)
                if "://" in ref:
                    continue
                spec = ref if ref.startswith((".", "/")) else "./" + ref
                self._resolve_js(spec, index, f"web_build:{package}")

    # -- native ------------------------------------------------------------
    def _discover_native_units(self) -> list[_NativeUnit]:
        units: dict[str, _NativeUnit] = {}
        for path in self.tree.all_files():
            if _ignored(path):
                continue
            if posixpath.basename(path) == "Cargo.toml":
                root = posixpath.dirname(path)
                keys = {root} if root else set()
                deps: set[str] = set()
                try:
                    data = tomllib.loads(self.tree.text(path))
                except tomllib.TOMLDecodeError:
                    data = {}
                package = data.get("package", {}).get("name")
                if isinstance(package, str):
                    keys.add(package)
                    keys.add(package.replace("-", "_"))
                lib = data.get("lib", {}).get("name")
                if isinstance(lib, str):
                    keys.add(lib)
                for section in ("bin",):
                    for item in data.get(section, []) or []:
                        if isinstance(item, dict) and isinstance(item.get("name"), str):
                            keys.add(item["name"])
                for bin_path in self.tree.files_under(posixpath.join(root, "src/bin") if root else "src/bin"):
                    keys.add(posixpath.splitext(posixpath.basename(bin_path))[0])
                base = posixpath.basename(root)
                if root.startswith("native/") or "_" in base or "-" in base:
                    keys.add(base)
                for dep_path in _cargo_path_deps(data):
                    resolved = _norm(posixpath.join(root, dep_path))
                    if resolved:
                        deps.add(resolved)
                units[root] = _NativeUnit(root, {key for key in keys if key}, deps)
        for path in self.tree.all_files():
            parts = path.split("/")
            if len(parts) >= 3 and parts[0] == "native":
                root = "/".join(parts[:2])
                if root in units:
                    continue
                keys = {root, parts[1]}
                for member in self.tree.files_under(root):
                    stem, ext = posixpath.splitext(posixpath.basename(member))
                    if ext == ".c" and ("_" in stem or "-" in stem):
                        keys.add(stem)
                units[root] = _NativeUnit(root, keys, set())
        return [units[key] for key in sorted(units)]

    def native(self, root: str, via: str) -> None:
        if root in self._native_done:
            return
        unit = next((unit for unit in self.native_units if unit.root == root), None)
        if unit is None:
            self.result.missing_entries.append(f"native:{root}")
            return
        self._native_done.add(root)
        for path in self.tree.files_under(root):
            # Build inputs: pinned in full, not followed as Python/JS code.
            self.add(path, f"native:{root}<-{via}", analyze=False)
        for dep in sorted(unit.path_deps):
            self.native(dep, f"cargo_path_dep:{root}")

    def _native_triggers(self, path: str, text: str) -> None:
        for unit in self.native_units:
            if unit.root in self._native_done or path.startswith(unit.root + "/"):
                continue
            if unit.named_in(text):
                self.native(unit.root, f"named_in:{path}")


@dataclass(frozen=True)
class _PythonRefs:
    imports: tuple[tuple[int, str, tuple[str, ...] | None], ...]
    modules: tuple[str, ...]
    literals: tuple[tuple[str, int], ...]  # (value, path strength)
    sys_path: tuple[tuple[str, ...], ...]
    dynamic: bool


_REFS_CACHE: dict[str, _PythonRefs | None] = {}


def _python_refs(text: str) -> _PythonRefs | None:
    """Extract every reference a Python source makes (cached by content digest)."""

    key = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    if key in _REFS_CACHE:
        return _REFS_CACHE[key]
    try:
        module_ast = ast.parse(text)
    except (SyntaxError, ValueError):
        _REFS_CACHE[key] = None
        return None
    imports: list[tuple[int, str, tuple[str, ...] | None]] = []
    modules: list[str] = []
    literals: dict[str, int] = {}
    sys_path: list[tuple[str, ...]] = []
    dynamic = False
    inner_joins: set[int] = set()
    join_parts: set[int] = set()
    anchors = _file_anchors(module_ast)

    def literal(value: str, strength: int) -> None:
        literals[value] = max(literals.get(value, PATH_PLAIN), strength)

    for node in ast.walk(module_ast):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((0, alias.name, None))
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.level, node.module or "", tuple(alias.name for alias in node.names)))
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in {"insert", "append"} and _is_sys_path(node.func):
                sys_path.append(tuple(_constant_strings(node.args)))
            if name in {"import_module", "__import__", "find_spec", "run_module"}:
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    modules.append(first.value)
                else:
                    dynamic = True
            if name in {"join", "joinpath", "Path", "PurePath", "PurePosixPath", "PosixPath"}:
                join_parts.update(id(arg) for arg in node.args)
                base: list[ast.AST] = [node.func.value] if isinstance(node.func, ast.Attribute) else []
                strength = PATH_ANCHORED if _anchored([*base, *node.args[:1]], anchors) else PATH_LIKE
                for run in _constant_runs(node.args, minimum=1):
                    literal("/".join(run), strength)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            # Only the maximal literal run of a whole path join is a path; the
            # inner sub-joins ("docs", "docs/handover") are not (ast.walk is
            # breadth-first, so the outermost join is seen first).
            if id(node) in inner_joins:
                continue
            for child in ast.walk(node):
                if child is not node and isinstance(child, ast.BinOp) and isinstance(child.op, ast.Div):
                    inner_joins.add(id(child))
            operands = _flatten_div(node)
            join_parts.update(id(operand) for operand in operands)
            # Anchored on the module's own location (ROOT = Path(__file__)...):
            # a real repository path, so a directory there is a listing read.
            strength = PATH_ANCHORED if _anchored(operands[:1], anchors) else PATH_LIKE
            for run in _constant_runs(operands, minimum=1):
                literal("/".join(run), strength)
        elif isinstance(node, (ast.List, ast.Tuple)):
            elements = node.elts
            for index, element in enumerate(elements[:-1]):
                nxt = elements[index + 1]
                if _is_constant(element, "-m") and isinstance(nxt, ast.Constant) and isinstance(nxt.value, str):
                    modules.append(nxt.value)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A path-join part ("x.json" in ROOT / "sub" / "x.json") is only
            # meaningful as part of its join, handled above.
            if id(node) not in join_parts:
                literal(node.value, PATH_LIKE if "/" in node.value else PATH_PLAIN)
    refs = _PythonRefs(
        imports=tuple(imports),
        modules=tuple(modules),
        literals=tuple(sorted(literals.items())),
        sys_path=tuple(sys_path),
        dynamic=dynamic,
    )
    _REFS_CACHE[key] = refs
    return refs


PATH_PLAIN, PATH_LIKE, PATH_ANCHORED = 0, 1, 2


def _file_anchors(module_ast: ast.AST) -> set[str]:
    """Names bound (directly or transitively) from ``__file__``."""

    anchors: set[str] = set()
    assigns = [node for node in ast.walk(module_ast) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    for _ in range(3):
        for node in assigns:
            value = node.value
            if value is None:
                continue
            names = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
            if "__file__" in names or names & anchors:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for leaf in ast.walk(target):
                        if isinstance(leaf, ast.Name):
                            anchors.add(leaf.id)
    return anchors


def _anchored(nodes: Iterable[ast.AST], anchors: set[str]) -> bool:
    for node in nodes:
        for leaf in ast.walk(node):
            if isinstance(leaf, ast.Name) and (leaf.id == "__file__" or leaf.id in anchors):
                return True
    return False


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_sys_path(func: ast.AST) -> bool:
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "path"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "sys"
    )


def _is_constant(node: ast.AST, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _constant_strings(nodes: Iterable[ast.AST]) -> list[str]:
    out: list[str] = []
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                out.append(child.value)
    return out


def _constant_runs(nodes: Sequence[ast.AST], *, minimum: int = 2) -> list[list[str]]:
    runs: list[list[str]] = []
    current: list[str] = []
    for node in nodes:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            current.append(node.value)
        else:
            if len(current) >= minimum:
                runs.append(current)
            current = []
    if len(current) >= minimum:
        runs.append(current)
    return runs


def _flatten_div(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return [*_flatten_div(node.left), *_flatten_div(node.right)]
    return [node]


def _cargo_path_deps(data: Mapping[str, Any]) -> list[str]:
    out: list[str] = []

    def visit(table: Any) -> None:
        if not isinstance(table, dict):
            return
        for key, value in table.items():
            if key in {"dependencies", "dev-dependencies", "build-dependencies"} and isinstance(value, dict):
                for spec in value.values():
                    if isinstance(spec, dict) and isinstance(spec.get("path"), str):
                        out.append(spec["path"])
            elif key == "target" and isinstance(value, dict):
                for target in value.values():
                    visit(target)
            elif key == "workspace" and isinstance(value, dict):
                for member in value.get("members", []) or []:
                    if isinstance(member, str):
                        out.append(member)
                visit(value)

    visit(dict(data))
    return sorted(set(out))


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def runday_root_default() -> Path | None:
    raw = os.environ.get(RUNDAY_ROOT_ENV, DEFAULT_RUNDAY_ROOT)
    path = Path(raw)
    return path if path.is_dir() else None


def _external_record(external_root: Path, relative: str) -> tuple[dict[str, Any], bytes | None]:
    target = Path(relative) if os.path.isabs(relative) else external_root / relative
    record: dict[str, Any] = {
        "path": relative,
        "resolved_path": str(target),
        "versioned_in_git": False,
        "sha256": None,
        "size_bytes": None,
    }
    if target.is_file():
        data = target.read_bytes()
        record["sha256"] = _sha256(data)
        record["size_bytes"] = len(data)
        return record, data
    return record, None


def compute_gate_source_set(
    tree: GitTree,
    definition: GateDefinition,
    *,
    external_root: Path | None | str = "default",
) -> GateSourceSet:
    """Compute one gate's pinned source set at ``tree``'s commit."""

    if external_root == "default":
        external_root = runday_root_default()
    closure = _Closure(tree, definition.gate)
    result = closure.result
    for entry in sorted(set(definition.python_entries) | set(definition.js_entries) | set(definition.paths)):
        if not closure.add(entry, "entry"):
            result.missing_entries.append(entry)
    for module in sorted(set(definition.python_modules)):
        if not closure.resolve_module(module, [""], "entry:-m", main=True):
            result.missing_entries.append(f"-m {module}")
    for directory in sorted(set(definition.trees)):
        files = [path for path in tree.files_under(directory) if not _ignored(path)]
        if not files:
            result.missing_entries.append(f"tree:{directory}")
        for path in files:
            closure.add(path, f"tree:{directory}")
    for package in sorted(set(definition.web_builds)):
        if not tree.is_file(posixpath.join(package, "package.json")):
            result.missing_entries.append(f"web_build:{package}")
        closure.web_build(package, "entry")
    for unit in sorted(set(definition.native_units)):
        closure.native(unit, "entry")
    if external_root is not None:
        result.externals_scanned = True
        for relative in sorted(set(definition.external)):
            record, data = _external_record(Path(external_root), relative)
            result.externals.append(record)
            if data is None:
                continue
            text = data.decode("utf-8", errors="replace")
            via = f"external:{relative}"
            if relative.endswith(".py"):
                closure._python(relative, text, external=True)  # imports resolve against the repo
            closure.scan_text(text, via=via)
            for unit in closure.native_units:
                if unit.root not in closure._native_done and unit.named_in(text):
                    closure.native(unit.root, via)
    else:
        result.externals = [
            {
                "path": relative,
                "resolved_path": None,
                "versioned_in_git": False,
                "sha256": None,
                "size_bytes": None,
            }
            for relative in sorted(set(definition.external))
        ]
    closure.run()
    result.missing_entries = sorted(set(result.missing_entries))
    return result


def gate_manifest(tree: GitTree, result: GateSourceSet, definition: GateDefinition) -> dict[str, Any]:
    paths = sorted(result.files)
    tree.prefetch(paths)
    files = [{"path": path, "sha256": tree.sha256(path), "size_bytes": len(tree.read(path))} for path in paths]
    body = {
        "protocol": PROTOCOL,
        "gate": definition.gate,
        "definition": definition.normalized(),
        "missing_entries": result.missing_entries,
        "files": files,
    }
    externals = sorted(result.externals, key=lambda item: item["path"])
    return {
        **body,
        "source_digest": _sha256(canonical_bytes(body)),
        "file_count": len(files),
        "generated_pin_files": sorted(set(paths) & GENERATED_PIN_FILES),
        "provenance": {path: result.files[path] for path in paths},
        "unresolved_python_top_level": sorted(result.unresolved_python),
        "unresolved_js_packages": sorted(result.unresolved_js),
        "unexpanded_directories": sorted(result.unexpanded_directories),
        "externals_scanned": result.externals_scanned,
        "externals": externals,
        "external_digest": _sha256(canonical_bytes([[e["path"], e["sha256"]] for e in externals])),
    }


def build_manifest(
    tree: GitTree,
    definitions: Iterable[GateDefinition] = GATE_DEFINITIONS,
    *,
    external_root: Path | None | str = "default",
    gates: Iterable[str] | None = None,
) -> dict[str, Any]:
    selected = sorted(definitions, key=lambda d: d.gate)
    if gates is not None:
        wanted = set(gates)
        selected = [d for d in selected if d.gate in wanted]
    per_gate = {
        d.gate: gate_manifest(tree, compute_gate_source_set(tree, d, external_root=external_root), d)
        for d in selected
    }
    body = {
        "protocol": PROTOCOL,
        "definitions_version": DEFINITIONS_VERSION,
        "definitions_digest": definitions_digest(selected),
        "commit": tree.commit,
        "tree": tree.tree_oid,
        "gate_source_digests": {gate: m["source_digest"] for gate, m in per_gate.items()},
    }
    return {**body, "manifest_digest": _sha256(canonical_bytes(body)), "gates": per_gate}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--gate", action="append", help="limit to this gate (repeatable)")
    parser.add_argument("--runday-root", type=Path, help=f"out-of-repo run-day root (default ${RUNDAY_ROOT_ENV} or {DEFAULT_RUNDAY_ROOT})")
    parser.add_argument("--no-externals", action="store_true", help="do not read out-of-repo run-day files")
    parser.add_argument("--output", type=Path, help="write the manifest here (default stdout)")
    parser.add_argument("--summary", action="store_true", help="print one line per gate instead of JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    external: Path | None | str
    external = None if args.no_externals else (args.runday_root or "default")
    tree = GitTree(args.repo, args.commit)
    manifest = build_manifest(tree, external_root=external, gates=args.gate)
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    if args.summary or args.output:
        for gate, item in manifest["gates"].items():
            print(
                f"{gate}: {item['file_count']} files {item['source_digest']}"
                f" missing={len(item['missing_entries'])} externals={sum(1 for e in item['externals'] if e['sha256'])}/{len(item['externals'])}"
            )
        print(f"manifest {manifest['manifest_digest']} commit {manifest['commit']}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
