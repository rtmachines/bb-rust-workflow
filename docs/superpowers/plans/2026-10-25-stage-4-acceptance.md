# Stage 4 acceptance criteria implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Implement stage 4: task requirements with mechanical proof and independent semantic acceptance, bound to the candidate, requirements and verification plan.

**Architecture:** Add a focused requirements module and a strictly versioned response contract, then integrate them into existing run/check/review/triage/fixer transitions. Retain existing gate IDs paired with a full plan hash. Persist immutable parsed inputs before implementation; do not change apply enforcement (stage 5).

**Tech Stack:** Python 3.11+, stdlib runtime, unittest fixtures, real offline Cargo regressions, Ruff/reviewdog.

**Spec:** docs/rustflow-development-plan.md section 4. Design recommendations at /tmp/bb-stage4-design.md are supporting material; decisions below are authoritative implementation detail.

## Global Constraints

- Python 3.11+ CLI; runtime uses stdlib only.
- Keep stage order, independent reviews, frozen-source checks and explicit apply guarantees.
- Prompt changes must retain evidence recovery and structured response validation.
- Use fixture providers for regressions; paid provider sessions need an explicit request.
- Keep full audit logs on disk; distinguish prompt size from measured provider tokens and unknown usage from zero.
- Triage cannot override failed mechanical criteria; zero executed filtered tests are not proof.
- Missing assessments, unknown IDs, stale binding, and implementer declarations cannot authorize success.
- Every result is bound to candidate + requirements + full verification plan.
- Stage 3 is independent; do not pull in its branch. Stage 5 apply enforcement is out of scope.
- Work only in the assigned managed worktree. Use graph tools with explicit repo_root before narrowing source; source wins.
- Apply existing project Ruff formatting (100 columns). Do not add dependencies.

## Review Focus

- Mutable requirements/config changes during a run: immutable inputs remain enforced.
- Malformed or unsupported test output, zero/ignored/absent cases: no false proof.
- Disputed semantic criterion without code findings: triage runs and unresolved blocks.
- V1/v2 migration and config v3 defaults: no accidental fallback or replacement of explicit checks.
- New candidate after fixes: repeat full gate and both independent assessments; no old proof reuse.

### Task 1: Requirements and response contracts

**Files:** create src/bb_workflow/requirements.py and tests/test_requirements.py; modify src/bb_workflow/protocol.py and tests/test_protocol.py only.

**Interfaces:**
- Immutable Criterion and Requirements dataclasses; Requirements contains requirements_version=1 and tuple of Criterion.
- Criterion fields: id, description, kind, verification (nonempty human description of verification method), optional check_id/test_name.
- load_requirements(path: Path) -> Requirements; strict TOML root requirements_version + criteria array.
- bind_requirements(requirements: Requirements, gate_plan: VerificationPlan) -> BoundRequirements; validate IDs/checks/test support before providers.
- requirements_id(requirements: Requirements) -> str; verification_plan_id(plan: VerificationPlan) -> str; SHA256 canonical JSON of all parsed fields, including version, ordered checks/profile/variants/package/test_filter/argv.
- evaluate_mechanical(bound: BoundRequirements, check_results: list[dict], binding: dict, artifact_root: Path) -> list[dict]; one item per mechanical criterion with criterion_id, verdict (satisfied|unmet|unresolved), evidence and binding. IDs in binding: candidate_id, requirements_id, verification_plan_id.
- protocol.response_schema(version: int = 1) -> dict, preserve legacy RESPONSE_SCHEMA.
- Extend validate_response(existing positional args, *, version=1, expected_binding=None, criteria=(), disputed_criteria=()). criteria is tuple of Criterion, disputed_criteria tuple of semantic IDs. All return fields as described below.

**Format:**
```toml
requirements_version = 1
[[criteria]]
id = "upper-bound"
description = "Reject a setpoint above the upper bound"
kind = "mechanical"
verification = "The exact regression test passes in the gate"
check_id = "2"
test_name = "validation::rejects_upper_bound"
[[criteria]]
id = "scope"
description = "Keep the public API"
kind = "semantic"
verification = "Review exported signatures and callers against baseline"
```
Mechanical requires check_id; semantic forbids check_id/test_name. test_name is optional on recognized direct Cargo test commands (optional +toolchain). Require nonempty strings and stable IDs [A-Za-z][A-Za-z0-9_-]*. Reject empty criteria, unknown keys/types/versions, bool version, duplicate IDs. Do not reload files after parsing.

**Mechanical proof:** Require exactly one matching current candidate validation record (id/profile/variant/command, both snapshots, source_unchanged true, passed true, returncode 0, no timeout/overflow/error). Validate binding hashes against bound inputs. Missing/duplicate/mismatched records are unproven. Failed checks => unmet. Inspect FULL audited stdout/stderr under artifact_root, not tails, when test proof needed. Every direct Cargo test criterion, including check-only, requires at least one actually passed nonignored test from completed successful libtest summaries; zero, ignored-only, no-run/list or unknown output cannot satisfy. Named tests additionally require exact successful libtest test line within completed successful output; another passing test cannot prove the requested name. Do not mutate commands or narrow gate. Custom non-test command criteria rely on trusted command success; test_name on custom commands rejected. Unknown/missing log output => unresolved; known zero/missing named test => unmet. Preserve full logs; bounded summaries only.

**V2 response:** Retain original four fields, add schema_version integer 2, binding object with three exact nonblank IDs, criterion_evidence [{criterion_id,evidence}], criterion_assessments [{criterion_id,verdict,evidence}], criterion_decisions [{criterion_id,verdict,rationale}]. Verdicts satisfied|unmet|unresolved.
- implementation/fixer may supply hints for known criteria only; assessments/decisions empty.
- reviewer_a/b must assess exactly all semantic IDs once; decisions and writer hints empty.
- triage must decide exactly disputed semantic IDs once; assessments and writer hints empty.
- Role-inappropriate fields, unknown/duplicate IDs, missing values, wrong version or any mismatched binding are rejected. Writer binding identifies pre-invocation input; hints cannot approve post-edit work.
- V1 remains byte-for-byte shape compatible, including unexpected-field rejection.
- JSON schemas are detached copies and work with existing backend schema pass-through.

- [x] Write failing unit tests for strict loading/binding/hashes, complete and stale proof, full logs beyond tails, zero/ignored/missing named tests, failed/time-limited results, unsupported harness output; run focused tests and record RED.
- [x] Implement requirements contracts with public type annotations and small focused helpers.
- [x] Write failing protocol tests for every role, stale three-way binding, missing/duplicate/unknown criterion IDs, exact triage coverage and legacy rejection; implement.
- [x] Run focused tests, full unittest suite, Ruff and format checks; self-review; commit only owned files. Report APIs, test evidence, concerns.

### Task 2: Run integration, migration and user documentation

**Files:** engine.py, cli.py, config.py, default.toml, prompts/*.md; engine/CLI/config/backend/Rust test files plus new test_acceptance.py if useful; README.md, docs/architecture.md, docs/rustflow-development-plan.md and examples/task-requirements.toml.

**Consumes:** Task 1 interfaces exactly; inspect task 1 report for finalized dataclasses. Do not change core APIs unless a concrete integration defect requires it.

**Produces:** Workflow(..., *, requirements: Requirements | None = None); run/plan --requirements PATH; persistent normalized requirements.json, verification_plan.json, acceptance mode/hashes and round records in state; v3 config migration.

- [x] Tests first: v3 missing requirements rejects before any provider/check, old configurations still run in explicit compatibility mode, opt-in v1/v2 uses v2 protocol. Config loader supports 1,2,3; missing version remains 1. Config() and packaged template advance to 3. V2 and V3 missing checks both use full DEFAULT_CHECKS; V1 fallback preserved; explicit checks unchanged. Existing tests intentionally exercising legacy workflow must pin version 2 and retain original assertions. verify/doctor/plan without requirements remain usable; plan reports requirements needed and compatibility mode.
- [x] Freeze resolved gate plan once, validate requirements/bindings before checks/providers, persist normalized spec and plan before implementation. CLI parses once. Programmatic workflows enforce v3 criteria too. Requirements supplied to old config opt in; malformed new contract never downgrades.
- [x] For every role pass common immutable spec and three-way binding. Writable role binding is pre-call snapshot and hints are recorded only as proposals. For reviewers use the actual prepared frozen candidate binding; both receive identical criteria/mechanical evidence with no mutual assessment. Preserve prompt recovery/log paths and source guards.
- [x] After prepare and whole gate, evaluate mechanical criteria. Known missing/zero proof goes through bounded Fixer path; unresolved proof blocks. Mechanical failure can never reach semantic override. Persist per-round mechanical result even on failure. In legacy mode preserve existing flow and v1 response schema.
- [x] Require both reviewers to assess every semantic criterion. Any unmet/unresolved or disagreement goes to existing triage even when code findings empty. Triage separately resolves original findings and disputed criteria exactly. Unmet semantic criterion goes to Fixer regardless of blocking_severities. Unresolved blocks. Repaired candidate reruns full gate and both reviews from scratch. At success require every criterion satisfied at current binding and original gate/source/budget conditions.
- [x] Add fixture end-to-end regressions for missing/unknown/stale assessments, implementer self-approval, semantic-only disagreement/unresolved/unmet/fix rounds, mechanical nonoverride, criteria file edits/config mutation, reviewer independence and legacy compatibility. Backend tests prove supplied v2 schema survives both native adapters if existing generic schema assertions do not suffice.
- [x] Add real small offline Cargo regression proving zero filtered cases cannot satisfy criterion and named passing test can. Use fixture providers, no paid CLI sessions.
- [x] Update examples/README/migration/architecture and stage 4 completion notes accurately; basic state/report mode disclosure required, richer final reporting/apply binding stays stage 5.
- [x] Run relevant tests then full suite, Ruff through local reviewdog and format check, self-review and commit. Report counts, skips, known limits and exact final interfaces.
