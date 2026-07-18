from __future__ import annotations

import ast
import importlib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2] / "mycelium_reference_oracle"
ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "gpt2",
    "hashlib",
    "importlib.metadata",
    "json",
    "math",
    "mlx.core",
    "pathlib",
    "re",
    "report",
    "types",
    "typing",
}


def imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_oracle_source_uses_only_explicitly_allowed_independent_imports() -> None:
    source_files = sorted(PACKAGE_ROOT.glob("*.py"))
    assert {path.name for path in source_files} == {"gpt2.py", "init.py", "report.py"}

    for path in source_files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        assert imported_modules(tree) <= ALLOWED_IMPORTS
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "__import__" not in called_names


def test_public_surface_loads_after_static_independence_audit() -> None:
    module = importlib.import_module("mycelium_reference_oracle.init")

    assert module.IMPLEMENTATION_VERSION == "tiny-gpt2-mlx-fp32-v1"
    assert callable(module.load_gpt2_fixture)
    assert callable(module.build_report)
