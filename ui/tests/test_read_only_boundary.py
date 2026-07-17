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


def production_source_files() -> list[Path]:
    return [
        path
        for path in source_files()
        if not path.name.startswith("test_")
        and not path.name.endswith(".test.ts")
        and not path.name.endswith(".test.tsx")
    ]


def test_ui_contains_no_mutating_http_calls() -> None:
    forbidden = re.compile(
        r"method\s*:\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]|"
        r"['\"](?:POST|PUT|PATCH|DELETE)['\"]",
        re.I,
    )
    for path in production_source_files():
        assert not forbidden.search(path.read_text(encoding="utf-8")), path


def test_browser_network_is_confined_to_read_only_source_transport() -> None:
    allowed_transport = UI_ROOT / "web" / "src" / "data" / "observatorySource.ts"
    forbidden_browser_writes = ("WebSocket", "XMLHttpRequest", "sendBeacon", "WebTransport")
    for path in production_source_files():
        text = path.read_text(encoding="utf-8")
        assert all(surface not in text for surface in forbidden_browser_writes), path
        if re.search(r"\bfetch\s*\(|\bEventSource\s*\(", text):
            assert path.resolve() == allowed_transport.resolve(), path


def test_elk_remains_lazy_and_confined_to_graph_layout() -> None:
    graph_module = UI_ROOT / "web" / "src" / "graph" / "graph.ts"
    elk_references: list[Path] = []
    for path in production_source_files():
        text = path.read_text(encoding="utf-8")
        if "elkjs" in text:
            elk_references.append(path.resolve())
            assert re.search(r"import\s*\(\s*['\"]elkjs/lib/elk\.bundled\.js['\"]\s*\)", text), path
            assert not re.search(
                r"^\s*import\s+(?!type\b).+\s+from\s+['\"]elkjs", text, re.M
            ), path
    assert elk_references == [graph_module.resolve()]


def test_fixture_sources_are_copies_not_symlinks() -> None:
    fixtures = UI_ROOT / "tests" / "fixtures"
    assert fixtures.is_dir()
    assert all(not path.is_symlink() for path in fixtures.rglob("*"))


def test_no_files_created_in_backend_by_ui_scaffold() -> None:
    assert UI_ROOT.parent == PROJECT_ROOT
    assert (UI_ROOT / "README.md").is_file()
