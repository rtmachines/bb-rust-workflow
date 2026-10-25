"""MVP configuration loading and deterministic model routing."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CURRENT_CONFIG_VERSION = 3
DEFAULT_CHECKS = (
    ("cargo", "fmt", "--all", "--", "--check"),
    ("cargo", "check", "--workspace", "--all-targets", "--locked"),
    ("cargo", "test", "--workspace", "--all-targets", "--locked"),
    ("cargo", "test", "--workspace", "--doc", "--locked"),
    ("cargo", "clippy", "--workspace", "--all-targets", "--locked", "--", "-D", "warnings"),
)
# Keep the former fallback for unversioned and version 1 files without checks.
LEGACY_CHECKS = (
    ("cargo", "fmt", "--check"),
    ("cargo", "check", "--all-targets"),
    ("cargo", "test", "--all-targets"),
    ("cargo", "clippy", "--all-targets", "--", "-D", "warnings"),
)


class ConfigError(ValueError):
    """Raised when configuration is malformed or requests an unsupported model."""


@dataclass(frozen=True)
class Selection:
    backend: str
    model: str
    effort: str | None
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    mcp_config: str | None = None


@dataclass
class ContextConfig:
    max_diff_chars: int = 16000
    failed_check_tail_chars: int = 2000
    skill_mode: str = "reference"


@dataclass(frozen=True)
class VerificationVariant:
    features: tuple[str, ...] = ()
    all_features: bool = False
    no_default_features: bool = False
    target: str | None = None
    toolchain: str | None = None


@dataclass(frozen=True)
class VerificationProfile:
    checks: tuple[tuple[str, ...], ...] | None = None
    variants: tuple[str, ...] = ("default",)


def _verification_profiles() -> dict[str, VerificationProfile]:
    return {
        "fast": VerificationProfile((DEFAULT_CHECKS[0], DEFAULT_CHECKS[1])),
        "focused": VerificationProfile((DEFAULT_CHECKS[1], DEFAULT_CHECKS[2])),
        "lint": VerificationProfile((DEFAULT_CHECKS[4],)),
        "gate": VerificationProfile(),
    }


@dataclass
class VerificationConfig:
    profiles: dict[str, VerificationProfile] = field(default_factory=_verification_profiles)
    variants: dict[str, VerificationVariant] = field(
        default_factory=lambda: {"default": VerificationVariant()}
    )


@dataclass(frozen=True)
class RustReviewEvidence:
    """One exact planned test used to support a Rust review obligation."""

    kind: str
    check_id: str
    test_name: str


@dataclass(frozen=True)
class RustReviewConfig:
    """Immutable opt-in policy for Rust review evidence."""

    enabled: bool = False
    version: int = 1
    required: tuple[str, ...] = ()
    evidence: tuple[RustReviewEvidence, ...] = ()


@dataclass
class Config:
    max_cycles: int = 3
    max_agent_calls: int = 20
    timeout_seconds: float = 900
    max_seconds: float = 3600
    max_retries: int = 0
    max_output_bytes: int = 16 * 1024 * 1024
    blocking_severities: tuple[str, ...] = ("critical", "high", "medium")
    prepare: list[list[str]] = field(default_factory=list)
    checks: list[list[str]] = field(
        default_factory=lambda: [list(command) for command in DEFAULT_CHECKS]
    )
    exclude: tuple[str, ...] = ()
    executables: dict[str, str] = field(
        default_factory=lambda: {"claude": "claude", "codex": "codex"}
    )
    profiles: dict[str, dict[str, Selection]] = field(default_factory=dict)
    roles: dict[str, Selection] = field(default_factory=dict)
    context: ContextConfig = field(default_factory=ContextConfig)
    config_version: int = CURRENT_CONFIG_VERSION
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    rust_review: RustReviewConfig = field(default_factory=RustReviewConfig)


ROLE_NAMES = ("implementation", "reviewer_a", "reviewer_b", "triage", "fixer")
_EFFORTS = {
    "claude": {
        "claude-haiku-5-5": ("low", "medium", "high", "xhigh", "max"),
        "claude-sonnet-5-5": ("low", "medium", "high", "xhigh", "max"),
        "claude-opus-5-5": ("low", "medium", "high", "xhigh", "max"),
        "claude-haiku-4-5-20251001": (),
        "claude-sonnet-4-6": ("low", "medium", "high", "max"),
        "claude-opus-4-6": ("low", "medium", "high", "max"),
    },
    "codex": {
        "gpt-6-luna": ("low", "medium", "high", "xhigh", "max"),
        "gpt-6.1-sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
    },
}
_TOP_KEYS = {
    "config_version",
    "context",
    "limits",
    "blocking_severities",
    "checks",
    "prepare",
    "exclude",
    "executables",
    "model_catalog",
    "profiles",
    "roles",
    "verification",
    "rust_review",
}
_RUST_REVIEW_KINDS = {"memory", "deadlocks", "cancellation", "task_lifecycle", "loom"}


def default_text() -> str:
    """Return the packaged, editable TOML template."""
    return (Path(__file__).with_name("default.toml")).read_text(encoding="utf-8")


def load_config(path: Path | None = None) -> Config:
    """Load TOML configuration, validating its shape and all selected model efforts."""
    try:
        raw = tomllib.loads(
            default_text() if path is None else Path(path).read_text(encoding="utf-8")
        )
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load configuration: {exc}") from exc
    _keys(raw, _TOP_KEYS, "root")
    config_version = _integer(raw.get("config_version", 1), "config_version", 1)
    if config_version not in (1, 2, 3):
        raise ConfigError(
            f"unsupported config_version {config_version}; supported versions are 1, 2 and "
            f"{CURRENT_CONFIG_VERSION}"
        )
    context_raw = raw.get("context", {})
    if not isinstance(context_raw, dict):
        raise ConfigError("context must be a table")
    _keys(context_raw, {"max_diff_chars", "failed_check_tail_chars", "skill_mode"}, "context")
    skill_mode = context_raw.get("skill_mode", "reference")
    if skill_mode not in ("reference", "inline"):
        raise ConfigError("context.skill_mode must be reference or inline")
    context = ContextConfig(
        max_diff_chars=_integer(
            context_raw.get("max_diff_chars", 16000), "context.max_diff_chars", 0
        ),
        failed_check_tail_chars=_integer(
            context_raw.get("failed_check_tail_chars", 2000), "context.failed_check_tail_chars", 0
        ),
        skill_mode=skill_mode,
    )
    limits = raw.get("limits", {})
    if not isinstance(limits, dict):
        raise ConfigError("limits must be a table")
    _keys(
        limits,
        {
            "max_cycles",
            "max_agent_calls",
            "timeout_seconds",
            "max_seconds",
            "max_retries",
            "max_output_bytes",
        },
        "limits",
    )
    max_cycles = _integer(limits.get("max_cycles", 3), "limits.max_cycles", 0)
    max_agent_calls = _integer(limits.get("max_agent_calls", 20), "limits.max_agent_calls", 1)
    timeout = _number(limits.get("timeout_seconds", 900), "limits.timeout_seconds", 0)
    max_seconds = _number(limits.get("max_seconds", 3600), "limits.max_seconds", 0)
    retries = _integer(limits.get("max_retries", 0), "limits.max_retries", 0)
    max_output_bytes = _integer(
        limits.get("max_output_bytes", 16 * 1024 * 1024), "limits.max_output_bytes", 1
    )
    severities = _strings(
        raw.get("blocking_severities", ["critical", "high", "medium"]), "blocking_severities"
    )
    if any(s not in {"critical", "high", "medium", "low"} for s in severities):
        raise ConfigError("blocking_severities contains an unknown severity")
    default_checks = DEFAULT_CHECKS if config_version >= 2 else LEGACY_CHECKS
    checks_raw = raw.get("checks", [list(command) for command in default_checks])
    if (
        not isinstance(checks_raw, list)
        or not checks_raw
        or any(not isinstance(c, list) or not c for c in checks_raw)
    ):
        raise ConfigError("checks must be a non-empty array of non-empty command arrays")
    checks = [list(_strings(c, "checks command")) for c in checks_raw]
    prepare_raw = raw.get("prepare", [])
    if not isinstance(prepare_raw, list) or any(
        not isinstance(command, list) or not command for command in prepare_raw
    ):
        raise ConfigError("prepare must be an array of non-empty command arrays")
    prepare = [list(_strings(command, "prepare command")) for command in prepare_raw]
    excludes = _strings(raw.get("exclude", []), "exclude")
    executables = raw.get("executables", {"claude": "claude", "codex": "codex"})
    if not isinstance(executables, dict):
        raise ConfigError("executables must be a table")
    _keys(executables, {"claude", "codex"}, "executables")
    executables = {k: _nonempty_str(v, f"executables.{k}") for k, v in executables.items()}
    if set(executables) != {"claude", "codex"}:
        raise ConfigError("executables must define claude and codex")

    catalog = {b: dict(models) for b, models in _EFFORTS.items()}
    catalog_raw = raw.get("model_catalog", {})
    if not isinstance(catalog_raw, dict):
        raise ConfigError("model_catalog must be a table")
    _keys(catalog_raw, set(catalog), "model_catalog")
    for backend, models in catalog_raw.items():
        if not isinstance(models, dict):
            raise ConfigError(f"model_catalog.{backend} must be a table")
        for model, efforts in models.items():
            catalog[backend][model] = _strings(efforts, f"model_catalog.{backend}.{model}")

    profiles_raw = raw.get("profiles", {})
    if not isinstance(profiles_raw, dict):
        raise ConfigError("profiles must be a table")
    _keys(profiles_raw, {"low", "medium", "high"}, "profiles")
    profiles: dict[str, dict[str, Selection]] = {}
    for level, table in profiles_raw.items():
        if not isinstance(table, dict):
            raise ConfigError(f"profiles.{level} must be a table")
        _keys(table, set(ROLE_NAMES), f"profiles.{level}")
        profiles[level] = {
            role: _selection(value, f"profiles.{level}.{role}", catalog)
            for role, value in table.items()
        }
        if set(profiles[level]) != set(ROLE_NAMES):
            raise ConfigError(f"profiles.{level} must define all roles: {', '.join(ROLE_NAMES)}")
    if set(profiles) != {"low", "medium", "high"}:
        raise ConfigError("profiles must define low, medium, and high")
    overrides_raw = raw.get("roles", {})
    if not isinstance(overrides_raw, dict):
        raise ConfigError("roles must be a table")
    _keys(overrides_raw, set(ROLE_NAMES), "roles")
    overrides = {
        role: _selection(value, f"roles.{role}", catalog) for role, value in overrides_raw.items()
    }
    return Config(
        config_version=config_version,
        max_cycles=max_cycles,
        max_agent_calls=max_agent_calls,
        timeout_seconds=timeout,
        max_seconds=max_seconds,
        max_retries=retries,
        max_output_bytes=max_output_bytes,
        blocking_severities=severities,
        checks=checks,
        prepare=prepare,
        exclude=excludes,
        executables=executables,
        profiles=profiles,
        roles=overrides,
        context=context,
        verification=_verification(raw.get("verification", {})),
        rust_review=parse_rust_review(raw.get("rust_review", {})),
    )


def parse_rust_review(value: object) -> RustReviewConfig:
    """Parse a Rust review policy from its TOML or JSON representation."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigError("rust_review must be a table with string field names")
    _keys(value, {"enabled", "version", "required", "evidence"}, "rust_review")
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("rust_review.enabled must be a boolean")
    version = value.get("version", 1)
    if type(version) is not int or version != 1:
        raise ConfigError("rust_review.version must be integer 1")
    required = _strings(value.get("required", []), "rust_review.required")
    if any(kind not in _RUST_REVIEW_KINDS for kind in required):
        raise ConfigError("rust_review.required contains an unknown evidence kind")
    if len(set(required)) != len(required):
        raise ConfigError("rust_review.required contains duplicate evidence kinds")
    evidence_raw = value.get("evidence", [])
    if not isinstance(evidence_raw, list):
        raise ConfigError("rust_review.evidence must be an array of tables")
    evidence: list[RustReviewEvidence] = []
    seen: set[RustReviewEvidence] = set()
    for index, entry in enumerate(evidence_raw):
        where = f"rust_review.evidence[{index}]"
        if not isinstance(entry, dict) or any(not isinstance(key, str) for key in entry):
            raise ConfigError(f"{where} must be a table with string field names")
        _keys(entry, {"kind", "check_id", "test_name"}, where)
        item = RustReviewEvidence(
            kind=_nonempty_str(entry.get("kind"), f"{where}.kind"),
            check_id=_nonempty_str(entry.get("check_id"), f"{where}.check_id"),
            test_name=_nonempty_str(entry.get("test_name"), f"{where}.test_name"),
        )
        if item.kind not in required:
            raise ConfigError(f"{where}.kind must be declared in rust_review.required")
        if item in seen:
            raise ConfigError(f"{where} duplicates an evidence declaration")
        seen.add(item)
        evidence.append(item)
    if not enabled and (required or evidence):
        raise ConfigError("disabled rust_review cannot declare required kinds or evidence")
    missing = set(required) - {item.kind for item in evidence}
    if enabled and missing:
        raise ConfigError(f"rust_review is missing evidence for: {', '.join(sorted(missing))}")
    return RustReviewConfig(enabled, version, required, tuple(evidence))


def route(config: Config, task: str, complexity: str = "auto") -> tuple[str, dict[str, Selection]]:
    """Resolve complexity and return role selections; auto is a heuristic, not a guarantee."""
    level = _complexity(task) if complexity == "auto" else complexity
    if level not in config.profiles:
        raise ConfigError("complexity must be auto, low, medium, or high")
    selections = dict(config.profiles[level])
    selections.update(config.roles)
    return level, selections


def _complexity(task: str) -> str:
    text = task.casefold()
    high = r"\b(unsafe|concurren\w*|thread\w*|security|auth\w*|credential\w*|ffi|f\s*fi|memory\s+safety|race\s+condition|niebezpiecz\w*|współbież\w*|bezpieczeństw\w*|uwierzyteln\w*)\b"
    if re.search(high, text):
        return "high"
    low = r"\b(docs?|documentation|readme|typo|rename|formatting|spelling|literówk\w*|dokumentac\w*|zmień\s+nazw\w*|zmiana\s+nazw\w*)\b"
    implementation = r"\b(implement\w*|build\w*|add\w*|create\w*|refactor\w*|dodaj\w*|napisz\w*|zaimplement\w*)\b"
    if re.search(low, text) and not re.search(implementation, text):
        return "low"
    return "medium"


def _selection(value: Any, where: str, catalog: dict[str, dict[str, tuple[str, ...]]]) -> Selection:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table")
    _keys(value, {"backend", "model", "effort", "skills", "tools", "mcp_config"}, where)
    backend = _nonempty_str(value.get("backend"), f"{where}.backend")
    model = _nonempty_str(value.get("model"), f"{where}.model")
    if backend not in catalog or model not in catalog[backend]:
        raise ConfigError(f"{where} uses unsupported backend/model {backend}/{model}")
    effort = value.get("effort")
    if effort is not None and not isinstance(effort, str):
        raise ConfigError(f"{where}.effort must be a string or null")
    supported_efforts = catalog[backend][model]
    if (supported_efforts and effort not in supported_efforts) or (
        not supported_efforts and effort is not None
    ):
        raise ConfigError(f"{where} uses unsupported effort {effort!r} for {backend}/{model}")
    skills = _strings(value.get("skills", []), f"{where}.skills")
    tools = _strings(value.get("tools", []), f"{where}.tools")
    if any(tool != "LSP" and not re.fullmatch(r"mcp__[\w-]+__[\w-]+", tool) for tool in tools):
        raise ConfigError(f"{where}.tools must name LSP or exact MCP tools (no wildcards)")
    mcp = value.get("mcp_config")
    if backend == "codex" and (tools or mcp is not None):
        raise ConfigError(
            f"{where}: Codex does not support configured tools or mcp_config in this MVP"
        )
    if mcp is not None:
        mcp = _nonempty_str(mcp, f"{where}.mcp_config")
    if any(tool.startswith("mcp__") for tool in tools) and mcp is None:
        raise ConfigError(f"{where}: MCP tools require an explicit mcp_config")
    return Selection(backend, model, effort, skills, tools, mcp)


def _keys(value: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"unknown {where} field(s): {', '.join(sorted(unknown))}")


def _nonempty_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string")
    return value


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ConfigError(f"{where} must be an array of strings")
    return tuple(value)


def _integer(value: Any, where: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{where} must be an integer >= {minimum}")
    return value


def _number(value: Any, where: str, minimum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) < float("inf")
    ):
        raise ConfigError(f"{where} must be a finite positive number")
    return float(value)


def _verification(value: Any) -> VerificationConfig:
    if not isinstance(value, dict):
        raise ConfigError("verification must be a table")
    _keys(value, {"profiles", "variants"}, "verification")
    result = VerificationConfig()
    variants = value.get("variants", {})
    if not isinstance(variants, dict):
        raise ConfigError("verification.variants must be a table")
    for name, options in variants.items():
        where = f"verification.variants.{name}"
        _nonempty_str(name, where)
        if not isinstance(options, dict):
            raise ConfigError(f"{where} must be a table")
        _keys(
            options,
            {"features", "all_features", "no_default_features", "target", "toolchain"},
            where,
        )
        features = _strings(options.get("features", []), f"{where}.features")
        for feature in features:
            _nonempty_str(feature, f"{where}.features")
        for flag in ("all_features", "no_default_features"):
            if flag in options and type(options[flag]) is not bool:
                raise ConfigError(f"{where}.{flag} must be a boolean")
        if features and options.get("all_features", False):
            raise ConfigError(f"{where}: features conflict with all_features")
        result.variants[name] = VerificationVariant(
            features=features,
            all_features=options.get("all_features", False),
            no_default_features=options.get("no_default_features", False),
            target=_nonempty_str(options["target"], f"{where}.target")
            if "target" in options
            else None,
            toolchain=_nonempty_str(options["toolchain"], f"{where}.toolchain")
            if "toolchain" in options
            else None,
        )
    profiles = value.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError("verification.profiles must be a table")
    _keys(profiles, set(result.profiles), "verification.profiles")
    for name, options in profiles.items():
        where = f"verification.profiles.{name}"
        if not isinstance(options, dict):
            raise ConfigError(f"{where} must be a table")
        _keys(options, {"variants"} if name == "gate" else {"checks", "variants"}, where)
        names = _strings(options.get("variants", ["default"]), f"{where}.variants")
        if (
            not names
            or len(set(names)) != len(names)
            or any(n not in result.variants for n in names)
        ):
            raise ConfigError(
                f"{where}.variants must be non-empty, unique and reference defined variants"
            )
        checks = result.profiles[name].checks
        if "checks" in options:
            raw = options["checks"]
            if (
                not isinstance(raw, list)
                or not raw
                or any(not isinstance(c, list) or not c for c in raw)
            ):
                raise ConfigError(f"{where}.checks must be a non-empty array of command arrays")
            checks = tuple(_strings(c, f"{where}.checks command") for c in raw)
        result.profiles[name] = VerificationProfile(checks, names)
    return result
