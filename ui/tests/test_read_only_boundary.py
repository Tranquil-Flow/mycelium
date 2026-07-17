from __future__ import annotations

import re
from pathlib import Path

UI_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = UI_ROOT.parent
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}


def source_files() -> list[Path]:
    return [
        path
        for path in UI_ROOT.rglob("*")
        if path.is_file()
        and path.resolve() != Path(__file__).resolve()
        and path.suffix in TEXT_SUFFIXES
        and "node_modules" not in path.parts
        and "dist" not in path.parts
    ]


def test_ui_is_a_sealed_downstream_package() -> None:
    for path in source_files():
        text = path.read_text(encoding="utf-8")
        assert "sys.path" not in text, path
        assert "subprocess" not in text, path
        assert not re.search(r"from\s+(allocator|router|mycelium_gossip|planner_simulator)\b", text), path
        assert not re.search(r"import\s+(allocator|router|mycelium_gossip|planner_simulator)\b", text), path


def test_ui_contains_no_mutating_http_calls() -> None:
    forbidden = re.compile(r"\b(POST|PUT|PATCH|DELETE)\b|method\s*:\s*['\"](?:POST|PUT|PATCH|DELETE)", re.I)
    for path in source_files():
        if path.name.startswith("test_") or path.name.endswith(".test.ts") or path.name.endswith(".test.tsx"):
            continue
        assert not forbidden.search(path.read_text(encoding="utf-8")), path


def test_fixture_sources_are_copies_not_symlinks() -> None:
    fixtures = UI_ROOT / "tests" / "fixtures"
    assert fixtures.is_dir()
    assert all(not path.is_symlink() for path in fixtures.rglob("*"))


def test_no_files_created_in_backend_by_ui_scaffold() -> None:
    assert UI_ROOT.parent == PROJECT_ROOT
    assert (UI_ROOT / "README.md").is_file()
