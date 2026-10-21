# SPDX-License-Identifier: AGPL-3.0-only
"""``mycelium.gate_requirements_spec.v1``: roles and requirements, never hosts.

A spec declares, for one gate, the ROLES a run needs (``entry``, ``source``,
``successor``, ``standby``, ``phone``, ...), how many members each role takes,
what each member must satisfy (peer class, OS family, architecture, backend,
usable memory for its share of the model, free disk, link RTT/throughput,
thermal/power state) and which evidence fields every case record must carry.

The spec digest is ``sha256`` over canonical JSON (sorted keys, no whitespace),
so it is stable under key order and formatting and changes with any value.
``specs/index.v1.json`` pins the digest of every shipped spec;
``load_pinned_spec`` refuses a spec file whose digest drifted from the pin.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SPEC_PROTOCOL = "mycelium.gate_requirements_spec.v1"
INDEX_PROTOCOL = "mycelium.gate_requirements_spec_index.v1"
SPEC_DIR = Path(__file__).resolve().parent / "specs"
INDEX_FILE = "index.v1.json"
PINNED_GATES = ("A1", "A5", "A6", "A7", "A10", "A11", "A12", "A14")

OS_FAMILIES = frozenset({"macos", "linux", "windows", "android"})
ARCHITECTURES = frozenset({"arm64", "x86_64"})
THERMAL_STATES = frozenset({"nominal", "fair", "serious", "critical"})
POWER_SOURCES = frozenset({"external", "battery"})
REPORT_PROTOCOLS = frozenset(
    {"mycelium.a13_capability_link_report.v1", "mycelium.membership.capability_report.v2"}
)

_SPEC_FIELDS = frozenset({"protocol", "gate_id", "spec_version", "feature", "claim_boundary", "model",
                          "resource_policy", "freshness", "roles", "cases", "evidence"})
_MODEL_FIELDS = frozenset({"model_id", "weight_bytes", "layer_count", "weight_bytes_source"})
_POLICY_FIELDS = frozenset({"usable_memory_fraction", "memory_reserve_bytes", "kv_headroom_fraction",
                            "disk_reserve_bytes"})
_ROLE_FIELDS = frozenset({"role", "count", "fit", "requirements", "may_share_member_with",
                          "same_backend_as"})
_CASE_FIELDS = frozenset({"case_id", "requires_roles"})
_EVIDENCE_FIELDS = frozenset({"per_case_fields", "binding_fields"})
_REQUIREMENT_KINDS: dict[str, str] = {
    "peer_classes": "segments",
    "os_families": "os",
    "architectures": "arch",
    "backends": "segments",
    "min_usable_memory_bytes": "bytes",
    "min_free_disk_bytes": "bytes",
    "max_link_rtt_ms": "positive",
    "min_link_bytes_per_second": "positive",
    "thermal_states": "thermal",
    "power_sources": "power",
    "report_protocols": "protocols",
}
_ENUMS = {"os": OS_FAMILIES, "arch": ARCHITECTURES, "thermal": THERMAL_STATES,
          "power": POWER_SOURCES, "protocols": REPORT_PROTOCOLS}
_GATE_ID = re.compile(r"A(?:[0-9]|1[0-9])b?\Z")
_ROLE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_SEGMENT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class GateSpecError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise GateSpecError(code)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def digest_of(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def spec_digest(document: Mapping[str, Any]) -> str:
    return digest_of(document)


def _int(value: Any, minimum: int = 0, maximum: int | None = None) -> bool:
    return type(value) is int and value >= minimum and (maximum is None or value <= maximum)


def _number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 2000


def _closed(value: Any, fields: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail(code)
    return value


def _requirement(name: str, value: Any) -> None:
    kind = _REQUIREMENT_KINDS.get(name)
    if kind is None:
        _fail("gate_spec_requirement_unknown")
    if kind == "bytes":
        ok = _int(value)
    elif kind == "positive":
        ok = _number(value) and float(value) > 0
    else:
        ok = (isinstance(value, list) and 0 < len(value) == len(set(value))
              and all(isinstance(item, str) for item in value)
              and value == sorted(value))
        if ok and kind == "segments":
            ok = all(_SEGMENT.fullmatch(item) for item in value)
        elif ok:
            ok = set(value) <= _ENUMS[kind]
    if not ok:
        _fail("gate_spec_requirement_invalid")


def _fit(value: Any, layer_count: int | None) -> None:
    if value in ("none", "full_model"):
        return
    if isinstance(value, Mapping) and set(value) == {"fraction_of_model"}:
        fraction = value["fraction_of_model"]
        if _number(fraction) and 0 < float(fraction) <= 1:
            return
    if isinstance(value, Mapping) and set(value) == {"layers"}:
        if _int(value["layers"], 1) and (layer_count is None or value["layers"] <= layer_count):
            return
    _fail("gate_spec_role_fit_invalid")


@dataclass(frozen=True)
class GateSpec:
    document: Mapping[str, Any]
    digest: str

    @property
    def gate_id(self) -> str:
        return str(self.document["gate_id"])

    @property
    def roles(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.document["roles"])

    def role(self, name: str) -> Mapping[str, Any]:
        for role in self.roles:
            if role["role"] == name:
                return role
        raise GateSpecError("gate_spec_role_unknown")

    def fit_bytes(self, role: Mapping[str, Any]) -> int:
        fit = role["fit"]
        model = self.document["model"]
        if fit == "none":
            return 0
        if fit == "full_model":
            return int(model["weight_bytes"])
        if "fraction_of_model" in fit:
            return int(math.ceil(model["weight_bytes"] * float(fit["fraction_of_model"])))
        return int(math.ceil(model["weight_bytes"] * fit["layers"] / model["layer_count"]))

    def required_memory_bytes(self, role: Mapping[str, Any]) -> int:
        policy = self.document["resource_policy"]
        need = int(math.ceil(self.fit_bytes(role) * (1.0 + float(policy["kv_headroom_fraction"]))))
        return max(need, int(role["requirements"].get("min_usable_memory_bytes", 0)))

    def required_disk_bytes(self, role: Mapping[str, Any]) -> int:
        policy = self.document["resource_policy"]
        fit = self.fit_bytes(role)
        need = fit + int(policy["disk_reserve_bytes"]) if fit else 0
        return max(need, int(role["requirements"].get("min_free_disk_bytes", 0)))


def parse_spec(document: Any) -> GateSpec:
    """Strictly validate one spec document; every refusal is a named code."""

    if not isinstance(document, Mapping) or set(document) != _SPEC_FIELDS:
        _fail("gate_spec_fields_invalid")
    if document["protocol"] != SPEC_PROTOCOL:
        _fail("gate_spec_protocol_invalid")
    if not isinstance(document["gate_id"], str) or not _GATE_ID.fullmatch(document["gate_id"]):
        _fail("gate_spec_gate_id_invalid")
    if not _int(document["spec_version"], 1):
        _fail("gate_spec_version_invalid")
    if not _text(document["feature"]) or not _text(document["claim_boundary"]):
        _fail("gate_spec_text_invalid")
    model = document["model"]
    if model is not None:
        _closed(model, _MODEL_FIELDS, "gate_spec_model_invalid")
        if not (_SEGMENT.fullmatch(str(model["model_id"]).lower()) and _int(model["weight_bytes"], 1)
                and _int(model["layer_count"], 1) and _text(model["weight_bytes_source"])):
            _fail("gate_spec_model_invalid")
    policy = _closed(document["resource_policy"], _POLICY_FIELDS, "gate_spec_resource_policy_invalid")
    if not (_number(policy["usable_memory_fraction"]) and 0 < policy["usable_memory_fraction"] <= 1
            and _int(policy["memory_reserve_bytes"]) and _int(policy["disk_reserve_bytes"])
            and _number(policy["kv_headroom_fraction"]) and 0 <= policy["kv_headroom_fraction"] <= 4):
        _fail("gate_spec_resource_policy_invalid")
    freshness = _closed(document["freshness"], frozenset({"max_report_age_seconds"}),
                        "gate_spec_freshness_invalid")
    if not _int(freshness["max_report_age_seconds"], 1, 3600):
        _fail("gate_spec_freshness_invalid")

    roles = document["roles"]
    if not isinstance(roles, list) or not 1 <= len(roles) <= 8:
        _fail("gate_spec_roles_invalid")
    names: list[str] = []
    for role in roles:
        _closed(role, _ROLE_FIELDS, "gate_spec_role_invalid")
        if not isinstance(role["role"], str) or not _ROLE.fullmatch(role["role"]):
            _fail("gate_spec_role_invalid")
        if role["role"] in names:
            _fail("gate_spec_role_duplicate")
        names.append(role["role"])
    for role in roles:
        count = role["count"]
        if not (isinstance(count, Mapping) and set(count) == {"min", "max"}
                and _int(count["min"], 0, 16) and _int(count["max"], 1, 16)
                and count["min"] <= count["max"]):
            _fail("gate_spec_role_count_invalid")
        _fit(role["fit"], model["layer_count"] if model else None)
        if role["fit"] != "none" and model is None:
            _fail("gate_spec_fit_requires_model")
        requirements = role["requirements"]
        if not isinstance(requirements, Mapping):
            _fail("gate_spec_requirement_invalid")
        for name, value in requirements.items():
            _requirement(name, value)
        shares = role["may_share_member_with"]
        if (not isinstance(shares, list) or len(set(shares)) != len(shares)
                or any(item not in names or item == role["role"] for item in shares)):
            _fail("gate_spec_role_reference_invalid")
        same = role["same_backend_as"]
        if same is not None and (same not in names or same == role["role"]):
            _fail("gate_spec_role_reference_invalid")

    cases = document["cases"]
    if not isinstance(cases, list) or not cases:
        _fail("gate_spec_cases_invalid")
    seen: set[str] = set()
    by_name = {role["role"]: role for role in roles}
    for case in cases:
        _closed(case, _CASE_FIELDS, "gate_spec_case_invalid")
        if (not isinstance(case["case_id"], str) or not _FIELD.fullmatch(case["case_id"])
                or case["case_id"] in seen or not isinstance(case["requires_roles"], Mapping)):
            _fail("gate_spec_case_invalid")
        seen.add(case["case_id"])
        for name, minimum in case["requires_roles"].items():
            if name not in by_name or not _int(minimum, 1, by_name[name]["count"]["max"]):
                _fail("gate_spec_case_invalid")
    evidence = _closed(document["evidence"], _EVIDENCE_FIELDS, "gate_spec_evidence_invalid")
    for field in ("per_case_fields", "binding_fields"):
        values = evidence[field]
        if (not isinstance(values, list) or not values or len(set(values)) != len(values)
                or not all(isinstance(item, str) and _FIELD.fullmatch(item) for item in values)):
            _fail("gate_spec_evidence_invalid")
    detached = json.loads(canonical_bytes(document))
    return GateSpec(document=detached, digest=spec_digest(detached))


def load_spec(path: str | Path) -> GateSpec:
    try:
        document = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        raise GateSpecError("gate_spec_unreadable") from None
    return parse_spec(document)


def load_index(spec_dir: str | Path | None = None) -> dict[str, Any]:
    root = Path(spec_dir) if spec_dir is not None else SPEC_DIR
    try:
        index = json.loads((root / INDEX_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        raise GateSpecError("gate_spec_index_unreadable") from None
    if (not isinstance(index, Mapping) or set(index) != {"protocol", "specs"}
            or index["protocol"] != INDEX_PROTOCOL or not isinstance(index["specs"], Mapping)):
        _fail("gate_spec_index_invalid")
    for entry in index["specs"].values():
        if (not isinstance(entry, Mapping) or set(entry) != {"file", "spec_digest"}
                or not _DIGEST.fullmatch(str(entry["spec_digest"]))
                or "/" in str(entry["file"]) or not str(entry["file"]).endswith(".json")):
            _fail("gate_spec_index_invalid")
    return dict(index)


def load_pinned_spec(gate_id: str, *, spec_dir: str | Path | None = None) -> GateSpec:
    """Load the shipped spec for ``gate_id`` and require its pinned digest."""

    root = Path(spec_dir) if spec_dir is not None else SPEC_DIR
    entry = load_index(root)["specs"].get(gate_id)
    if entry is None:
        _fail("gate_spec_unknown")
    spec = load_spec(root / entry["file"])
    if spec.gate_id != gate_id:
        _fail("gate_spec_gate_id_mismatch")
    if spec.digest != entry["spec_digest"]:
        _fail("gate_spec_digest_mismatch")
    return spec
