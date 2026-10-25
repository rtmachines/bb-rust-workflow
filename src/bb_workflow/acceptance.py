"""Final local acceptance contract, reconstructed from the frozen audit evidence.

Digests detect inconsistent artifacts; coordinated rewriting of all local audit
files is outside this contract's trust boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict
from pathlib import Path

from .protocol import merge_findings, resolve_triage, validate_response
from .requirements import (
    Criterion,
    Requirements,
    bind_requirements,
    evaluate_mechanical,
    verification_plan_id,
)
from .verification import PlannedCheck, VerificationPlan
from .workspace import WorkspaceError, _directory, _read_json, _reparse_point

REPORT_CONTRACT = 1


def canonical_digest(value: object) -> str:
    """Hash JSON deterministically, retaining array order and scalar types."""
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise WorkspaceError(f"final acceptance: {message}")


def _text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _plan(data: object) -> VerificationPlan:
    _require(
        type(data) is dict
        and set(data) == {"profile", "variants", "package", "test_filter", "checks"},
        "verification_plan.json must contain the complete frozen gate plan",
    )
    _require(
        data["profile"] == "gate" and data["package"] is None and data["test_filter"] is None,
        "verification plan must be the full gate",
    )
    variants, checks = data["variants"], data["checks"]
    _require(
        type(variants) is list
        and bool(variants)
        and all(_text(v) for v in variants)
        and len(set(variants)) == len(variants),
        "invalid planned variants",
    )
    _require(type(checks) is list and bool(checks), "planned checks must be a nonempty array")
    parsed = []
    for check in checks:
        _require(
            type(check) is dict and set(check) == {"id", "profile", "variant", "argv"},
            "invalid planned check fields",
        )
        _require(
            _text(check["id"])
            and check["profile"] == "gate"
            and check["variant"] in variants
            and type(check["argv"]) is list
            and bool(check["argv"])
            and all(_text(arg) for arg in check["argv"]),
            "invalid planned check identity or command",
        )
        parsed.append(
            PlannedCheck(check["id"], check["profile"], check["variant"], tuple(check["argv"]))
        )
    _require(len({c.id for c in parsed}) == len(parsed), "duplicate planned check IDs")
    return VerificationPlan("gate", tuple(variants), None, None, tuple(parsed))


def _requirements(data: object) -> Requirements:
    _require(
        type(data) is dict
        and set(data) == {"requirements_version", "criteria"}
        and type(data["criteria"]) is list,
        "invalid frozen requirements.json",
    )
    fields = {"id", "description", "kind", "verification", "check_id", "test_name"}
    _require(
        all(type(c) is dict and set(c) == fields for c in data["criteria"]),
        "invalid frozen criterion fields",
    )
    return Requirements(
        data["requirements_version"], tuple(Criterion(**c) for c in data["criteria"])
    )


def _provenance(root: Path, state: dict, role: str, response: dict, cycle: int) -> dict:
    agents = state["agents"]
    _require(
        type(agents) is list and all(type(a) is dict for a in agents),
        "agents must be an array of objects",
    )
    matches = [
        a
        for a in agents
        if a.get("role") == role
        and type(a.get("cycle")) is int
        and a["cycle"] == cycle
        and a.get("status") == "completed"
    ]
    _require(len(matches) == 1, f"missing or duplicate completed {role} provenance for final cycle")
    agent = matches[0]
    _require(
        _canonical(agent.get("binding")) == _canonical(response.get("binding")),
        f"{role} provenance has stale input binding",
    )
    relative = agent["artifacts"]
    _require(_text(relative) and not Path(relative).is_absolute(), f"invalid {role} artifact path")
    path = root / relative / "validated_response.json"
    _require(path.resolve().is_relative_to(root.resolve()), f"{role} artifact path escapes run")
    durable = _read_json(path)
    _require(
        _canonical(durable) == _canonical(response),
        f"{role} response differs from durable validated response",
    )
    return {"role": role, "artifacts": relative, "cycle": cycle}


def _construct(root: Path, state: dict) -> dict:
    acceptance = state["acceptance"]
    _require(type(acceptance) is dict, "acceptance metadata must be an object")
    mode = acceptance["mode"]
    _require(mode in {"criteria", "legacy"}, "unsupported requirements mode")
    _require(type(state["cycles"]) is int and state["cycles"] >= 0, "invalid final cycle")
    cycle = state["cycles"]
    candidate = state["candidate_id"]
    _require(_text(candidate), "missing candidate_id")
    plan = _plan(_read_json(root / "verification_plan.json"))
    plan_id = verification_plan_id(plan)
    _require(
        acceptance["verification_plan_id"] == plan_id, "frozen verification plan hash mismatch"
    )
    bound = None
    if mode == "criteria":
        _require(
            acceptance["compatibility_mode"] is False, "criteria cannot use legacy compatibility"
        )
        bound = bind_requirements(_requirements(_read_json(root / "requirements.json")), plan)
        _require(
            acceptance["requirements_id"] == bound.requirements_id,
            "frozen requirements hash mismatch",
        )
    else:
        _require(
            acceptance["compatibility_mode"] is True and acceptance["requirements_id"] is None,
            "legacy requirements must be explicitly absent",
        )
        _require(
            type(state["config"]["config_version"]) is int
            and state["config"]["config_version"] in {1, 2},
            "configuration requires criteria",
        )
        _require(
            not (root / "requirements.json").exists() and acceptance["rounds"] == [],
            "legacy mode has contradictory criteria evidence",
        )
    binding = {
        "candidate_id": candidate,
        "requirements_id": bound.requirements_id if bound else None,
        "verification_plan_id": plan_id,
    }
    phase = f"candidate-{cycle}"
    _require(
        type(state["checks"]) is list and all(type(c) is dict for c in state["checks"]),
        "checks must be an array of objects",
    )
    results = [c for c in state["checks"] if c.get("phase") == phase]
    _require(len(results) == len(plan.checks), "final gate is incomplete or contains extra checks")
    for check, record in zip(plan.checks, results):
        _require(
            record.get("id") == check.id
            and record.get("profile") == check.profile
            and record.get("variant") == check.variant
            and record.get("command") == list(check.argv)
            and record.get("kind") == "validation",
            "gate evidence does not match exact planned checks",
        )
        _require(
            record.get("snapshot_id") == candidate
            and record.get("result_snapshot_id") == candidate
            and record.get("source_unchanged") is True,
            "gate evidence has stale candidate binding",
        )
        _require(
            record.get("passed") is True
            and type(record.get("returncode")) is int
            and record["returncode"] == 0
            and record.get("timed_out") is False
            and record.get("output_limit_exceeded") is False
            and not record.get("error"),
            "final gate did not complete successfully",
        )
        relative = record["artifacts"]
        _require(_text(relative) and not Path(relative).is_absolute(), "invalid gate log path")
        for stream in ("stdout.txt", "stderr.txt"):
            path = root / relative / stream
            _require(
                path.resolve().is_relative_to(root.resolve()) and path.is_file(),
                "full gate logs unavailable",
            )
    _require(
        type(state["reviews"]) is list and bool(state["reviews"]),
        "missing independent final reviews",
    )
    review = state["reviews"][-1]
    _require(
        type(review) is dict
        and type(review.get("cycle")) is int
        and review["cycle"] == cycle
        and review.get("snapshot_id") == candidate,
        "stale final review cycle or candidate",
    )
    reviews = review["reviews"]
    _require(
        type(reviews) is list and len(reviews) == 2, "two independent reviewer responses required"
    )
    criteria = bound.requirements.criteria if bound else ()
    provenance = []
    for role, response in zip(("reviewer_a", "reviewer_b"), reviews):
        validate_response(
            response,
            role,
            root / "workspace",
            root / "baseline",
            version=2 if bound else 1,
            expected_binding=binding if bound else None,
            criteria=criteria,
        )
        _require(response["status"] == "completed", f"{role} did not complete review")
        provenance.append(_provenance(root, state, role, response, cycle))
    _require(
        provenance[0]["artifacts"] != provenance[1]["artifacts"],
        "reviewer evidence must be independent",
    )
    findings = merge_findings(reviews)
    _require(
        _canonical(review["findings"]) == _canonical(findings),
        "merged findings differ from reviewer evidence",
    )
    semantic = []
    disputed = []
    for criterion in criteria:
        if criterion.kind != "semantic":
            continue
        assessments = [
            next(a for a in r["criterion_assessments"] if a["criterion_id"] == criterion.id)
            for r in reviews
        ]
        satisfied = all(a["verdict"] == "satisfied" for a in assessments)
        if not satisfied:
            disputed.append(criterion.id)
        semantic.append(
            {
                "criterion_id": criterion.id,
                "verdict": "satisfied" if satisfied else "unresolved",
                "evidence": assessments,
                "binding": binding,
            }
        )
    triage = review.get("triage")
    confirmed, unresolved = [], []
    if findings or disputed:
        _require(type(triage) is dict, "findings or disputed criteria require triage")
        validate_response(
            triage,
            "triage",
            root / "workspace",
            root / "baseline",
            version=2 if bound else 1,
            expected_binding=binding if bound else None,
            criteria=criteria,
            disputed_criteria=tuple(disputed),
        )
        _require(triage["status"] == "completed", "triage did not complete")
        provenance.append(_provenance(root, state, "triage", triage, cycle))
        confirmed, unresolved = resolve_triage(findings, triage)
        decisions = {d["criterion_id"]: d for d in triage.get("criterion_decisions", [])}
        semantic = [{**s, **decisions.get(s["criterion_id"], {})} for s in semantic]
    else:
        _require(triage is None, "unexpected triage without findings or disputed criteria")
    _require(
        not unresolved
        and _canonical(review["confirmed"]) == _canonical(confirmed)
        and review["unresolved"] == [],
        "unresolved or inconsistent final findings",
    )
    blocking = state["config"]["blocking_severities"]
    _require(
        type(blocking) is list
        and all(s in {"critical", "high", "medium", "low"} for s in blocking),
        "invalid blocking severities",
    )
    _require(
        not any(f["severity"] in blocking for f in confirmed), "confirmed blocking finding remains"
    )
    final_criteria = None
    if bound:
        mechanical = evaluate_mechanical(bound, results, binding, root)
        _require(
            type(acceptance["rounds"]) is list and bool(acceptance["rounds"]),
            "missing criterion rounds",
        )
        final_round = acceptance["rounds"][-1]
        _require(
            type(final_round) is dict
            and type(final_round.get("cycle")) is int
            and final_round["cycle"] == cycle
            and final_round.get("binding") == binding,
            "stale criterion round",
        )
        for key, expected in (
            ("mechanical", mechanical),
            ("semantic", semantic),
            ("reviews", reviews),
        ):
            _require(
                _canonical(final_round[key]) == _canonical(expected),
                f"{key} evidence differs from final audit",
            )
        _require(
            _canonical(final_round.get("triage")) == _canonical(triage),
            "criterion triage differs from final audit",
        )
        derived = {c["criterion_id"]: c for c in mechanical + semantic}
        _require(
            all(c["verdict"] == "satisfied" for c in derived.values()),
            "every criterion must be satisfied",
        )
        final_criteria = [{**asdict(c), **derived[c.id]} for c in criteria]
    return {
        "version": REPORT_CONTRACT,
        "mode": mode,
        "cycle": cycle,
        "binding": binding,
        "gate": {
            "profile": plan.profile,
            "variants": list(plan.variants),
            "package": None,
            "test_filter": None,
            "planned_checks": [asdict(c) for c in plan.checks],
            "results": results,
        },
        "criteria": final_criteria,
        "review": review,
        "provenance": provenance,
    }


def build_acceptance(root: Path, state: dict) -> dict:
    """Construct final approval only from a complete successful current audit."""
    try:
        # Normalize dataclass tuples and in-memory configuration exactly as persisted JSON.
        return json.loads(_canonical(_construct(root, json.loads(_canonical(state)))))
    except WorkspaceError:
        raise
    except (KeyError, TypeError, ValueError, OSError, AttributeError, StopIteration) as exc:
        raise WorkspaceError(
            f"final acceptance: malformed or unavailable audit evidence: {exc}"
        ) from exc


def _has_new_provenance(root: Path, state: dict) -> bool:
    """Recognize surviving stage-5 evidence without recursive artifact traversal."""
    for collection, fields in (("agents", {"cycle", "response"}), ("reviews", {"cycle"})):
        records = state.get(collection)
        if type(records) is list and any(
            type(record) is dict and fields.intersection(record) for record in records
        ):
            return True
    directory = root / "agents"
    try:
        try:
            directory.lstat()
        except FileNotFoundError:
            return False
        _directory(directory)
        # Stream only immediate invocation directories. Never expand a recursive glob
        # or follow a symlink/junction into another run or outside the artifact root.
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                _require(
                    not _reparse_point(info) and not stat.S_ISLNK(info.st_mode),
                    "agent provenance cannot traverse symlinks or reparse points",
                )
                if not stat.S_ISDIR(info.st_mode):
                    continue
                invocation = Path(entry.path)
                _directory(invocation)
                try:
                    (invocation / "validated_response.json").lstat()
                except FileNotFoundError:
                    continue
                return True
    except OSError as exc:
        raise WorkspaceError(f"final acceptance: cannot inspect agent provenance: {exc}") from exc
    return False


def validate_acceptance(root: Path, state: dict) -> None:
    """Validate new contracts under apply's existing lock; historical v1 keeps its contract."""
    version = state.get("version", 1)
    indicators = (
        any(key in state for key in ("report_contract", "acceptance_report"))
        or (root / "acceptance.json").exists()
        or (root / "acceptance.json").is_symlink()
        or _has_new_provenance(root, state)
    )
    _require(type(version) is int and version in {1, 2}, "unknown state version")
    if version == 1 and not indicators:
        return
    _require(
        version == 2
        and type(state.get("report_contract")) is int
        and state["report_contract"] == REPORT_CONTRACT,
        "missing or contradictory report contract marker",
    )
    try:
        metadata = state["acceptance_report"]
        _require(
            type(metadata) is dict and set(metadata) == {"version", "file", "digest", "binding"},
            "missing or malformed acceptance report metadata",
        )
        _require(
            type(metadata["version"]) is int
            and metadata["version"] == REPORT_CONTRACT
            and metadata["file"] == "acceptance.json",
            "unsupported acceptance report version or path",
        )
        report = _read_json(root / "acceptance.json")
        _require(
            type(report) is dict
            and type(report.get("version")) is int
            and report["version"] == REPORT_CONTRACT,
            "malformed acceptance.json version",
        )
        _require(metadata["digest"] == canonical_digest(report), "acceptance.json digest mismatch")
        expected = build_acceptance(root, state)
        _require(
            _canonical(report) == _canonical(expected),
            "acceptance.json differs from current frozen audit evidence",
        )
        _require(metadata["binding"] == expected["binding"], "report metadata binding mismatch")
    except WorkspaceError:
        raise
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise WorkspaceError(f"final acceptance: malformed or unavailable report: {exc}") from exc
