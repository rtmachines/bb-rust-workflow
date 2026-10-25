# Rust PR review adapter

The user approved implementing the recommended adapter for both existing reviewers,
acceptance criteria, and regressions preventing approval without required evidence.

## Design

Keep the current stage order and separate reviewer snapshots. An opt-in
`[rust_review]` policy appends a packaged, provider-neutral adapter to both reviewer
prompts and the triage prompt. It does not execute upstream shell scripts, hooks,
subagents, fetches, or provider sessions. Existing role skills remain available.
The adapter follows rtmachines/rtm-coding at
`268582b5c4d8f91bc709294b9ebbe270a181f9b2`, with source attribution.

`enabled` defaults to false for compatibility; `version = 1` freezes the policy
and generated criterion wording for historical approval replay. `required` lists evidence kinds:
`memory`, `deadlocks`, `cancellation`, `task_lifecycle`, `loom`. Each required kind
must have one or more `evidence` entries with `kind`, `check_id`, and exact
`test_name`. Unknown kinds, duplicates, disabled policies with declarations,
missing evidence, and unsuitable planned commands are rejected before providers.
Memory requires direct `cargo [+toolchain] miri test`; other kinds require direct
Cargo tests. Both require completed, nonempty, named libtest proof. Loom test
adequacy and completion remain explicit semantic obligations, not inferred from
exit status. Nextest/wrapped commands are not supported as targeted proof.

Generate reserved `rust-review-*` ordinary acceptance criteria before freezing
requirements: specification, scope completeness, memory/aliasing, async coverage,
and synchronization/model adequacy, plus a mechanical criterion per evidence
entry. Preserve user requirements and the existing requirement/response versions.
No user-authored reserved IDs are accepted. The policy needs explicit user task
requirements even for legacy configurations. Reviewers cannot waive mechanical
proof. New risk absent from the frozen plan requires `blocked` and a new run.
Missing semantic evidence remains unresolved, never a fabricated defect. Triage
cannot waive an unresolved safety scope/argument reported by either reviewer;
ordinary specification disputes retain the existing adjudication behavior.

Replay acceptance validates the policy's canonical generated criteria and actual
proof, so removing criteria or losing logs cannot authorize apply. Persist policy
identity in the reserved criteria and final acceptance contract, retaining old
contracts when the policy is absent. Do not mutate the plan after implementation.

## Scope

Python 3.11+, stdlib runtime, fixture providers only. No stage changes, implicit
tool installation, model changes, native Claude agent hooks, or paid test runs.
Do not load all upstream Rust rules; selected project-local knowledge can be
supplied via existing skills. LSP availability must be reported honestly.

## Alternatives

Direct invocation conflicts with snapshots, JSON and tool restrictions. A third
reviewer duplicates existing orchestration. The approved adapter reuses the
current two reviewers and acceptance/replay machinery.

## Acceptance

Missing, zero, ignored-only, wrong-name, stale or incomplete required test evidence
cannot produce succeeded/apply, even with zero findings or favorable semantic
verdicts. Unresolved aliasing/synchronization blocks success. Complete current
proof permits success with the existing stage order and reviewer independence.
Legacy disabled-policy behavior and saved artifacts retain their current contract.
