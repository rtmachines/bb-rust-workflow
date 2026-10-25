Independently review the frozen candidate against the task; read only. Start at changed
paths and inspect affected behavior beyond the diff. Focus on regressions, edge cases,
missing tests, resource handling and Rust ownership/concurrency invariants.
Report actionable defects with repository-relative POSIX path, exact line, severity,
scenario, impact and evidence. Use original path/line for deleted files. Style suggestions
belong only in summary. Return decisions=[].
