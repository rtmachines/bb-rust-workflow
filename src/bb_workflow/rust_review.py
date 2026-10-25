"""Versioned Rust review policy expressed as ordinary frozen acceptance criteria."""

from __future__ import annotations

import json
from dataclasses import asdict, replace

from .config import RustReviewConfig, RustReviewEvidence, parse_rust_review
from .requirements import (
    Criterion,
    Requirements,
    RequirementsError,
    _validate_requirements,
    cargo_test_kind,
)
from .verification import VerificationPlan

PREFIX = "rust-review-"
SOURCE = "https://github.com/rtmachines/rtm-coding/tree/268582b5c4d8f91bc709294b9ebbe270a181f9b2/skills/rust-pr-review"
_MARKER = "bb-rust-workflow:rust-pr-review:v1"
_SAFETY_IDS = frozenset(PREFIX + name for name in ("scope", "memory", "async", "sync"))


def rust_review_context(policy: RustReviewConfig) -> dict:
    """Validate programmatic policies as strictly as TOML and return portable context."""
    if (
        type(policy) is not RustReviewConfig
        or type(policy.required) is not tuple
        or type(policy.evidence) is not tuple
        or any(type(item) is not RustReviewEvidence for item in policy.evidence)
    ):
        raise RequirementsError("Invalid immutable rust_review policy")
    data = json.loads(json.dumps(asdict(policy)))
    parse_rust_review(data)
    return data


def _criteria(policy: RustReviewConfig, plan: VerificationPlan) -> tuple[Criterion, ...]:
    # Version 1 text is part of persisted requirement identity: retain it for replay.
    data = rust_review_context(policy)
    scope = json.dumps(data, sort_keys=True, separators=(",", ":"))
    criteria = [
        Criterion(
            PREFIX + "spec",
            "The candidate implements the requested behavior without contradictory scope.",
            "semantic",
            "Perform a separate specification pass against the task and all user criteria; "
            "identify missing, unrequested and contradictory behavior with source evidence.",
        ),
        Criterion(
            PREFIX + "scope",
            "Declared Rust safety obligations cover the actual changed behavior.",
            "semantic",
            f"{_MARKER}; source={SOURCE}; policy={scope}. Inspect added and removed code and "
            "affected callers. Justify inapplicable categories. Any applicable obligation "
            "absent from this frozen policy or plan is unresolved; request a new run.",
        ),
        Criterion(
            PREFIX + "memory",
            "Affected memory boundaries have targeted Miri evidence and an aliasing argument.",
            "semantic",
            "For affected unsafe/FFI or safety-sensitive callers, assess the declared memory "
            "tests against the exact implementation and independently explain pointer creation, "
            "access, invalidation, ownership and foreign guarantees. Missing tests or an "
            "unresolved guarantee is unresolved. Otherwise justify why memory is inapplicable.",
        ),
        Criterion(
            PREFIX + "async",
            "Affected progress, cancellation and task lifecycle have adequate targeted tests.",
            "semantic",
            "Map applicable deadlock/progress, cancellation and task-lifecycle scenarios to "
            "declared executed tests of the actual implementation. Include boundary/interleaving "
            "cases and observable worker termination. Missing applicable proof is unresolved; "
            "justify every inapplicable category.",
        ),
        Criterion(
            PREFIX + "sync",
            "Changed custom synchronization has a completed Loom model and independent argument.",
            "semantic",
            "Inspect the declared Loom test, tracked primitives, mapping to actual source, "
            "assertions, exploration bounds and completion. Explain invariants and memory "
            "ordering independently. Missing or incomplete evidence is unresolved. Ordinary "
            "async alone does not require Loom; justify inapplicability.",
        ),
    ]
    checks = {check.id: check for check in plan.checks}
    for index, evidence in enumerate(policy.evidence):
        check = checks.get(evidence.check_id)
        if check is None:
            raise RequirementsError(f"rust_review: unknown check_id {evidence.check_id}")
        expected = "miri" if evidence.kind == "memory" else "test"
        if cargo_test_kind(check.argv) != expected:
            raise RequirementsError(
                f"rust_review {evidence.kind} requires a direct Cargo {expected} test command"
            )
        criteria.append(
            Criterion(
                f"{PREFIX}test-{index}",
                f"Execute required {evidence.kind} test {evidence.test_name}.",
                "mechanical",
                "Require this exact nonignored test in completed successful libtest output "
                "from the current frozen candidate; a summary or zero exit code is insufficient.",
                evidence.check_id,
                evidence.test_name,
            )
        )
    return tuple(criteria)


def prepare_rust_review(
    requirements: Requirements, policy: RustReviewConfig, plan: VerificationPlan
) -> Requirements:
    """Append canonical obligations once, before requirements and the gate are frozen."""
    rust_review_context(policy)
    if not policy.enabled:
        return requirements
    _validate_requirements(requirements)
    if any(item.id.startswith(PREFIX) for item in requirements.criteria):
        raise RequirementsError(f"Criterion IDs beginning with {PREFIX} are reserved")
    return replace(requirements, criteria=(*requirements.criteria, *_criteria(policy, plan)))


def validate_rust_review(
    requirements: Requirements | None, policy: RustReviewConfig, plan: VerificationPlan
) -> None:
    """Reject missing/altered obligations or a disabled policy during local approval replay."""
    rust_review_context(policy)
    if not policy.enabled:
        if requirements and any(_MARKER in item.verification for item in requirements.criteria):
            raise RequirementsError("Rust review policy is missing or disabled in saved state")
        return
    if requirements is None:
        raise RequirementsError("Rust review requires explicit task acceptance criteria")
    _validate_requirements(requirements)
    expected = _criteria(policy, plan)
    actual = tuple(item for item in requirements.criteria if item.id.startswith(PREFIX))
    if actual != expected or requirements.criteria[-len(expected) :] != expected:
        raise RequirementsError("Rust review acceptance criteria are missing or changed")
    if len(requirements.criteria) == len(expected):
        raise RequirementsError("Rust review cannot replace explicit task acceptance criteria")


def preserve_safety_gaps(semantic: list[dict], reviews: list[dict]) -> list[dict]:
    """Triage cannot waive an independently reported missing safety argument or scope gap."""
    gaps = {
        assessment["criterion_id"]
        for review in reviews
        for assessment in review["criterion_assessments"]
        if assessment["criterion_id"] in _SAFETY_IDS and assessment["verdict"] == "unresolved"
    }
    return [
        {
            **item,
            "verdict": "unresolved",
            "rationale": "Required safety evidence remains unresolved in an independent review",
        }
        if item["criterion_id"] in gaps
        else item
        for item in semantic
    ]
