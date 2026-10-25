# P2 regression fixes implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement the independent tasks below with regression tests first.

**Goal:** Preserve interrupted-process audit output and reject snapshot paths that cannot be applied.

**Architecture:** Carry captured process output through cancellation without suppressing KeyboardInterrupt. Share portable path validation between snapshot capture and manifest loading; retain apply's strict validation.

**Tech Stack:** Python 3.11+, standard library, unittest, Ruff, local reviewdog.

**Spec:** The two confirmed P2 findings and the user's explicit instruction to fix both with regression tests. Preserve the existing documented guarantees in docs/architecture.md.

## Global constraints

- Runtime uses stdlib only; no paid provider sessions.
- Keep stage order, independent reviews, frozen-source checks and explicit apply guarantees.
- Preserve existing user changes. Commit only these fixes, their tests and documentation; publication is outside the task scope.
- Agents own disjoint files, so the two fixes can proceed in parallel.
- Full tests and lint run after both changes and independent review.

## Task 1: Preserve cancellation audit

**Ownership:** src/bb_workflow/process.py, backends.py, engine.py and tests/test_process.py, test_backends.py, test_engine.py.

**Interface:** Add a KeyboardInterrupt subclass carrying a ProcessResult for interrupted captures. Existing KeyboardInterrupt handlers must continue to work. No retry on interruption.

- [x] Add regressions proving captured stdout/stderr survive interruption, the process is cleaned up, provider invocation audit is marked interrupted, and interrupted checks retain logs.
- [x] Run the regressions against the original implementation and observe the intended failures.
- [x] Build a bounded ProcessResult after cleanup and attach it to the interruption; persist output in both callers before reraising. Preserve unknown/partial usage semantics and output limits.
- [x] Run affected tests and report results for independent review.

## Task 2: Validate captured paths early

**Ownership:** src/bb_workflow/workspace.py and tests/test_workspace.py.

**Interface:** Reuse one internal relative-path validator from capture and _read_manifest. Keep exclusions and manifest tamper guards intact.

- [x] Add POSIX regressions for colon/backslash filenames and invalid directory components; a full fixture Workflow must reject the source before any provider call. Explicitly excluded paths must remain usable.
- [x] Run the regressions against the original implementation and observe the intended failures.
- [x] Reject included unsupported paths during capture with an actionable WorkspaceError. Keep apply validation strict.
- [x] Run affected tests and report results for independent review.

## Review focus

- Cancellation remains KeyboardInterrupt and releases process resources/project locks.
- Empty output and bounded/truncated output remain distinguishable from success.
- Logs from preparation and validation are preserved, as well as provider logs.
- Invalid ancestor components cannot bypass capture checks through reinclusion rules.
- Excluded files remain ignored, and existing malicious manifest paths remain rejected.

## Completion

- [x] Independent review of each task and combined changes.
- [x] Update concise user documentation for the two changed behaviors.
- [x] Run PYTHONPATH=src python3 -m unittest discover -s tests -q.
- [x] Run raw Ruff through local reviewdog and ruff format --check src tests examples.
- [x] Inspect final diff and report results.

## Execution notes

Work proceeds in the existing checkout on codex/fix-review-p2; the pre-existing user changes are outside the owned files. Baseline review verified 210 tests (2 skipped), Ruff and format checks. No product decision is missing from the accepted findings, so implementation proceeds without an additional approval round.

Task 2: complete; 37 workspace tests passed. Independent GPT-6.1 Sol review confirmed spec compliance and code quality with no findings.

Task 1: implemented; 118 focused tests passed. Four initial regressions failed before production changes. The original real-SIGINT reproducer now retains both output streams and invocation.json. Provider interruption preserves partial measurements and never retries.

First combined verification: 221 tests in 42.497s, OK (2 native Windows tests skipped on Linux). Ruff through local reviewdog, Ruff format check, and git diff --check passed.

Final GPT-6 Astra review found one interaction: an interrupted command can leave an unscannable tree, and the post-command manifest then masks interruption before recording it. The final fix wave skips that scan only for interrupted commands, records unknown result_snapshot_id/source_unchanged as null, and preserves ordinary frozen-source validation. A regression covers interrupted preparation and validation leaving invalid source. Focused re-review and fresh full verification are recorded below.

Final fix wave: addressed; 54 engine tests passed. GPT-6 Astra scoped re-review marked the finding addressed and both Task 1 and combined review PASS, with no remaining findings.

Final verification after the fix wave: 222 tests in 43.800s, OK (2 native Windows tests skipped on Linux). Raw Ruff through local reviewdog, Ruff format check and git diff --check all passed. The user subsequently requested a commit of these fixes, tests and documentation on codex/fix-review-p2, excluding unrelated changes.

## Integration with current main

The user requested resolving PR #6 against main at ec5aa166583a01f07a206d310bd77a1e87537e83, preserving both P2 fixes and all main features, then testing, committing and pushing the branch.

- [x] Retain main's shared verification runner, diagnostics, acceptance and final report contracts while porting interruption audit handling into the shared runner and metadata probes.
- [x] Preserve both sets of workspace regressions and main's permission/report-binding safeguards; adapt the early-capture fixture to the current configuration contract.
- [x] Independently review the integrated diff against main, then run the full test suite, Ruff, formatting and local reviewdog.

The integration adds two regression tests covering four interruption scenarios: standalone verification with zero exit and optional output overflow, and cancellation during a Cargo version probe versus the actual check. Interrupted probes retain their own output without attributing it to an unstarted check. All test methods from both parents remain. Independent GPT-6 Astra review: PASS, no actionable regressions.

Full integrated verification: 421 tests in 85.366s, OK (2 native Windows tests skipped on Linux). Raw Ruff through local reviewdog passed with no findings; Ruff format check passed for all 40 files; git diff --check passed. The offline-provider demo with real Cargo completed with status succeeded. No paid provider sessions were invoked.

Publication is authorized on codex/fix-review-p2, excluding unrelated local changes. The merge commit and GitHub integration status are recorded in PR #6 after pushing.
