Validate supplied findings against source/tests; read only. Decide each finding_id exactly
once: confirmed, rejected, duplicate or unresolved, with concrete rationale. Resolve
conflicts by evidence, not majority vote. Insufficient evidence means unresolved.
Duplicates must point directly to a confirmed finding via duplicate_of; other decisions
use duplicate_of="". Return findings=[]; do not introduce findings.

For schema version 2, also decide each disputed_criteria ID exactly once in criterion_decisions
with satisfied, unmet or unresolved and a concrete rationale. Criterion decisions are separate
from finding decisions, including when findings=[]. Use both independent assessments and source
evidence. Never override mechanical results. Return criterion_evidence=[] and
criterion_assessments=[]. A completed triage may leave criteria unmet or unresolved.
