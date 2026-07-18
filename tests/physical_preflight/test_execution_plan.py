from __future__ import annotations

import ast

import pytest

from conftest import ABORT_CONDITIONS, NEGATIVE_TESTS, ROOT, canonical_bytes


def test_generated_plan_contains_complete_nonexecuted_qualification_recipe(
    plan: dict[str, object],
) -> None:
    from mycelium_physical_preflight import validate_and_generate

    result = validate_and_generate(canonical_bytes(plan), source_tree_root=ROOT)

    assert result["physical_qualification_executed"] is False
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert result["operator_plan_digest"].startswith("sha256:")
    assert result["authorization_statement"] == plan["authorization_statement"]
    assert result["source_files"] == plan["source_files"]
    assert result["identities"] == plan["identities"]
    assert result["hosts"] == plan["hosts"]
    assert result["coordinator"] == plan["coordinator"]

    phases = result["execution_phases"]
    assert [phase["phase"] for phase in phases] == [
        "authorization_gate",
        "host_local_path_revalidation",
        "stage_explicit_files",
        "credential_file_preflight",
        "coordinator_start_and_pending_status",
        "cold_runs",
        "warm_offline_runs",
        "coordinator_restart_and_report_resubmission",
        "stage_local_kv_prefill",
        "eight_step_decode_parity",
        "negative_tests",
        "evidence_copyback_and_verification",
        "run_scoped_cleanup",
        "abort_and_rollback",
    ]

    decode = phases[9]
    assert len(decode["actions"]) == 8
    assert [action["decode_index"] for action in decode["actions"]] == list(range(8))
    assert all(action["route_ready"] is False for action in decode["actions"])
    assert decode["requirements"] == plan["decode_parity"]

    negative = phases[10]
    assert [action["test_id"] for action in negative["actions"]] == NEGATIVE_TESTS
    assert all(action["expected_outcome"] == "fail_closed" for action in negative["actions"])
    assert phases[-1]["abort_conditions"] == ABORT_CONDITIONS


def test_rejects_incomplete_cold_warm_decode_negative_cleanup_and_rollback(
    plan: dict[str, object],
) -> None:
    from mycelium_physical_preflight import PreflightValidationError, validate_and_generate

    mutations = [
        ("invalid_run_matrix", lambda value: value["run_matrix"]["warm"].update(local_files_only=False)),
        ("invalid_decode_steps", lambda value: value["decode_parity"].update(decode_steps=7)),
        ("invalid_negative_tests", lambda value: value["negative_tests"].pop()),
        ("invalid_cleanup_plan", lambda value: value["cleanup"].update(remove_token_files=False)),
        (
            "invalid_rollback_plan",
            lambda value: value["rollback"].update(preserve_remote_evidence_on_copyback_failure=False),
        ),
        ("invalid_abort_conditions", lambda value: value["abort_conditions"].pop()),
    ]
    for code, mutate in mutations:
        import copy

        candidate = copy.deepcopy(plan)
        mutate(candidate)
        with pytest.raises(PreflightValidationError, match=code):
            validate_and_generate(canonical_bytes(candidate), source_tree_root=ROOT)


def test_source_files_are_explicit_sorted_relative_regular_files(plan: dict[str, object]) -> None:
    from mycelium_physical_preflight import PreflightValidationError, validate_and_generate

    bad_values = [
        (["runtime_loader.py", "*.py"], "unsafe_source_file"),
        (["../runtime_loader.py"], "unsafe_source_file"),
        ([str(ROOT / "runtime_loader.py")], "unsafe_source_file"),
        ([".gitignore"], "unsafe_source_file"),
        (["credentials.py"], "unsafe_source_file"),
        (["runtime_loader.py", "runtime_loader.py"], "duplicate_source_file"),
        (["runtime_loader.py", "mycelium_router/transports/iroh.py"], "noncanonical_source_file_order"),
        (["docs"], "source_file_not_regular"),
    ]
    for source_files, code in bad_values:
        candidate = dict(plan)
        candidate["source_files"] = source_files
        with pytest.raises(PreflightValidationError, match=code):
            validate_and_generate(canonical_bytes(candidate), source_tree_root=ROOT)


def test_package_has_no_execution_or_network_capabilities() -> None:
    package = ROOT / "mycelium_physical_preflight"
    forbidden_imports = {
        "asyncio",
        "http",
        "paramiko",
        "requests",
        "shutil",
        "socket",
        "subprocess",
        "urllib",
    }
    forbidden_calls = {"connect", "copy", "copy2", "mkdir", "remove", "rename", "replace", "rmdir", "unlink"}
    imports: set[str] = set()
    calls: set[str] = set()

    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)

    assert not (imports & forbidden_imports)
    assert not (calls & forbidden_calls)
