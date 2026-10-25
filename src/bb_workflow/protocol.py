"""Portable structured responses, validated again after the provider returns."""

from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from .requirements import Criterion, Requirements, RequirementsError, _validate_requirements


class ProtocolError(ValueError):
    """An agent response does not meet the workflow contract."""


def _object(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_TEXT = {"type": "string", "minLength": 1}
FINDING_SCHEMA = _object(
    {
        "id": _TEXT,
        "path": _TEXT,
        "line": {"type": "integer", "minimum": 1},
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
        "title": _TEXT,
        "detail": _TEXT,
        "evidence": _TEXT,
    }
)
RESPONSE_SCHEMA = _object(
    {
        "status": {"type": "string", "enum": ["completed", "blocked"]},
        "summary": _TEXT,
        "findings": {"type": "array", "items": FINDING_SCHEMA},
        "decisions": {
            "type": "array",
            "items": _object(
                {
                    "finding_id": _TEXT,
                    "disposition": {
                        "type": "string",
                        "enum": ["confirmed", "rejected", "duplicate", "unresolved"],
                    },
                    "rationale": _TEXT,
                    "duplicate_of": {"type": "string"},
                }
            ),
        },
    }
)


_BINDING_SCHEMA = _object(
    {"candidate_id": _TEXT, "requirements_id": _TEXT, "verification_plan_id": _TEXT}
)
_VERDICT_SCHEMA = {"type": "string", "enum": ["satisfied", "unmet", "unresolved"]}
_RESPONSE_SCHEMA_V2 = _object(
    {
        **RESPONSE_SCHEMA["properties"],
        "schema_version": {"type": "integer", "enum": [2]},
        "binding": _BINDING_SCHEMA,
        "criterion_evidence": {
            "type": "array",
            "items": _object({"criterion_id": _TEXT, "evidence": _TEXT}),
        },
        "criterion_assessments": {
            "type": "array",
            "items": _object(
                {"criterion_id": _TEXT, "verdict": _VERDICT_SCHEMA, "evidence": _TEXT}
            ),
        },
        "criterion_decisions": {
            "type": "array",
            "items": _object(
                {"criterion_id": _TEXT, "verdict": _VERDICT_SCHEMA, "rationale": _TEXT}
            ),
        },
    }
)


def response_schema(version: int = 1) -> dict:
    """Return an isolated portable schema, preserving the legacy response contract."""
    if type(version) is not int or version not in {1, 2}:
        raise ProtocolError("Unsupported response schema version")
    return deepcopy(RESPONSE_SCHEMA if version == 1 else _RESPONSE_SCHEMA_V2)


def _validate_acceptance(
    value: dict,
    role: str,
    expected_binding: dict | None,
    criteria: tuple[Criterion, ...],
    disputed_criteria: tuple[str, ...],
) -> None:
    if expected_binding is None:
        raise ProtocolError("Version 2 requires the expected input binding")
    _validate(expected_binding, _BINDING_SCHEMA, "expected_binding")
    if value["binding"] != expected_binding:
        raise ProtocolError("Response binding does not match the expected input")
    try:
        _validate_requirements(Requirements(criteria=criteria))
    except RequirementsError as exc:
        raise ProtocolError(f"Invalid response criteria: {exc}") from exc
    known = {criterion.id for criterion in criteria}
    semantic = {criterion.id for criterion in criteria if criterion.kind == "semantic"}
    if (
        type(disputed_criteria) is not tuple
        or any(type(identifier) is not str for identifier in disputed_criteria)
        or len(disputed_criteria) != len(set(disputed_criteria))
        or not set(disputed_criteria) <= semantic
    ):
        raise ProtocolError("Disputed criteria must be unique known semantic IDs")
    role_fields = {
        "implementation": "criterion_evidence",
        "fixer": "criterion_evidence",
        "reviewer_a": "criterion_assessments",
        "reviewer_b": "criterion_assessments",
        "triage": "criterion_decisions",
    }
    if role not in role_fields:
        raise ProtocolError("Unsupported acceptance response role")
    for field in ("criterion_evidence", "criterion_assessments", "criterion_decisions"):
        items = value[field]
        if field != role_fields[role] and items:
            raise ProtocolError(f"{role} must not return {field}")
        ids = [item["criterion_id"] for item in items]
        if len(ids) != len(set(ids)) or not set(ids) <= known:
            raise ProtocolError(f"{field} requires unique known criterion IDs")
    if role in {"reviewer_a", "reviewer_b"}:
        if {item["criterion_id"] for item in value["criterion_assessments"]} != semantic:
            raise ProtocolError("Reviewers must assess every semantic criterion exactly once")
    elif role == "triage":
        if {item["criterion_id"] for item in value["criterion_decisions"]} != set(
            disputed_criteria
        ):
            raise ProtocolError("Triage must decide every disputed semantic criterion exactly once")


def _validate(value: Any, schema: dict, where: str = "response") -> None:
    kind = schema["type"]
    expected = {"object": dict, "array": list, "string": str, "integer": int}[kind]
    if type(value) is not expected:
        raise ProtocolError(f"{where}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ProtocolError(f"{where}: unsupported value {value!r}")
    if kind == "object":
        if set(value) != set(schema["properties"]):
            raise ProtocolError(f"{where}: missing or unexpected fields")
        for name, subschema in schema["properties"].items():
            _validate(value[name], subschema, f"{where}.{name}")
    elif kind == "array":
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{where}[{index}]")
    elif kind == "string" and schema.get("minLength") and not value.strip():
        raise ProtocolError(f"{where}: must not be blank")
    elif kind == "integer" and value < schema.get("minimum", value):
        raise ProtocolError(f"{where}: below minimum")


def validate_response(
    value: Any,
    role: str,
    workspace: Path,
    baseline_root: Path | None = None,
    *,
    version: int = 1,
    expected_binding: dict | None = None,
    criteria: tuple[Criterion, ...] = (),
    disputed_criteria: tuple[str, ...] = (),
) -> dict:
    _validate(value, response_schema(version))
    if version == 2:
        _validate_acceptance(value, role, expected_binding, criteria, disputed_criteria)
    if role != "triage" and value["decisions"]:
        raise ProtocolError(f"{role} must not return triage decisions")
    if role not in {"reviewer_a", "reviewer_b"} and value["findings"]:
        raise ProtocolError(f"{role} must not return review findings")
    ids = set()
    for finding in value["findings"]:
        name = finding["path"]
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise ProtocolError(f"invalid finding path: {name}")
        target = (workspace / name).resolve()
        allowed_root = workspace.resolve()
        if not target.exists() and baseline_root is not None:
            target = (baseline_root / name).resolve()
            allowed_root = baseline_root.resolve()
        if not target.is_relative_to(allowed_root) or not target.is_file():
            raise ProtocolError(f"finding does not reference a workspace file: {name}")
        if finding["line"] > max(1, len(target.read_bytes().splitlines())):
            raise ProtocolError(f"finding line exceeds file: {name}:{finding['line']}")
        if finding["id"] in ids:
            raise ProtocolError("duplicate finding ID within a review")
        ids.add(finding["id"])
    return value


def merge_findings(reviews: list[dict]) -> list[dict]:
    """Collapse only exact duplicates; preserve provenance from both reviews."""
    merged: dict[tuple, dict] = {}
    for index, review in enumerate(reviews):
        for finding in review["findings"]:
            key = tuple((k, v) for k, v in sorted(finding.items()) if k != "id")
            source = f"reviewer_{'ab'[index]}:{finding['id']}"
            if key in merged:
                merged[key]["sources"].append(source)
            else:
                merged[key] = {**finding, "id": f"F{len(merged) + 1}", "sources": [source]}
    return list(merged.values())


def resolve_triage(findings: list[dict], response: dict) -> tuple[list[dict], list[dict]]:
    """Require one decision per finding; unresolved never becomes a clean review."""
    by_id = {finding["id"]: finding for finding in findings}
    decisions = response["decisions"]
    ids = [item["finding_id"] for item in decisions]
    if len(ids) != len(set(ids)) or set(ids) != set(by_id):
        raise ProtocolError("triage must decide each finding exactly once")
    decision_map = {item["finding_id"]: item for item in decisions}
    for item in decisions:
        if item["disposition"] == "duplicate":
            target = decision_map.get(item["duplicate_of"])
            if target is None or target["disposition"] != "confirmed":
                raise ProtocolError("duplicate must refer directly to a confirmed finding")
        elif item["duplicate_of"]:
            raise ProtocolError("duplicate_of must be empty for non-duplicate decisions")
    confirmed = [
        deepcopy(by_id[item["finding_id"]])
        for item in decisions
        if item["disposition"] == "confirmed"
    ]
    severity_rank = {
        name: index for index, name in enumerate(("low", "medium", "high", "critical"))
    }
    for finding in confirmed:
        duplicates = [
            by_id[item["finding_id"]]
            for item in decisions
            if item["disposition"] == "duplicate" and item["duplicate_of"] == finding["id"]
        ]
        finding["severity"] = max(
            [finding, *duplicates], key=lambda item: severity_rank[item["severity"]]
        )["severity"]
        if duplicates:
            finding["duplicates"] = deepcopy(duplicates)
    unresolved = [
        by_id[item["finding_id"]] for item in decisions if item["disposition"] == "unresolved"
    ]
    return confirmed, unresolved
