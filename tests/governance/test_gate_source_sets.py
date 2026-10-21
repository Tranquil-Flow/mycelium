# SPDX-License-Identifier: AGPL-3.0-only
"""Per-gate pinned source sets and the D2 evidence carry-over check.

Owner decision D2 (2026-10-13): GREEN physical evidence obtained on one
candidate stands on the final candidate only when the gate's pinned source set
is byte-identical between the two; any change in a pinned file forces a rerun.
These tests pin the mechanics on small throwaway Git repositories and check
that the real gate definitions resolve on this checkout.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.gate_evidence_carryover import (
    RECEIPT_PROTOCOL,
    evaluate_carryover,
    verify_receipt,
)
from scripts.gate_source_sets import (
    GATE_DEFINITIONS,
    GENERATED_PIN_FILES,
    PROTOCOL,
    GateDefinition,
    GitTree,
    build_manifest,
    compute_gate_source_set,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_GIT_ENV_DROP = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def _commit(repo: Path, files: dict[str, str], message: str = "c") -> str:
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


BASE_FILES = {
    "entry.py": (
        "import subprocess, sys\n"
        "from pkg.a import run\n"
        "import yaml  # third party today\n"
        "def main():\n"
        "    subprocess.run([sys.executable, '-m', 'worker'])\n"
        "    return run()\n"
    ),
    "pkg/__init__.py": "",
    "pkg/a.py": "from .b import thing\ndef run():\n    return thing()\n",
    "pkg/b.py": (
        "from pathlib import Path\n"
        "import helper\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "def thing():\n"
        "    return (ROOT / 'contracts' / 'x.json').read_text() + helper.VALUE\n"
    ),
    "helper.py": (
        "import importlib\n"
        "VALUE = 'v'\n"
        "def plugin():\n"
        "    return importlib.import_module('plugins.alpha')\n"
        "SIDECAR = 'mycelium-demo-sidecar'\n"
    ),
    "plugins/__init__.py": "",
    "plugins/alpha.py": "A = 1\n",
    "plugins/beta.py": "B = 2\n",
    "worker/__init__.py": "",
    "worker/__main__.py": "from worker.loop import go\ngo()\n",
    "worker/loop.py": "def go():\n    return 0\n",
    # Data naming a native crate must not pin it (only executable text does).
    "contracts/x.json": '{"crate": "native/unused", "name": "unused-crate"}\n',
    "data/a.json": "{}\n",
    "data/b.json": "{}\n",
    "reader.py": (
        "from pathlib import Path\n"
        "ROOT = Path(__file__).parent\n"
        "A = ROOT / 'data' / 'a.json'\n"
        "DOC = 'docs'\n"
        "NESTED = ROOT / 'nested' / 'unrelated.py'\n"
    ),
    "lister.py": "from pathlib import Path\nDATA = Path(__file__).parent / 'data'\n",
    # Runtime-relative names that merely coincide with a repository directory.
    "stager.py": (
        "from pathlib import Path\n"
        "def stage(state_root):\n"
        "    out = state_root / 'data'\n"
        "    rel = Path('data')\n"
        "    return [p for p in out.iterdir() if not p.name.startswith(('data/', 'tmp/'))], rel\n"
    ),
    "native/demo/Cargo.toml": (
        "[package]\nname = \"mycelium-demo\"\n\n[dependencies]\n"
        "core = { path = \"../core\" }\n"
    ),
    "native/demo/Cargo.lock": "# lock\n",
    "native/demo/src/bin/mycelium-demo-sidecar.rs": "fn main() {}\n",
    "native/core/Cargo.toml": "[package]\nname = \"demo-core\"\n",
    "native/core/src/lib.rs": "pub fn f() {}\n",
    "native/unused/Cargo.toml": "[package]\nname = \"unused-crate\"\n",
    "native/unused/src/lib.rs": "pub fn g() {}\n",
    "web/scripts/closeout.mjs": "import { helper } from '../lib/util.mjs';\nhelper();\n",
    "web/lib/util.mjs": "export function helper() { return 1; }\n",
    "unrelated.py": "X = 1\n",
    "docs/notes.md": "notes\n",
}

DEMO_GATE = GateDefinition(
    gate="AX",
    python_entries=("entry.py",),
    js_entries=("web/scripts/closeout.mjs",),
    rationale="synthetic test gate",
)


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    base = _commit(root, BASE_FILES, "base")
    return root, base


def _paths(tree: GitTree, definition: GateDefinition) -> set[str]:
    return set(compute_gate_source_set(tree, definition).files)


def test_closure_includes_known_transitive_dependencies(repo: tuple[Path, str]) -> None:
    root, base = repo
    paths = _paths(GitTree(root, base), DEMO_GATE)

    # entry -> pkg.a -> (relative) pkg.b -> helper; parent package __init__ too.
    for expected in ("entry.py", "pkg/__init__.py", "pkg/a.py", "pkg/b.py", "helper.py"):
        assert expected in paths, expected
    # Data file read through a Path join.
    assert "contracts/x.json" in paths
    # `-m worker` spawned as a child process, and its own imports.
    assert {"worker/__init__.py", "worker/__main__.py", "worker/loop.py"} <= paths
    # importlib.import_module with a literal module name.
    assert {"plugins/__init__.py", "plugins/alpha.py"} <= paths
    # Native build inputs: the crate whose binary name the Python code names,
    # plus its Cargo path dependency; an unreferenced crate is not pinned.
    assert {
        "native/demo/Cargo.toml",
        "native/demo/Cargo.lock",
        "native/demo/src/bin/mycelium-demo-sidecar.rs",
        "native/core/Cargo.toml",
        "native/core/src/lib.rs",
    } <= paths
    assert "native/unused/src/lib.rs" not in paths
    # JS entry and its relative import.
    assert {"web/scripts/closeout.mjs", "web/lib/util.mjs"} <= paths
    # Unrelated code and docs are not pinned.
    assert "unrelated.py" not in paths
    assert "plugins/beta.py" not in paths
    assert "docs/notes.md" not in paths


def test_unrelated_change_carries(repo: tuple[Path, str]) -> None:
    root, base = repo
    final = _commit(root, {"unrelated.py": "X = 2\n", "docs/notes.md": "more\n"})
    receipt = evaluate_carryover(root, base, final, definitions=(DEMO_GATE,))
    gate = receipt["gates"]["AX"]
    assert gate["verdict"] == "CARRY"
    assert gate["changed_files"] == []
    assert gate["evidence_source_digest"] == gate["final_source_digest"]


def test_every_pinned_file_change_forces_rerun(repo: tuple[Path, str]) -> None:
    root, base = repo
    pinned = sorted(_paths(GitTree(root, base), DEMO_GATE) - GENERATED_PIN_FILES)
    assert len(pinned) >= 15
    for path in pinned:
        _git(root, "checkout", "-q", "--detach", base)
        original = (root / path).read_bytes()
        (root / path).write_bytes(original + b"\n# changed\n")
        final = _commit(root, {}, f"touch {path}")
        receipt = evaluate_carryover(root, base, final, definitions=(DEMO_GATE,))
        gate = receipt["gates"]["AX"]
        assert gate["verdict"] == "RERUN", path
        assert [item["path"] for item in gate["changed_files"]] == [path]
        assert gate["changed_files"][0]["status"] == "modified"


def test_deleting_a_pinned_file_forces_rerun(repo: tuple[Path, str]) -> None:
    root, base = repo
    (root / "plugins" / "alpha.py").unlink()
    final = _commit(root, {}, "delete alpha")
    gate = evaluate_carryover(root, base, final, definitions=(DEMO_GATE,))["gates"]["AX"]
    assert gate["verdict"] == "RERUN"
    assert gate["changed_files"] == [
        {
            "path": "plugins/alpha.py",
            "status": "removed",
            "evidence_sha256": gate["changed_files"][0]["evidence_sha256"],
            "final_sha256": None,
        }
    ]


def test_new_module_shadowing_a_third_party_import_forces_rerun(repo: tuple[Path, str]) -> None:
    root, base = repo
    final = _commit(root, {"yaml.py": "SHADOW = True\n"}, "shadow yaml")
    gate = evaluate_carryover(root, base, final, definitions=(DEMO_GATE,))["gates"]["AX"]
    assert gate["verdict"] == "RERUN"
    assert [(c["path"], c["status"]) for c in gate["changed_files"]] == [("yaml.py", "added")]


def test_manifest_is_stable_under_definition_and_entry_ordering(repo: tuple[Path, str]) -> None:
    root, base = repo
    tree = GitTree(root, base)
    other = GateDefinition(gate="AY", python_entries=("unrelated.py",), rationale="second")
    shuffled = GateDefinition(
        gate="AX",
        python_entries=DEMO_GATE.python_entries,
        js_entries=DEMO_GATE.js_entries,
        rationale=DEMO_GATE.rationale,
    )
    first = build_manifest(tree, (DEMO_GATE, other), external_root=root)
    second = build_manifest(GitTree(root, base), (other, shuffled), external_root=root)
    assert first["protocol"] == PROTOCOL
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    files = first["gates"]["AX"]["files"]
    assert [entry["path"] for entry in files] == sorted(entry["path"] for entry in files)
    assert all(entry["sha256"].startswith("sha256:") for entry in files)
    assert first["manifest_digest"].startswith("sha256:")


def test_out_of_repo_driver_is_scanned_and_digested_but_flagged_unversioned(
    repo: tuple[Path, str], tmp_path: Path
) -> None:
    root, base = repo
    runday = tmp_path / "runday"
    runday.mkdir()
    (runday / "window.sh").write_text(
        'cd "$C" && "$PY" -B scripts/never_present.py\n'
        '"$PY" -B "$C/unrelated.py" --flag\n'
    )
    gate = GateDefinition(gate="AZ", external=("runday/window.sh", "runday/missing.sh"), rationale="ext")
    result = compute_gate_source_set(GitTree(root, base), gate, external_root=tmp_path)
    assert "unrelated.py" in result.files
    externals = {item["path"]: item for item in result.externals}
    assert externals["runday/window.sh"]["versioned_in_git"] is False
    assert externals["runday/window.sh"]["sha256"].startswith("sha256:")
    assert externals["runday/missing.sh"]["sha256"] is None


def test_receipt_digest_binds_the_verdicts(repo: tuple[Path, str]) -> None:
    root, base = repo
    final = _commit(root, {"pkg/b.py": BASE_FILES["pkg/b.py"] + "# edit\n"})
    receipt = evaluate_carryover(root, base, final, definitions=(DEMO_GATE,))
    assert receipt["protocol"] == RECEIPT_PROTOCOL
    assert receipt["evidence_commit"] == base and receipt["final_commit"] == final
    assert verify_receipt(receipt) == receipt["receipt_digest"]
    tampered = json.loads(json.dumps(receipt))
    tampered["gates"]["AX"]["verdict"] = "CARRY"
    with pytest.raises(ValueError, match="gate_carryover_receipt_digest_mismatch"):
        verify_receipt(tampered)


def test_path_join_pins_the_joined_file_not_its_parent_directories(repo: tuple[Path, str]) -> None:
    root, base = repo
    tree = GitTree(root, base)
    reader = _paths(tree, GateDefinition(gate="R", python_entries=("reader.py",)))
    assert "data/a.json" in reader
    # Neither the inner join ``ROOT / 'data'`` nor the bare word "docs" is a
    # directory read.
    assert "data/b.json" not in reader
    assert "docs/notes.md" not in reader
    # A join's last part is not a repository-root file name on its own.
    assert "unrelated.py" not in reader
    # A join that ends at a directory pins the data files under it.
    lister = _paths(tree, GateDefinition(gate="L", python_entries=("lister.py",)))
    assert {"data/a.json", "data/b.json"} <= lister
    # A one-component directory name joined onto a runtime path, a relative
    # Path('data') or a 'data/' prefix test is not a read of the repo dir.
    stager = _paths(tree, GateDefinition(gate="S", python_entries=("stager.py",)))
    assert stager == {"stager.py"}


def test_change_only_in_generated_pin_files_carries_under_r1(repo: tuple[Path, str]) -> None:
    root, base = repo
    files = {
        "contracts/contract-manifest.v1.json": '{"v": 1}\n',
        "audit.py": "from pathlib import Path\nM = Path(__file__).parent / 'contracts' / 'contract-manifest.v1.json'\n",
    }
    evidence = _commit(root, files, "pins")
    final = _commit(root, {"contracts/contract-manifest.v1.json": '{"v": 2}\n'}, "repin")
    gate = GateDefinition(gate="G", python_entries=("audit.py",))
    item = evaluate_carryover(root, evidence, final, definitions=(gate,))["gates"]["G"]
    assert item["verdict"] == "CARRY"
    assert item["changed_only_generated_pins"] is True
    assert "generated_pins_only" in item["reasons"]
    assert "pinned_source_changed" not in item["reasons"]
    assert [c["path"] for c in item["changed_files"]] == ["contracts/contract-manifest.v1.json"]


def test_generated_pin_change_with_a_code_change_still_reruns(repo: tuple[Path, str]) -> None:
    root, base = repo
    files = {
        "contracts/contract-manifest.v1.json": '{"v": 1}\n',
        "audit.py": "from pathlib import Path\nM = Path(__file__).parent / 'contracts' / 'contract-manifest.v1.json'\n",
    }
    evidence = _commit(root, files, "pins")
    final = _commit(
        root,
        {"contracts/contract-manifest.v1.json": '{"v": 2}\n', "audit.py": files["audit.py"] + "X = 1\n"},
        "code and repin",
    )
    gate = GateDefinition(gate="G", python_entries=("audit.py",))
    item = evaluate_carryover(root, evidence, final, definitions=(gate,))["gates"]["G"]
    assert item["verdict"] == "RERUN"
    assert item["changed_only_generated_pins"] is False
    assert "pinned_source_changed" in item["reasons"]


def test_external_window_npm_build_pins_the_web_bundle(repo: tuple[Path, str], tmp_path: Path) -> None:
    root, _ = repo
    web = {
        "webapp/package.json": '{"scripts": {"build:live": "tsc -b && vite build"}}\n',
        "webapp/package-lock.json": "{}\n",
        "webapp/vite.config.ts": "export default {};\n",
        "webapp/index.html": '<script type="module" src="/src/main.ts"></script>\n',
        "webapp/src/main.ts": "import { view } from './view';\nview();\n",
        "webapp/src/view.ts": "export function view() {}\n",
        "webapp/src/unused.ts": "export const unused = 1;\n",
    }
    commit = _commit(root, web, "web")
    runday = tmp_path / "rd"
    runday.mkdir()
    (runday / "w.sh").write_text('( cd "$C/webapp" && npm run build:live ) > build.log\n')
    gate = GateDefinition(gate="W", external=("w.sh",))
    paths = set(compute_gate_source_set(GitTree(root, commit), gate, external_root=runday).files)
    assert {
        "webapp/package.json",
        "webapp/package-lock.json",
        "webapp/vite.config.ts",
        "webapp/index.html",
        "webapp/src/main.ts",
        "webapp/src/view.ts",
    } <= paths
    assert "webapp/src/unused.ts" not in paths


def _repo_head() -> str | None:
    try:
        return _git(REPO_ROOT, "rev-parse", "--verify", "HEAD^{commit}")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def test_real_gate_definitions_resolve_on_this_checkout() -> None:
    head = _repo_head()
    if head is None:
        pytest.skip("checkout is not a Git work tree; gate source sets are commit-bound")
    tree = GitTree(REPO_ROOT, head)
    gates = {definition.gate for definition in GATE_DEFINITIONS}
    assert {"A1", "A5", "A6", "A7", "A9", "A10", "A11", "A12", "A13", "A14", "A15"} <= gates
    for definition in GATE_DEFINITIONS:
        result = compute_gate_source_set(tree, definition, external_root=None)
        assert result.missing_entries == [], (definition.gate, result.missing_entries)
        assert result.files, definition.gate
    a6 = compute_gate_source_set(
        tree, next(d for d in GATE_DEFINITIONS if d.gate == "A6"), external_root=None
    ).files
    # Node process and a transitive product dependency of the live serve.
    assert "physical_inference_node.py" in a6
    assert "mycelium_live/route.py" in a6
    assert "native/iroh_transport/Cargo.toml" in a6
    assert "ui/web/src/main.tsx" in a6
