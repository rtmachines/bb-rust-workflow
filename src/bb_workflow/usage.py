"""Normalize CLI token counters without treating missing measurements as zero."""

from __future__ import annotations

TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "total_tokens",
)


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _sum_known(*values: int | None) -> int | None:
    return sum(values) if all(value is not None for value in values) else None


def normalize_usage(backend: str, usage: dict) -> dict[str, int | None]:
    """Return total input (including cache), cache breakdown, output and total.

    Claude reports uncached input separately from cache reads and writes. Codex
    already includes cached input in input_tokens. Missing or malformed counters
    remain None; an exact total requires every component of that total.
    """
    counters = dict.fromkeys(TOKEN_FIELDS)
    if backend not in {"claude", "codex"}:
        return counters
    input_tokens = _count(usage.get("input_tokens"))
    output_tokens = _count(usage.get("output_tokens"))
    if backend == "claude":
        cached = _count(usage.get("cache_read_input_tokens"))
        created = _count(usage.get("cache_creation_input_tokens"))
        input_tokens = _sum_known(input_tokens, cached, created)
    else:
        cached = _count(usage.get("cached_input_tokens"))
        created = _count(usage.get("cache_write_input_tokens"))
    counters.update(
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        cache_creation_input_tokens=created,
        output_tokens=output_tokens,
        total_tokens=_sum_known(input_tokens, output_tokens),
    )
    return counters


def summarize_usage(agents: list[dict]) -> dict:
    """Group measured subtotals and missing-counter counts by backend.

    A complete call has an exact total; a partial call has some measurements but
    no total; an unknown call has none. Subtotals are never a claim that all calls
    were measured. Older records containing only raw usage are also supported.
    """
    groups: dict[str, dict] = {}
    for agent in agents:
        backend = agent.get("selection", {}).get("backend", "unknown")
        normalized = agent.get("token_usage")
        if not isinstance(normalized, dict):
            raw = agent.get("usage", {})
            normalized = normalize_usage(backend, raw if isinstance(raw, dict) else {})
        counters = {name: _count(normalized.get(name)) for name in TOKEN_FIELDS}
        group = groups.setdefault(
            backend,
            {
                "calls": 0,
                "complete_calls": 0,
                "partial_calls": 0,
                "unknown_calls": 0,
                "known_totals": dict.fromkeys(TOKEN_FIELDS),
                "missing_calls": dict.fromkeys(TOKEN_FIELDS, 0),
            },
        )
        group["calls"] += 1
        if counters["total_tokens"] is not None:
            group["complete_calls"] += 1
        elif any(value is not None for value in counters.values()):
            group["partial_calls"] += 1
        else:
            group["unknown_calls"] += 1
        for name, value in counters.items():
            if value is None:
                group["missing_calls"][name] += 1
            else:
                group["known_totals"][name] = (group["known_totals"][name] or 0) + value
    return {"by_backend": groups}
