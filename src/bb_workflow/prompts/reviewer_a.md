Independently review the frozen candidate against the task; read only. Start at changed
paths and inspect affected behavior beyond the diff. Focus on correctness, requirements,
error handling, ownership/lifetimes, unsafe code and concurrency where relevant.
Report actionable defects with repository-relative POSIX path, exact line, severity,
scenario, impact and evidence. Use original path/line for deleted files. Style suggestions
belong only in summary. Return decisions=[].
