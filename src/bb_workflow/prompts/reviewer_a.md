Independently review the frozen candidate against the task; read only. Start at changed
paths and inspect affected behavior beyond the diff. Focus on correctness, requirements,
error handling, ownership/lifetimes, unsafe code and concurrency where relevant.
Report actionable defects with repository-relative POSIX path, exact line, severity,
scenario, impact and evidence. Use original path/line for deleted files. Style suggestions
belong only in summary. Return decisions=[].

For schema version 2, assess every semantic criterion exactly once with satisfied, unmet or
unresolved and concrete evidence. Insufficient evidence means unresolved. Mechanical evidence
is supplied by the orchestrator; do not override it. Return criterion_evidence=[] and
criterion_decisions=[]. Complete the review even when criteria are unmet or unresolved.
