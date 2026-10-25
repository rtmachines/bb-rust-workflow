# Stage 5 reporting and apply integration implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Finish stage 5 with an auditable final report and candidate-bound apply validation.

**Architecture:** A versioned final acceptance artifact binds the frozen inputs, completed gate and independent review decisions to the successful candidate. A small reporting module renders existing audit evidence; apply validates the final contract inside its existing lock before backup or mutation. Historical runs retain their prior contract.

**Tech Stack:** Python 3.11+, stdlib runtime, unittest fixture providers, offline Cargo fixtures, Ruff and local reviewdog.

**Spec:** `docs/rustflow-development-plan.md`, stage 5; preserved guarantees in stages 1–4 and `docs/architecture.md`.

## Global Constraints

- Keep stage order, independent reviews, frozen-source checks and explicit apply guarantees.
- Keep full audit logs on disk; distinguish prompt size from measured provider tokens and unknown usage from zero.
- Use fixture providers for regressions; do not run paid provider sessions.
- Keep original/candidate manifest checks, backup and rollback intact.
- Historical state version 1 (including stage-4 criteria runs) retains the previous apply contract. New state version 2 requires the final report even when requirements mode is legacy.
- Record an explicit final-report contract marker at new-run creation; missing pieces or unknown versions reject, never downgrade. A new artifact or marker in otherwise old metadata must also invoke strict validation.
- Work on `codex/reporting-stage-5`, based on integrated stages 1–4 at `a90f97810807539ff7507f1b69ea6978b543560d`.

## Review Focus

- Missing or downgraded report metadata must reject before backup or original mutation.
- Final evidence from an earlier candidate/cycle, incomplete criteria or incomplete gate cannot authorize apply.
- Report write failures cannot leave an apply-eligible success without a durable final artifact.
- Reports distinguish planned variants, actually executed variants and incomplete results.
- Historical version-1 runs and new explicit legacy requirements mode remain separate compatibility cases.

### Task 1: Final acceptance contract, reporting and apply integration

**Files:** Create `src/bb_workflow/acceptance.py`, `src/bb_workflow/reporting.py`, `tests/test_reporting.py`; modify `src/bb_workflow/engine.py`, `src/bb_workflow/workspace.py`, `tests/test_acceptance.py`, `tests/test_workspace.py` and `tests/test_engine.py` as needed.

**Interfaces:** `acceptance.py` owns final artifact construction and validation; `reporting.py` owns Markdown rendering. The engine records state version 2 and a report contract marker when creating the run, persists `acceptance.json` before success, and records its canonical digest and binding. The apply validator reads only local audit artifacts under the existing lock and runs no models/checks. Import the validator lazily from workspace if required to avoid the existing requirements → verification → workspace dependency cycle. Keep public Workflow/apply signatures unchanged.

- [ ] Write regression tests first and observe expected failures for new successful criteria and legacy runs, missing/malformed/digest-mismatched report, altered candidate/input hashes, missing/unknown/stale criterion evidence, incomplete gate, new metadata downgrade attempts and failed report persistence. Existing v1 fixtures must still apply.
- [ ] Implement versioned `acceptance.json` containing final cycle, candidate/requirements/plan binding, exact planned checks, actual gate evidence, complete criterion results and independent reviewer/triage outcomes. Use deterministic canonical hashing. Verify full current successful gate and complete satisfied criteria before authorizing apply, and reject malformed types with actionable WorkspaceError. Validate stored frozen requirements/plan content and consistency with state/report. Validate review/triage provenance rather than trusting a standalone satisfied label. Do not claim tamper resistance against coordinated editing of every local artifact.
- [ ] Persist the artifact before `succeeded`; any write/validation error must fail closed. Require the same contract for new legacy-mode runs, with criteria explicitly absent and no new requirements guarantee. Old absent/version-1 states without any new-contract indicators continue through the original apply path; unknown or contradictory versions fail.
- [ ] Extend report.md with separate gate scope and planned/executed variants, diagnostic classifications and log references, per-criterion descriptions/verdicts/evidence/binding, reviewer findings and assessments, triage decisions and rationales. Show current and historical/partial evidence without implying old evidence certifies the current candidate. Preserve usage and cost reporting. Failed, interrupted and budget-limited runs still get useful reports without final approval.
- [ ] Run focused tests while iterating, then one full unittest suite and Ruff checks/formatting. Record RED/GREEN commands and results in the task report; self-review and commit.

### Task 2: End-to-end acceptance and documentation

**Files:** Modify `tests/test_rust_checks.py`, `tests/test_reporting.py` or `tests/test_acceptance.py` if needed; `README.md`, `docs/architecture.md`, `docs/validation.md`, `docs/rustflow-development-plan.md`; `.github/workflows/tests.yml` only if needed to ensure the declared CI platforms actually run Cargo tests.

**Interfaces:** Consume Task 1's persisted `acceptance.json`, state version 2 and apply validator. Do not introduce another report format or change provider protocols/config defaults.

- [ ] Extend real dependency-free offline Cargo coverage to exercise final reporting and apply with workspace/doctests/features; preserve the real zero-test-filter rejection. Ensure new assertions would fail on removal of the production contract. Cover no additional provider invocations at apply.
- [ ] Check existing tests for tool launch failure, interruption, timeout/output/budget limits, changed sources, reviewer independence, v1/v2 config migration and response schemas; add only missing behavior coverage relevant to stage 5.
- [ ] Ensure supported platform CI installs/probes Cargo components before the tests so missing tools cannot silently skip required Cargo coverage. Record actual local platform verification separately from configured remote matrix.
- [ ] Update user and architecture documentation with report fields, files, state-version migration, strict apply behavior and limits. Update development-plan stage statuses based on evidence, and validation documentation with exact executed results and platform limitations.
- [ ] Run focused tests for changes, then the full suite, Ruff, format check and local reviewdog on raw Ruff output. Self-review and commit. No live providers and no claims of Windows/macOS execution on Linux.

## Final verification

- [ ] Independent task review after each task; fix important findings with focused regressions.
- [ ] Whole-branch review by the strongest available model, followed by focused correction/review if needed.
- [ ] Confirm clean final git state and provide branch, results and any real limitations. Do not merge or publish without user instruction.
