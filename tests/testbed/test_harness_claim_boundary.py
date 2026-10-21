# SPDX-License-Identifier: AGPL-3.0-only
"""Harness-level claim-boundary invariant (RCA wp2-emulation, defect 1).

``scripts/claim_boundary_audit.py`` is the specification: ``route_ready`` is how a
route claims it can serve, so that literal may only be minted under the
qualification authority.  The audit scans *tracked production source*, which means a
harness file becomes its subject only once it is committed - precisely why the
harness's own green test run did not catch ``mycelium_testbed/emulation.py`` writing
``"route_ready": True`` into an evidence document.

These tests guard the same class inside ``mycelium_testbed/**``, where it can be
checked *before* a commit, and they fail on the shape of the code rather than on a
particular file and line: a literal-true mint of a reserved name, however it is
spelled (dict key, keyword argument, assignment, walrus).  The scanner mirrors the
audit's own ``_literal_claims`` rather than inventing a second definition of the
class.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_ROOT = REPO_ROOT / "mycelium_testbed"

#: Reserved by the claim-boundary audit (``AUTHORITY_LITERALS``).
AUTHORITY_LITERALS = ("route_ready", "release_ready")

#: Claim-shaped names the audit does not reserve yet.  A harness that mints
#: ``qualified = True`` makes the same mistake in a different word, so the
#: invariant covers the wider set the RCA named.
CLAIM_SHAPED_LITERALS = ("physical_qualification", "qualified")

#: The one module allowed to mint ``route_ready = True``.
ROUTE_READY_AUTHORITY = "mycelium_qualification/qualifier.py"


def _harness_sources() -> list[Path]:
    return sorted(
        path for path in HARNESS_ROOT.rglob("*.py") if "__pycache__" not in path.parts
    )


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _target_name(target: ast.AST) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Subscript):
        key = target.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return key.value
    return None


def _literal_true_mints(tree: ast.AST, literals: tuple[str, ...]) -> list[tuple[str, int]]:
    """Every ``<literal>: True`` / ``<literal>=True`` the module mints.

    The shapes and the walk order follow ``scripts/claim_boundary_audit.py``; the
    failure this guards against is a *minted* claim, not a name that appears in a
    comment, a comparison or a value read from somewhere else.
    """

    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value in literals
                    and isinstance(value, ast.Constant)
                    and value.value is True
                ):
                    found.append((key.value, key.lineno))
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (
                    keyword.arg in literals
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    found.append((keyword.arg, keyword.value.lineno))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if not isinstance(value, ast.Constant) or value.value is not True:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = _target_name(target)
                if name in literals:
                    found.append((name, node.lineno))
        elif isinstance(node, ast.NamedExpr):
            if isinstance(node.value, ast.Constant) and node.value.value is True:
                name = _target_name(node.target)
                if name in literals:
                    found.append((name, node.lineno))
    return sorted(found, key=lambda item: (item[1], item[0]))


def _carried_reserved_values() -> list[tuple[str, int, str, str]]:
    """Reserved names the harness *carries* as a dict value, with the expression."""

    carried: list[tuple[str, int, str, str]] = []
    for path in _harness_sources():
        relative = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value in AUTHORITY_LITERALS
                ):
                    carried.append(
                        (relative, key.lineno, key.value, ast.unparse(value).strip())
                    )
    return sorted(carried)


def test_the_harness_mints_no_authority_reserved_literal() -> None:
    """No file under ``mycelium_testbed/**`` may mint a reserved claim."""

    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{line} mints {literal}=True"
        for path in _harness_sources()
        for literal, line in _literal_true_mints(_parse(path), AUTHORITY_LITERALS)
    ]
    assert offenders == [], (
        "the harness may not mint an authority-reserved literal; only "
        f"{ROUTE_READY_AUTHORITY} may "
        "(scripts/claim_boundary_audit.py is the specification): " + "; ".join(offenders)
    )


def test_the_harness_mints_no_claim_shaped_literal() -> None:
    """The wider set the RCA named: readiness claims in other words."""

    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{line} mints {literal}=True"
        for path in _harness_sources()
        for literal, line in _literal_true_mints(_parse(path), CLAIM_SHAPED_LITERALS)
    ]
    assert offenders == [], (
        "the harness may not mint a qualification claim under another name: "
        + "; ".join(offenders)
    )


def test_the_harness_only_carries_reserved_values_it_read_from_a_record() -> None:
    """A reserved name that stays in the harness must be a value, not a literal.

    ``mycelium_testbed/iroh_e2e.py`` records what the *product's* own evidence
    object said (``evidence.route_ready``).  That is a read of somebody else's
    record: it is allowed, and it is the only allowed shape.
    """

    offenders = [
        f"{relative}:{line} carries {name} = {expression!r}"
        for relative, line, name, expression in _carried_reserved_values()
        if not expression.startswith(("evidence.", "record.", "qualification.", "self."))
    ]
    assert offenders == [], (
        "a reserved literal may only be carried as a value read from the record "
        "that owns it (never a literal, never a computed guess): " + "; ".join(offenders)
    )
