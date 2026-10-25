"""Frozen acceptance requirements and conservative mechanical proof from audited checks."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .verification import PlannedCheck, VerificationPlan


class RequirementsError(ValueError):
    """Requirements cannot be loaded or bound to their full gate plan."""


@dataclass(frozen=True)
class Criterion:
    id: str
    description: str
    kind: str
    verification: str
    check_id: str | None = None
    test_name: str | None = None


@dataclass(frozen=True)
class Requirements:
    requirements_version: int = 1
    criteria: tuple[Criterion, ...] = ()


@dataclass(frozen=True)
class BoundRequirements:
    requirements: Requirements
    gate_plan: VerificationPlan
    requirements_id: str
    verification_plan_id: str


def _text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _validate_requirements(requirements: Requirements) -> None:
    if type(requirements) is not Requirements:
        raise RequirementsError("Expected Requirements")
    if type(requirements.requirements_version) is not int or requirements.requirements_version != 1:
        raise RequirementsError("Unsupported requirements_version")
    if type(requirements.criteria) is not tuple or not requirements.criteria:
        raise RequirementsError("criteria must be a nonempty immutable tuple")
    ids = set()
    for criterion in requirements.criteria:
        if type(criterion) is not Criterion:
            raise RequirementsError("Expected Criterion")
        if not _text(criterion.id) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", criterion.id):
            raise RequirementsError("Invalid criterion ID")
        if criterion.id in ids:
            raise RequirementsError("Duplicate criterion ID")
        ids.add(criterion.id)
        if not _text(criterion.description) or not _text(criterion.verification):
            raise RequirementsError("description and verification must be nonblank strings")
        if not _text(criterion.kind) or criterion.kind not in {"mechanical", "semantic"}:
            raise RequirementsError("Unsupported criterion kind")
        if criterion.kind == "semantic":
            if criterion.check_id is not None or criterion.test_name is not None:
                raise RequirementsError("Semantic criteria forbid check_id and test_name")
        elif not _text(criterion.check_id):
            raise RequirementsError("Mechanical criteria require check_id")
        if criterion.test_name is not None and not _text(criterion.test_name):
            raise RequirementsError("test_name must be a nonblank string")


def load_requirements(path: Path) -> Requirements:
    """Read a strict versioned TOML contract once, without retaining its mutable source."""
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RequirementsError(f"Cannot load requirements: {exc}") from exc
    if set(data) != {"requirements_version", "criteria"} or type(data["criteria"]) is not list:
        raise RequirementsError("Expected requirements_version and criteria array only")
    criteria = []
    required = {"id", "description", "kind", "verification"}
    for item in data["criteria"]:
        if (
            type(item) is not dict
            or not required <= set(item)
            or set(item) - (required | {"check_id", "test_name"})
        ):
            raise RequirementsError("Missing or unexpected criterion fields")
        criteria.append(Criterion(**item))
    requirements = Requirements(data["requirements_version"], tuple(criteria))
    _validate_requirements(requirements)
    return requirements


def _hash(value: Requirements | VerificationPlan) -> str:
    canonical = json.dumps(asdict(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def requirements_id(requirements: Requirements) -> str:
    """Identify every parsed requirement field in its declared order."""
    return _hash(requirements)


def verification_plan_id(plan: VerificationPlan) -> str:
    """Identify the entire resolved plan, including ordered checks and commands."""
    return _hash(plan)


def _cargo_test(argv: tuple[str, ...]) -> bool:
    """Classify direct Cargo tests; unknown Cargo prefix grammar cannot become custom proof."""
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable not in {"cargo", "cargo.exe"}:
        return False
    position = 1
    if len(argv) > position and argv[position].startswith("+"):
        if len(argv[position]) == 1:
            raise RequirementsError("Cargo toolchain must not be empty")
        position += 1
    flags = {"--verbose", "--quiet", "--locked", "--offline", "--frozen"}
    valued = {"--color", "--config", "-C", "-Z"}
    while position < len(argv):
        arg = argv[position]
        if not arg.startswith(("-", "+")):
            return arg in {"test", "t"}
        if arg in flags or re.fullmatch(r"-[vq]+", arg):
            position += 1
            continue
        option, separator, value = arg.partition("=")
        if option in {"--color", "--config"} and separator:
            pass
        elif arg in valued:
            position += 1
            value = argv[position] if position < len(argv) else ""
            option = arg
        elif arg.startswith(("-C", "-Z")) and len(arg) > 2:
            option, value = arg[:2], arg[2:]
        else:
            raise RequirementsError(f"Unsupported Cargo global argument: {arg}")
        if (
            not value
            or value.startswith("-")
            or (option == "--color" and value not in {"auto", "always", "never"})
        ):
            raise RequirementsError(f"Invalid or missing Cargo global argument value: {option}")
        position += 1
    raise RequirementsError("Cargo acceptance check requires an unambiguous subcommand")


def bind_requirements(requirements: Requirements, gate_plan: VerificationPlan) -> BoundRequirements:
    """Reject invalid references before invoking any provider; never narrow the gate."""
    _validate_requirements(requirements)
    if (
        gate_plan.profile != "gate"
        or gate_plan.package is not None
        or gate_plan.test_filter is not None
    ):
        raise RequirementsError("Requirements must bind to the full gate plan")
    if (
        type(gate_plan.checks) is not tuple
        or type(gate_plan.variants) is not tuple
        or not gate_plan.checks
        or not gate_plan.variants
        or any(not _text(variant) for variant in gate_plan.variants)
        or len(set(gate_plan.variants)) != len(gate_plan.variants)
    ):
        raise RequirementsError("Gate checks and variants must be nonempty immutable tuples")
    checks = {}
    for check in gate_plan.checks:
        if (
            type(check) is not PlannedCheck
            or not _text(check.id)
            or check.id in checks
            or check.profile != "gate"
            or check.variant not in gate_plan.variants
            or type(check.argv) is not tuple
            or not check.argv
            or any(not _text(arg) for arg in check.argv)
        ):
            raise RequirementsError("Invalid or duplicate gate check")
        checks[check.id] = check
    for criterion in requirements.criteria:
        if criterion.kind != "mechanical":
            continue
        check = checks.get(criterion.check_id)
        if check is None:
            raise RequirementsError(f"Unknown check_id: {criterion.check_id}")
        is_test = _cargo_test(check.argv)
        if criterion.test_name is not None and not is_test:
            raise RequirementsError("test_name requires a direct Cargo test command")
    return BoundRequirements(
        requirements, gate_plan, requirements_id(requirements), verification_plan_id(gate_plan)
    )


_SUMMARY = re.compile(
    r"^test result: ok\. (\d+) passed; 0 failed; \d+ ignored; \d+ measured; "
    r"\d+ filtered out(?:; finished in .+)?$"
)
_TEST = re.compile(r"^test (.+) \.\.\. ok$")


def _test_proof(
    check: PlannedCheck, criterion: Criterion, record: dict, root: Path
) -> tuple[str, str]:
    if "--no-run" in check.argv or "--list" in check.argv:
        return "unmet", "Cargo command did not execute tests"
    try:
        directory = (root / record["artifacts"]).resolve()
        if not directory.is_relative_to(root.resolve()):
            raise ValueError("Log path escapes artifact root")
        outputs = []
        for name in ("stdout.txt", "stderr.txt"):
            path = (directory / name).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("Log path escapes artifact root")
            outputs.append(path.read_text(encoding="utf-8"))
    except (KeyError, OSError, UnicodeError, TypeError, ValueError):
        return "unresolved", "Full audited stdout/stderr unavailable"
    completed = passed = named = 0
    for output in outputs:
        successful_names = set()
        for line in output.splitlines():
            match = _TEST.fullmatch(line)
            if match:
                successful_names.add(match[1].removesuffix(" - should panic"))
            summary = _SUMMARY.fullmatch(line)
            if summary:
                completed += 1
                count = int(summary[1])
                passed += count
                if count and criterion.test_name in successful_names:
                    named += 1
                successful_names.clear()
            elif line.startswith("test result:") or re.fullmatch(r"running \d+ tests?", line):
                successful_names.clear()
    if not completed:
        return "unresolved", "No completed successful libtest summary in full logs"
    if not passed:
        return "unmet", "Completed libtest output contains no passed nonignored tests"
    if criterion.test_name is not None and not named:
        return "unmet", "Exact named test did not pass in completed successful libtest output"
    return "satisfied", "Completed successful libtest output proves the required test execution"


def evaluate_mechanical(
    bound: BoundRequirements, check_results: list[dict], binding: dict, artifact_root: Path
) -> list[dict]:
    """Prove each mechanical criterion only from one matching current validation record."""
    binding_valid = (
        type(binding) is dict
        and set(binding) == {"candidate_id", "requirements_id", "verification_plan_id"}
        and all(_text(value) for value in binding.values())
        and binding["requirements_id"]
        == bound.requirements_id
        == requirements_id(bound.requirements)
        and binding["verification_plan_id"]
        == bound.verification_plan_id
        == verification_plan_id(bound.gate_plan)
    )
    checks = {check.id: check for check in bound.gate_plan.checks}
    items = []
    for criterion in bound.requirements.criteria:
        if criterion.kind != "mechanical":
            continue
        check = checks[criterion.check_id]
        matches = [record for record in check_results if record.get("id") == check.id]
        verdict, evidence = "unresolved", "Missing, duplicate, stale or mismatched validation proof"
        if binding_valid and len(matches) == 1:
            record = matches[0]
            matches_plan = (
                record.get("kind") == "validation"
                and record.get("profile") == check.profile
                and record.get("variant") == check.variant
                and record.get("command") == list(check.argv)
                and record.get("snapshot_id") == binding["candidate_id"]
                and record.get("result_snapshot_id") == binding["candidate_id"]
                and record.get("source_unchanged") is True
            )
            if matches_plan:
                if (
                    record.get("passed") is False
                    or (type(record.get("returncode")) is int and record["returncode"] != 0)
                    or record.get("timed_out") is True
                    or record.get("output_limit_exceeded") is True
                    or record.get("error")
                ):
                    verdict, evidence = (
                        "unmet",
                        "Validation command failed or exceeded execution limits",
                    )
                elif (
                    record.get("passed") is True
                    and type(record.get("returncode")) is int
                    and record["returncode"] == 0
                    and record.get("timed_out") is False
                    and record.get("output_limit_exceeded") is False
                ):
                    if _cargo_test(check.argv):
                        verdict, evidence = _test_proof(check, criterion, record, artifact_root)
                    else:
                        verdict, evidence = (
                            "satisfied",
                            "Bound validation command completed successfully",
                        )
        items.append(
            dict(
                criterion_id=criterion.id, verdict=verdict, evidence=evidence, binding=dict(binding)
            )
        )
    return items
