# Operating bb-workflow from Codex or Claude Code

`bb-workflow` is a local Python 3.11+ CLI. Install it in the environment used by
the calling agent (`python -m pip install -e /path/to/bb-rust-workflow` for a local
checkout). Both `bb-workflow` and `python -m bb_workflow` then work outside that
checkout. Check `bb-workflow --version`, `bb-workflow --help` and each command's
`--help` for the installed interface.

Git and the tools selected in your configuration must be installed. Authenticate
the selected provider CLIs separately and check model access. `doctor` makes
metadata probes without starting inference; it cannot verify authentication,
model access or LSP. Use trusted repositories, check commands and MCP servers:
working copies isolate source changes, not access to the host.

## Prepare before using providers

From the Rust project, create a configuration once:

```sh
bb-workflow --json init --config workflow.toml
```

`init` exclusively creates the file and refuses overwrite. Edit it for the
project's preparation/check commands, provider models and budgets. Changing a model
requires an explicit configuration change; there is no automatic cheaper fallback.
An existing configuration can be used directly.

Create `task.toml` before `plan`/`run`. Configuration v3 requires nonempty criteria.
This valid starting example includes a mechanical formatting check and a semantic
requirement; adapt the behavior and evidence to the actual task:

```toml
requirements_version = 1

[[criteria]]
id = "format"
description = "Workspace formatting check passes"
kind = "mechanical"
verification = "Run the configured full-gate formatting check"
check_id = "0"

[[criteria]]
id = "error-handling"
description = "Configuration read errors reach the caller without a panic"
kind = "semantic"
verification = "Inspect the error path, callers and regression tests"
```

`check_id` is a string identifying a check in the resolved full gate: `"0"` means
formatting only with the default configuration. Verify it against `plan` after
changing commands. Mechanical criteria can have an exact `test_name`; semantic
criteria forbid `check_id` and `test_name`. Direct Cargo tests require a complete,
successful nonempty test result; named tests require the exact passing name.
The [full example](../examples/task-requirements.toml) and
[acceptance architecture](architecture.md#kryteria-odbioru-etap-4) explain the scope.

```sh
bb-workflow --json doctor --repo .
bb-workflow --json plan "Return configuration read errors without a panic" --repo . --requirements task.toml --complexity low
```

Inspect `data.roles`, `checks`, `verification.gate_plan`, `prepare`, limits and
`requirements_needed`. `plan` runs no provider and validates requirements against
the gate. Requirements and the entire verification plan have binding IDs; `run`
freezes them and binds later review/evidence to the actual candidate. Keep inputs
consistent between plan and run. The two independent reviews remain mandatory.
Choose complexity explicitly where appropriate: `auto` uses task keywords and is
not a risk assessment. A plan needing requirements still exits zero.

`--repo` defaults to the caller directory. Without `--config`, commands discover
`workflow.toml` inside that repository, then use built-in defaults if absent.
Explicit `--config`, `--requirements` and `run_dir` paths are relative to the
caller directory, not to `--repo`. Absolute paths are convenient outside the project.

## Start, observe and inspect

After the user authorizes provider work:

```sh
bb-workflow --json run "Return configuration read errors without a panic" --repo . --requirements task.toml --complexity low --events-jsonl
```

The run directory is announced on stderr before expensive work; retain it even
if the command is interrupted. `run` works in snapshots under
`<repo>/.bb-workflow/runs/<id>/` and leaves original source unchanged. Stdout is
one final versioned envelope. Text progress goes to stderr; `--events-jsonl`
selects versioned stderr events instead. See the [machine contract](cli-contract.md)
for fields, error codes, safe next-action identifiers and exit codes.

```sh
bb-workflow --json status /path/to/run --summary
bb-workflow --json status /path/to/run
```

Start with `--summary`: status, phase, counters, usage, compact acceptance and
valid active-operation/heartbeat metadata while running. Heartbeat indicates only
supervisor activity, not provider progress or tokens. Terminal saved state wins
over live telemetry. An absent/malformed sidecar does not imply completion.

`ok` describes the CLI command. `status` returns zero when it successfully reads
a failed, blocked or interrupted run. Read `data.status` and `reason` as well.
For failed commands, inspect `error.code`, `next_action` and known existing
`artifacts`; do not classify failure by matching message prose. `retryable: false`
does not authorize automatically repeating a paid or partially completed command.

Escalate evidence only as needed: `report.md`, then full `state.json` and
`acceptance.json`, then the relevant full `checks/` or `agents/` logs and saved
prompts/responses. Summaries and truncated output are not missing evidence.
Prompt characters/bytes measure prompt size, not total provider tokens. Keep
unknown token/cost measurements unknown; a known subtotal is not a full bill.
Logs may include project content and secrets visible to providers; protect them.

`bb-workflow --json verify fast --repo /path/to/project` runs local checks without
providers or preparation. Other profiles are `focused`, `lint`, `gate`.
`--package`/`--test-filter` can narrow suitable profiles, never `gate`. Verify
runs directly in the original project and detects source changes without undoing
them. Its logs are in `.bb-workflow/verifications/`; even `passed` never grants apply.

## Interrupt and recover

Use Ctrl+C, or SIGTERM on POSIX, for graceful interruption and child cleanup.
Exit 130 identifies interruption; saved reports/logs remain inspectable and the
workflow releases its lock through cleanup. SIGKILL and crashes cannot guarantee
that cleanup. Preserve the run directory and inspect processes, state and logs.

There is no resume command. A subsequent authorized `run` starts a new snapshot
and can consume new budget. A stale heartbeat does not prove that a process is
dead. If `.bb-workflow/lock` remains, establish manually that no workflow process
is active before deciding on manual lock recovery; never delete it automatically.
Correct configuration, requirements or provider access from evidence before another
run. Do not use a paid session as a smoke test.

## Apply separately

After a succeeded run, inspect the result and obtain separate user authorization
for application:

```sh
bb-workflow --json apply /path/to/run
```

`apply` revalidates saved acceptance, the original snapshot and the approved
candidate before copying changes. It refuses source/candidate conflicts and never
merges them automatically. Avoid concurrent editing while applying. If applying
fails, inspect `apply.json`: rollback preserves later edits when possible, and
incomplete recovery reports its backup path. Inspect that evidence before retrying.

## Optional portable operator skill

The distribution contains the real file `bb_workflow/skills/bb-workflow/SKILL.md`.
It is for the calling operator, not a provider role; do not list it in backend
`skills` or use it to launch nested workflows. Installation is an explicit copy,
independent of the orchestrator checkout. No personal configuration is modified
by installing the Python package.

Save this as `copy_operator_skill.py` and run it with the same Python environment
as bb-workflow. The selected root is an argument; existing files are preserved:

```python
from importlib.resources import files
from pathlib import Path
import sys

destination = Path(sys.argv[1]).expanduser() / "bb-workflow" / "SKILL.md"
content = files("bb_workflow").joinpath("skills/bb-workflow/SKILL.md").read_bytes()
destination.parent.mkdir(parents=True, exist_ok=True)
with destination.open("xb") as stream:
    stream.write(content)
print(destination)
```

Choose **one** destination:

```sh
# Personal Codex skill:
python copy_operator_skill.py "~/.agents/skills"
# Personal local Claude Code skill:
python copy_operator_skill.py "~/.claude/skills"
# Or a chosen target project's .agents/skills (Codex) or .claude/skills (Claude):
python copy_operator_skill.py "/path/to/target/.agents/skills"
```

This creates a regular file, refuses overwrite, and makes no symlink or shell
hook. Review any existing skill before deliberately replacing it yourself.
Invoke `$bb-workflow` in Codex or `/bb-workflow` in local Claude Code. These local
files do not install a skill into Claude cloud/Cowork sessions. Official host
references: [Codex skills](https://learn.chatgpt.com/docs/build-skills) and
[Claude Code skills](https://code.claude.com/docs/en/skills).
