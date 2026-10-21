# SPDX-License-Identifier: AGPL-3.0-only
"""A14b §3, §7 and §8: the globe renderer and region sources stay inside the boundary.

These are deterministic source-boundary checks over the shipped renderer and the
operator declaration reader. The renderer's behaviour (toggle, equivalence,
selection sync, privacy of the rendered DOM) is exercised by
`ui/web/src/features/routeExplorer/globe.test.tsx`; this module binds the
cross-language mirror and the "no network-derived region" rule from the Python
side so neither can drift silently.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from . import conformance

ROOT = Path(__file__).resolve().parents[2]
EXPLORER = ROOT / "ui/web/src/features/routeExplorer"
GLOBE_TS = EXPLORER / "globe.ts"
GLOBE_TSX = EXPLORER / "RegionGlobe.tsx"
NETWORK_MODULES = {"socket", "ipaddress", "dns", "urllib", "http", "requests", "geoip2", "maxminddb"}


def _ts_display_cells() -> dict[str, str]:
    source = GLOBE_TS.read_text(encoding="utf-8")
    block = re.search(r"DISPLAY_CELLS[^=]*=\s*Object\.freeze\(\{(.*?)\}\)", source, re.S)
    assert block is not None, "globe.ts no longer declares DISPLAY_CELLS as a frozen literal"
    return dict(re.findall(r"(\w+):\s*'([^']*)'", block.group(1)))


def test_renderer_display_cells_mirror_the_product_constant():
    coarse = conformance.contract()
    assert _ts_display_cells() == dict(coarse.DISPLAY_CELLS) == conformance.DISPLAY_CELLS


def test_renderer_reads_only_the_published_node_region():
    # The globe may consume node.region and nothing else from the projection:
    # no segment, metric, transport, endpoint or evidence field, and no fetch.
    for path in (GLOBE_TS, GLOBE_TSX):
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "segments", "metrics", "rtt", "transport", "endpoint_id", "relay_url",
            "coarse_region_evidence", "authority_digest", "fetch(", "XMLHttpRequest",
            "WebSocket", "navigator.geolocation", "Intl.DateTimeFormat", "timeZone",
            "latitude", "longitude", "geohash",
        ):
            assert forbidden not in source, f"{path.name} references {forbidden!r}"
    used = set(re.findall(r"\bnode\.(\w+)", GLOBE_TS.read_text(encoding="utf-8")))
    assert used == {"region"}


def test_renderer_imports_only_the_static_land_asset_and_projection_code():
    # The only geography the globe may carry is the bundled, decorative Natural
    # Earth 1:110m land outline, imported statically (never fetched at runtime).
    allowed = {
        GLOBE_TS: {"d3-geo", "topojson-client", "topojson-specification", "world-atlas/land-110m.json", "./contracts"},
        GLOBE_TSX: {"./contracts", "./globe"},
    }
    for path, expected in allowed.items():
        source = path.read_text(encoding="utf-8")
        assert set(re.findall(r"^import [^;]*? from '([^']+)';", source, re.M)) == expected, path.name
        assert "import(" not in source
        assert "world-atlas" not in source or "world-atlas/land-110m.json" in source


def test_renderer_never_puts_a_cell_id_or_peer_identity_into_the_dom():
    source = GLOBE_TSX.read_text(encoding="utf-8")
    assert "DISPLAY_CELLS" not in source
    assert "cell_" not in source
    assert ".id" not in source


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_region_sources_cannot_reach_network_identity():
    # §3 rule 5: no IP, DNS name, hostname, EndpointID or measurement may derive
    # or refine a region. The only region producers are the closed reduction and
    # the owner-private declaration reader; neither can resolve or look up a peer.
    for module in ("coarse_region.py", "region_declarations.py"):
        imported = _imports(ROOT / "mycelium_product_spine" / module)
        assert not imported & NETWORK_MODULES, f"{module} imports {imported & NETWORK_MODULES}"
