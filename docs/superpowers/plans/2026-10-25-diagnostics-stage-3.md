# Stage 3 diagnostics implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Follow the tasks with tests first.

**Goal:** Classify Cargo/rustc/Clippy diagnostics against comparable baseline evidence and provide concise agent summaries without weakening verification.

**Architecture:** Pure parsing and comparison live in `diagnostics.py`. The shared verification runner captures full output and comparison metadata; the engine compares every candidate to the original baseline and projects bounded evidence into prompts.

**Tech Stack:** Python 3.11+, stdlib only, unittest, existing process runner, offline Cargo fixtures.

**Spec:** `docs/rustflow-development-plan.md`, stage 3. Base: completed stage 2 commit `30612b45e5d8b4dc55dc9e8a5240294621e2749b`.

## Global Constraints

- Keep stage order, independent reviews, frozen-source checks and explicit apply guarantees.
- Execution success is separate from parsing success. No diagnostic classification can waive a failed gate.
- Full stdout/stderr remain on disk; prompt summaries must retain recovery paths.
- Preserve configured command argv. Recognize JSON when present and common human output without inserting flags.
- Only fixture model providers; no paid provider runs. Metadata probes use the existing bounded runner and global budget.
- Classification values: `new`, `persistent`, `resolved`, `unclassified`, `unobserved_previous`.
- Changes to package/test scope, variant, commands, tool versions or relevant configuration invalidate comparison. Unknown metadata does not compare equal.
- Line numbers are presentation data, never the only identity. Normalize actual project-root paths without conflating external files.

## Review Focus

- Compile failure prevents test execution: previous test failure must be `unobserved_previous`.
- Timeout, output overflow, malformed output and skipped checks must never manufacture `resolved`.
- Different snapshots, moved source lines and repeated same-name tests must avoid false identity matches.
- Tool/config changes (including inherited Cargo configuration) invalidate comparison; probes stay bounded and audited.
- Red baseline plus persistent red candidate must still stop before review/apply.

### Task 1: Pure diagnostics parser and conservative comparison

**Files:** Create `src/bb_workflow/diagnostics.py`, `tests/test_diagnostics.py`.

**Interfaces:** Produce a frozen `DiagnosticContext` dataclass with `profile: str`, `variant: str`, `argv: tuple[str, ...]`, `scope: dict`, `tool_versions: dict[str, str | None]`, `configuration_digest: str | None`.

Public functions:

```python
def parse_diagnostics(stdout: str, stderr: str, *, project_root: Path,
                      context: DiagnosticContext, execution: dict) -> dict: ...
def compare_diagnostics(current: dict, baseline: dict | None) -> dict: ...
def summarize_diagnostics(report: dict, *, max_items: int = 20) -> dict: ...
```

All returned values are JSON serializable. Reports carry schema version, captured context, parsed findings with fingerprints and locations, domain observation/uncertainty and execution validity. Comparison returns all five named classification lists plus counts and reasons. Document exact dictionary structure in docstrings for Task 2.

- [ ] Write failing behavior tests for Cargo compiler-message JSON, direct rustc JSON, common rustc/Clippy text, malformed records, unrecognized/custom test output, explicit libtest failures/passes and missing/ambiguous suite identity.
- [ ] Write identity/classification tests: different project roots and changed line numbers match; severity/code/path/message/source anchors distinguish findings; duplicates retain occurrence counts; changed argv/profile/variant/scope/tool/config or missing metadata makes current findings unclassified and previous ones unobserved.
- [ ] Write safety tests: matching findings stay persistent even with partial current output; new requires sufficient baseline observation; missing compiler finding resolves only on comparable valid successful completed check; a test resolves only with explicit corresponding pass and unambiguous suite identity. Failed compilation, filtered/zero tests, timeout, overflow, invalidated source, interruption and malformed/unknown output cannot resolve a previous test.
- [ ] Run `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_diagnostics.py' -q`, record expected missing-feature failures, then implement minimal code and rerun.
- [ ] Parse full streams independently. `build-finished.success` is compilation evidence only, never test completion. Cargo `package_id` may contain local URLs; use manifest/target paths for local identity. Preserve external paths. Do not attempt comprehensive custom harness parsing.
- [ ] Make summaries bounded in both item count and text size, include omitted counts/uncertainty, and keep full reports untouched.
- [ ] Run full unit suite, Ruff on changed files and commit Task 1. Record API shape, red/green evidence, tests, commit and remaining concerns in the task report.

### Task 2: Capture metadata, integrate baseline comparison and audit evidence

**Files:** Modify `src/bb_workflow/verification.py`, `src/bb_workflow/engine.py`, `src/bb_workflow/prompts/common.md`; test `tests/test_verification.py`, `tests/test_engine.py`, `tests/test_rust_checks.py`; update README, architecture and development-plan stage 3 status after verification.

**Interfaces:** Consume the Task 1 dataclass and functions. Existing `execute_checks()` records retain their original execution fields. Add optional baseline records and scope metadata only as needed without breaking current callers. Persist per-check full `diagnostics.json` and bounded report references; aggregate phase comparisons include baseline findings from checks not reached.

- [ ] First read Task 1 implementation and report. Write failing integration tests proving diagnostics are parsed from full streams (not tails), saved before fatal propagation and projected to implementer/fixer/reviewer prompts without mutating audit records.
- [ ] Add metadata capture for recognized Cargo/rustc commands, including explicit toolchain selectors. Probe actual relevant tools using injected process_fn, snapshot environment, timeout callback and output cap. Save probe argv/stdout/stderr/result under check-phase artifacts. Cache only within a phase. Failed/empty/unrecognized version evidence stays unknown; interruptions and exhausted global budget propagate. Unknown wrappers/custom commands remain unclassified and incur no guessed probes.
- [ ] Capture effective scope/argv/profile/variant and a digest of relevant project manifests/lockfiles, toolchain selectors, Cargo/Clippy configuration, applicable ancestor/user Cargo configuration and relevant environment (hash values rather than store secrets). Capture separately for baseline and each candidate, before commands. Source mutation by metadata probes must invalidate checks.
- [ ] Add comparison to `execute_checks` before recording each validation result. Preparation remains outside comparisons. Preserve all current execution status and source guards. Standalone verify has no automatic baseline and reports unclassified diagnostics.
- [ ] Engine compares every candidate with immutable original baseline records. Add phase aggregate in audit and agent evidence, including unobserved previous findings for checks skipped after fatal failure. On failure/finalization, preserve partial evidence. Existing failed-log tails and full-log references remain.
- [ ] Add fixtures for changed tool/config/scope, failed probes, timeout/overflow, partial/skipped checks and persistent red baseline; verify gate still blocks and reviews remain independent. Update old fixtures to explicitly recognize metadata probes only when Rust commands are used.
- [ ] Add real offline Cargo regression: baseline test failure, candidate compile failure produces unobserved previous test, then a passing corresponding test resolves. Add real compiler diagnostic comparison across distinct roots/moved lines, and JSON parsing coverage if not already provided by pure tests.
- [ ] Document conservative classifications, metadata and parser uncertainty, retained logs, and standalone verify behavior. Do not implement stage 4 requirements or stage 5 apply changes.
- [ ] Run focused tests, full `PYTHONPATH=src python3 -m unittest discover -s tests -q`, `ruff check src tests examples`, `ruff format --check src tests examples`, local reviewdog with raw Ruff output and `git diff --check`; commit. Report exact verification evidence and limitations.
