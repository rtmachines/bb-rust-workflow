# Machine CLI contract

Add `--json` before or after any command to request schema version 1:

```sh
bb-workflow --json plan "Describe the requested change" --repo /path/to/project
bb-workflow status /path/to/run --summary --json
bb-workflow run "Describe the requested change" --requirements requirements.toml --json --events-jsonl
```

Only an explicit `--json` before literal `--` selects this contract. For example,
`bb-workflow plan -- --json` treats `--json` as task text and keeps legacy output.
With `bb-workflow --json plan -- --json`, the task is still literal text and the
result uses the envelope. Quote task text containing spaces.

## Envelope

Stdout contains one JSON document, including on argument errors, help and handled
exceptions. Progress is separate on stderr. The envelope fields are:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1`. Independent of configuration, response and state versions. |
| `command` | Selected command, or `null` before a command was selected. |
| `ok` | Whether this CLI command completed successfully. |
| `exit_code` | The process exit code, also returned by embedded `main()`. |
| `data` | The command payload, or `null` when no payload was available. |
| `error` | `null` on command success; otherwise the error object below. |

Example of a successful read of a failed run:

```json
{
  "schema_version": 1,
  "command": "status",
  "ok": true,
  "exit_code": 0,
  "data": {"status": "failed", "reason": "Verification commands failed"},
  "error": null
}
```

`ok` describes the command, not workflow approval. In particular, reading a
failed, blocked or interrupted run with `status` returns zero. `plan` with
`requirements_needed: true` also returns zero: the plan was successfully produced.
A `run` missing required criteria remains a blocked run with exit 1 and saved
artifacts. Standalone `verify` never authorizes apply, even when it passes.

| Command | `data` on a normal result |
| --- | --- |
| `init` | `{"config": "/absolute/path/workflow.toml"}` after exclusive creation. |
| `plan` | Existing routing, requirements, verification and limits payload. |
| `doctor` | Existing backend and executable metadata probe results. |
| `verify` | Existing `status`, `reason`, `run_dir`, `report`, `apply_eligible` payload. |
| `run` | Existing `status`, `reason`, `run_dir`, `report` payload. |
| `status` | Persisted state; `--summary` uses the existing compact view and valid live progress. |
| `apply` | Existing `status: "applied"` and `changed_paths` payload after guarded application. |

Root and subcommand `--help` return a successful envelope with
`data: {"help": "..."}`. A root help request has `command: null`.
`bb-workflow --json --version` also returns a successful root envelope with
`command: null` and `data: {"version": "<package version>"}`. Without `--json`,
`--version` prints `bb-workflow <package version>` and exits zero.
Argument errors return `invalid_arguments` and exit 2; command selection comes
from the parser, never from task text or option values. Status reads accept sparse
legacy state objects. Malformed JSON, non-object states, invalid optional
status/phase/reason values and non-object acceptance metadata return `invalid_state`.

## Errors and next actions

Each error has `code`, human-readable `message`, boolean `retryable`, stable
`next_action` identifier, and `artifacts` (an array of known existing file paths).
Do not classify failures by parsing `message`; its wording is not a contract.

| Code | Typical origin | `next_action` |
| --- | --- | --- |
| `invalid_arguments` | Parser rejection or invalid run directory/task input | `correct_arguments` |
| `invalid_config` | Missing explicit configuration, malformed or unsupported configuration | `correct_configuration` |
| `invalid_requirements` | Missing requirements file, malformed criteria or invalid binding | `correct_requirements` |
| `requirements_required` | Run requires criteria that were not supplied | `provide_requirements` |
| `project_locked` | Exclusive project lock already exists | `inspect_project_lock` |
| `source_conflict` | Original source differs during apply or standalone verification | `inspect_source_conflict` |
| `candidate_conflict` | Candidate differs after validation or while copying it for apply | `inspect_candidate_conflict` |
| `provider_error` | Backend error propagated to the workflow or CLI | `inspect_provider_logs` |
| `interrupted` | Graceful interruption, including POSIX SIGTERM | `inspect_interrupted_operation` |
| `io_error` | Filesystem error or failed report persistence | `inspect_filesystem` |
| `invalid_state` | Malformed status metadata or a run without succeeded status passed to apply | `inspect_state` |
| `workspace_error` | Other snapshot or guarded-apply rejection | `inspect_workspace` |
| `apply_recovery_required` | Apply failed and rollback was incomplete | `recover_apply_backup` |
| `doctor_failed` | One or more required metadata probes failed | `inspect_doctor_results` |
| `blocked`, `failed`, `limit_exhausted`, `budget_exhausted`, `no_progress` | Terminal outcome without a more specific structured cause | `inspect_run_artifacts` |
| `internal_error` | Unclassified exception at the CLI boundary | `inspect_internal_error` |

Categories originate in exception types and structured stop metadata. The
workflow persists meaningful failure categories as additive `error_code` state
metadata. Provider failures are not exhaustively subdivided: do not infer login,
model access, rate limit or transport categories from this general code.

In version 1, failures conservatively have `retryable: false`. This means safe
automatic repetition of the **CLI command** has not been established. It is
independent of backend retry policies for an individual role. A partial or failed
run can have consumed budget; repeating it starts a new run. Apply can have
modified files or left a recovery backup. Inspect the saved evidence and correct
the cause before deciding on another command.

`next_action` is guidance for the caller. It never executes apply, removes a lock,
logs in, starts a paid run or resumes work. In particular, inspect the process and
lock before any manual recovery; a stale heartbeat does not establish process
death. `apply_recovery_required` directs attention to `apply.json` and its backup
path, with concurrent edits preserved by best-effort rollback.

`error.artifacts` links only known files that exist when the result is emitted:
`state.json`, `report.md`, `report.json`, `apply.json`, and `acceptance.json`, when
available for the known run directory. A report write failure may leave only
`state.json`; missing files are not advertised in this array. Existing payload
fields such as `data.report` retain their legacy expected-path semantics and do
not prove that the file was written. Inspect `apply.json` for the backup directory;
the artifacts array contains file links, not guessed directory links.

## Exit codes and progress

- `0`: Successful command; for `run`, succeeded; for `verify`, passed.
- `1`: Completed command reporting unsuccessful workflow, verification or doctor outcome.
- `2`: Invalid input/configuration/state, guarded apply rejection, I/O error or unclassified CLI failure.
- `130`: Interruption.

`--json` changes stdout only. Run announcements and phase progress stay on stderr.
`run --events-jsonl` replaces those text messages with versioned lifecycle JSONL
on stderr, and can be combined with `--json`. The final stdout result remains one
envelope. Heartbeat and active-operation data are advisory supervisor telemetry;
they do not prove provider progress, completion, or token consumption. Terminal
state remains authoritative.

Usage values retain their existing semantics: prompt size is not measured
provider token usage, unknown token/cost measurements stay unknown, and missing
usage is never converted to zero. Full audit evidence remains on disk.

## Compatibility

Without `--json`, existing result payloads, init text, stderr diagnostics, and
0/1/2/130 exit meanings are retained. Legacy help and argument errors keep
argparse's `SystemExit` behavior for callers embedding `main()`. JSON mode returns
the exit code normally for help and parse errors. Sparse historical states are
accepted; malformed states produce a diagnostic instead of an accidental traceback.

Callers should check `schema_version`, ignore additive fields they do not use, and
handle unknown error codes conservatively. Incompatible envelope changes require
a new schema version. Error message wording and help text can evolve. Successful
workflow output still requires explicit `apply`; this contract adds no resume or
automatic recovery behavior.
