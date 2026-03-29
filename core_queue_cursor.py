"""core_queue_cursor.py -- keyset pagination helpers for queue workers."""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote


OrderSpec = tuple[str, str]


def _safe_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    return quote(str(value), safe="-_.:T")


def _branch(filters: list[str]) -> str:
    if not filters:
        return ""
    if len(filters) == 1:
        return filters[0]
    return f"and({','.join(filters)})"


def cursor_from_row(row: Mapping[str, Any], order: Sequence[OrderSpec]) -> dict[str, Any]:
    return {field: row.get(field) for field, _ in order}


def build_seek_filter(cursor: Mapping[str, Any] | None, order: Sequence[OrderSpec]) -> str:
    """Return a PostgREST keyset filter that selects rows after `cursor`."""
    if not cursor or not order:
        return ""

    def recurse(idx: int, equal_prefix: list[str]) -> list[str]:
        if idx >= len(order):
            return []
        field, direction = order[idx]
        value = cursor.get(field)
        if value in (None, ""):
            return []
        op = "gt" if str(direction).lower() == "asc" else "lt"
        encoded = _safe_value(value)
        clauses = [_branch(equal_prefix + [f"{field}.{op}.{encoded}"])]
        clauses.extend(recurse(idx + 1, equal_prefix + [f"{field}.eq.{encoded}"]))
        return [clause for clause in clauses if clause]

    clauses = recurse(0, [])
    return f"or=({','.join(clauses)})" if clauses else ""

