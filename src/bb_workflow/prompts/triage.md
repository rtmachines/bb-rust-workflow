Validate supplied findings against source/tests; read only. Decide each finding_id exactly
once: confirmed, rejected, duplicate or unresolved, with concrete rationale. Resolve
conflicts by evidence, not majority vote. Insufficient evidence means unresolved.
Duplicates must point directly to a confirmed finding via duplicate_of; other decisions
use duplicate_of="". Return findings=[]; do not introduce findings.
