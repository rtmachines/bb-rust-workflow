# Rust PR Review Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate both reviewers with rust-pr-review and prevent approval without required evidence.

**Architecture:** Opt-in policy expands existing immutable acceptance criteria and adds a packaged reviewer adapter. Mechanical proof stays with the orchestrator and is rechecked at apply; stage order stays unchanged.

**Tech Stack:** Python 3.11 stdlib, unittest fixture providers, Ruff/reviewdog.

**Spec:** `docs/superpowers/specs/2026-10-25-rust-pr-review-adapter-design.md`

## Global Constraints

- Python 3.11+, stdlib runtime, fixture providers only.
- Preserve stage order, independent snapshots, frozen-source checks and explicit apply.
- No implicit installation, provider calls, model switches, or full upstream knowledge ingestion.
- Existing requirement/response versions remain unchanged when using ordinary generated criteria.

## Review Focus

- Missing or zero required tests despite successful process exit must block success.
- Favorable triage must not override mechanical proof or conceal an unplanned safety obligation.
- Apply must reject missing generated criteria, stale proof and unavailable logs.
- Disabled policy and historical saved states must retain existing behavior.
- Both reviewers receive identical criteria/evidence without each other's results.

### Task 1: Configuration and immutable policy

**Files:** `src/bb_workflow/config.py`, `src/bb_workflow/default.toml`, `tests/test_config.py`

**Interfaces:** Produce frozen `RustReviewEvidence(kind, check_id, test_name)` and
`RustReviewConfig(enabled=False, version=1, required=(), evidence=())`, plus strict
`parse_rust_review(value)` accepting TOML/JSON arrays. Add `Config.rust_review`.

- [x] Write failing tests for valid policy round-trip and rejection of missing/duplicate/unknown evidence.
- [x] Run focused configuration tests and verify expected failures.
- [x] Implement policy parsing without changing role/model routing.
- [x] Run focused tests and format/lint the touched files.

### Task 2: Adapter, generated criteria and targeted Miri proof

**Files:** create `src/bb_workflow/rust_review.py`, `src/bb_workflow/prompts/rust_pr_review.md`, `tests/test_rust_review.py`; modify `requirements.py`, `tests/test_requirements.py`.

**Interfaces:** `prepare_rust_review(requirements, policy, plan)` returns expanded
Requirements; `validate_rust_review(requirements, policy, plan)` verifies persisted
canonical criteria; `rust_review_context(policy)` supplies the explicit scope.

- [x] Write failing tests for canonical expansion, reserved IDs, command mismatch and Miri named proof.
- [x] Implement expansion, frozen policy identity, and direct Miri libtest recognition.
- [x] Add provider-neutral adapter preserving JSON, evidence recovery, role boundaries and source attribution.
- [x] Run focused policy and requirements tests.

### Task 3: Workflow and apply integration

**Files:** `engine.py`, `acceptance.py`, `cli.py`, `tests/test_rust_review.py`, `tests/test_acceptance.py`, `tests/test_cli.py`.

- [x] Write failing fixture integration tests for missing/zero/stale evidence, unresolved arguments, successful proof and unchanged stage order.
- [x] Integrate expansion before freezing, adapter for both reviewers/triage, plan preview, and apply replay validation.
- [x] Verify favorable model verdicts cannot replace required tests; zero findings never waives proof.
- [x] Verify old states without policy and ordinary requirement hashes remain compatible.

### Task 4: Documentation and final verification

**Files:** `README.md`, `docs/architecture.md`, `examples/rust-review/`.

- [x] Document a usable opt-in policy, exact check IDs/test names, new-risk restart rule and supported evidence limitations.
- [x] Run `PYTHONPATH=src python3 -m unittest discover -s tests -q`.
- [x] Run raw Ruff diagnostics through local reviewdog and `ruff format --check src tests examples`.
- [x] Request independent review using graph-first scope and source verification; fix actionable findings and rerun affected checks.
- [x] Commit the verified changes on `codex/rust-pr-review-adapter` and leave the managed worktree available.

## Verification result

Final full suite: 456 tests, 2 skipped, 96.908 seconds, successful. Raw Ruff through
local reviewdog, Ruff formatting and diff whitespace checks passed. Provider calls
and test output used fixtures; no paid sessions or real Miri/Loom runs.

Independent review found one late-validation issue for invalid programmatic legacy
policies. A failing regression reproduced premature execution; unconditional policy
validation fixed it. The reviewer verified the fix with no remaining actionable
findings, and independently checked all eight safety-criterion/reviewer combinations
against favorable triage and acceptance replay.
