---
name: bb-workflow
description: Use when operating an installed bb-workflow CLI from Codex or Claude Code for a Rust task, inspecting its run evidence, or handling a blocked, failed or interrupted run.
---

# bb-workflow operator

For the calling operator; keep out of backend role `skills`. The CLI owns stages
and two independent reviews; `run` leaves original source unchanged.

## First run

If version is unknown, check `bb-workflow --version` once. Use `<command> --help`
for unclear behavior. Substitute the project path:

```sh
bb-workflow --json init --config /path/to/project/workflow.toml
```

Edit that configuration for the project's trusted checks, model access and limits.
Existing configurations need no `init`; it refuses overwrite. Create `task.toml`
in the caller directory with task-specific criteria. Minimal valid example:

```toml
requirements_version = 1
[[criteria]]
id = "requested-behavior"
description = "The requested behavior works and existing public API is preserved"
kind = "semantic"
verification = "Inspect changed source, callers and regression tests"
```

Make the description concrete for the task. Mechanical criteria use
`kind = "mechanical"`, `check_id = "0"` (gate check index), and optionally
`test_name` for an exact test. Semantic criteria forbid those two fields. Use `plan` to validate binding before spending provider budget:

```sh
bb-workflow --json doctor --repo /path/to/project
bb-workflow --json plan --task-file task.txt --repo /path/to/project --requirements task.toml --complexity low
bb-workflow --json run --task-file - --repo /path/to/project --requirements task.toml --complexity low --events-jsonl < task.txt
```

Exactly one positional task or `--task-file` is required. File paths are caller-relative.
Only `-` reads stdin until EOF; `./-` names a file. Supply nonblank UTF-8; whitespace
and line endings stay intact. Task text is separate from `--requirements` criteria.

Choose complexity deliberately: `auto|low|medium|high`; `auto` is a keyword heuristic. Inspect routing,
full checks, limits and `requirements_needed`; keep plan/run inputs consistent.
Requirements/config paths are relative to the caller, even with `--repo`.
`doctor` probes versions/flags, not authentication, model access or LSP. Only run
providers when the user authorized that work; no paid smoke tests or automatic
model changes.

## Read and act

| Need | Action |
| --- | --- |
| Compact progress/result | `bb-workflow --json status /path/to/run --summary` |
| More evidence | `status`, `report.md`, `state.json`, `acceptance.json`, relevant `checks/` or `agents/` logs |
| Check project directly | `bb-workflow --json verify fast --repo /path/to/project`; never grants apply eligibility |
| Authorized application | `bb-workflow --json apply /path/to/run` after succeeded result and separate apply authorization |

Stdout is one version-1 envelope. Check `ok`/`error.code` and `data.status`:
successful `status` can read a failed run; successful `plan` can still need
requirements. Progress/events go to stderr. Prompt characters are not measured
provider tokens; unknown usage/cost remains unknown.

Ctrl+C or POSIX SIGTERM requests cleanup; preserve the announced run directory.
No resume exists: inspect evidence before a new authorized run. A stale heartbeat
only describes supervisor telemetry; it proves neither process death nor permission
to remove `.bb-workflow/lock`. Check processes and lock manually. Apply revalidates
acceptance plus original/candidate snapshots; conflicts need inspection, not bypass.
If apply rollback is incomplete, inspect `apply.json` and its backup path first.
