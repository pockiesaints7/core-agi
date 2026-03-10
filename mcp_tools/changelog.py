"""
Jarvis Changelog Router v1.0
=============================
Permanent audit trail for all system changes.
Every upgrade, bugfix, schema change, audit, and growth-flag action gets an entry here.
Never deleted. Never overwritten. Append-only history.

Endpoints:
  POST /changelog/add       - add an entry
  GET  /changelog/list      - query entries (filter by component, change_type, limit)
  GET  /changelog/summary   - count by type/component for audit phase 1
"""

from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional, List
from modules.db import sql, dq
from datetime import datetime, timezone

router = APIRouter(tags=["changelog"])


class ChangelogEntry(BaseModel):
    version: str = ""                     # semver: 2.1.0  MAJOR=schema, MINOR=feature, PATCH=fix
    change_type: str                       # upgrade / bugfix / schema / audit / growth / manual
    component: str = ""                   # api / brain / vault / prompt / scanner / skill_file
    title: str                             # short description
    description: str = ""                 # full detail
    triggered_by: str = ""                # growth_flag / audit / manual / scanner
    growth_flag_type: str = ""            # nullable -- which flag triggered this
    before_state: str = ""                # what it was before
    after_state: str = ""                 # what it is after
    files_changed: List[str] = []         # files / endpoints modified
    session_id: Optional[int] = None      # FK sessions.id if known


@router.post("/add")
async def add_changelog_entry(entry: ChangelogEntry):
    """Add a changelog entry. Every system change should call this."""
    try:
        files_sql = "{" + ",".join(f'"{f}"' for f in entry.files_changed) + "}"
        sid = str(entry.session_id) if entry.session_id else "NULL"
        q = f"""
        INSERT INTO changelog
          (version, change_type, component, title, description,
           triggered_by, growth_flag_type, before_state, after_state, files_changed, session_id)
        VALUES
          ({dq(entry.version)}, {dq(entry.change_type)}, {dq(entry.component)}, {dq(entry.title)},
           {dq(entry.description)}, {dq(entry.triggered_by)}, {dq(entry.growth_flag_type)},
           {dq(entry.before_state)}, {dq(entry.after_state)}, '{files_sql}', {sid})
        RETURNING id, created_at
        """
        result = await sql(q)
        row = result[0] if result else {}
        return {"ok": True, "id": row.get("id"), "created_at": row.get("created_at")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/list")
async def list_changelog(
    limit: int = Query(default=20, le=200),
    change_type: str = Query(default=""),
    component: str = Query(default=""),
    triggered_by: str = Query(default=""),
):
    """Query changelog entries. Supports filter by change_type, component, triggered_by."""
    try:
        conditions = []
        if change_type:
            conditions.append(f"change_type = {dq(change_type)}")
        if component:
            conditions.append(f"component = {dq(component)}")
        if triggered_by:
            conditions.append(f"triggered_by = {dq(triggered_by)}")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        q = f"SELECT * FROM changelog {where} ORDER BY created_at DESC LIMIT {limit}"
        rows = await sql(q)
        return {"ok": True, "count": len(rows or []), "entries": rows or []}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/summary")
async def changelog_summary():
    """Counts by type and component. Used in audit phase 1 for pattern review."""
    try:
        by_type = await sql("SELECT change_type, COUNT(*) as count FROM changelog GROUP BY change_type ORDER BY count DESC")
        by_component = await sql("SELECT component, COUNT(*) as count FROM changelog GROUP BY component ORDER BY count DESC")
        by_trigger = await sql("SELECT triggered_by, COUNT(*) as count FROM changelog GROUP BY triggered_by ORDER BY count DESC")
        total = await sql("SELECT COUNT(*) as total FROM changelog")
        return {
            "ok": True,
            "total": total[0]["total"] if total else 0,
            "by_type": by_type or [],
            "by_component": by_component or [],
            "by_trigger": by_trigger or [],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
