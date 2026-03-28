"""core_tools_task.py -- canonical task packets and task error handling.

This module keeps task management logic out of the huge facade file while still
giving CORE one authoritative place to:
- read task state in a structured packet
- terminalize task failures with consistent error metadata
- verify that task rows landed in the expected state
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any

from core_config import sb_get, sb_patch


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _safe_text(value: Any, limit: int = 240) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _coerce_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_task(row: dict) -> dict:
    raw = row.get("task", "")
    if isinstance(raw, dict):
        task = dict(raw)
    else:
        try:
            task = json.loads(raw) if raw else {}
        except Exception:
            task = {"title": _safe_text(raw, 200), "description": ""}
    task.setdefault("title", _safe_text(task.get("title") or task.get("task_id") or raw, 200))
    task.setdefault("description", _safe_text(task.get("description") or "", 1000))
    return task


def _resolve_rows(task_id: str) -> list[dict]:
    import re as _re

    if not task_id:
        return []
    is_uuid = bool(_re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", task_id.lower()))
    cols = "id,task,status,result,checkpoint,checkpoint_draft,next_step,created_at,updated_at,priority,source,error"
    if is_uuid:
        rows = sb_get("task_queue", f"select={cols}&id=eq.{task_id}&limit=1", svc=True) or []
        if rows:
            return rows

    rows = sb_get(
        "task_queue",
        f"select={cols}&order=updated_at.desc&limit=500",
        svc=True,
    ) or []
    matches: list[dict] = []
    needle = task_id.strip().lower()
    for row in rows:
        task = _parse_task(row)
        blob = json.dumps(task, default=str).lower()
        if needle == str(row.get("id", "")).lower() or needle in blob:
            matches.append(row)
    return matches


@dataclass
class TaskPacket:
    task_id: str
    row_id: Any = ""
    title: str = ""
    current_status: str = ""
    expected_status: str = ""
    kind: str = "verification"
    phase: str = ""
    error: str = ""
    retryable: bool = False
    next_step: str = ""
    source: str = ""
    priority: Any = ""
    result_present: bool = False
    checkpoint_present: bool = False
    error_present: bool = False
    verification_score: float = 0.0
    blocked: bool = False
    passed_checks: list[str] | None = None
    failed_checks: list[str] | None = None
    warnings: list[str] | None = None
    summary: str = ""
    row: dict | None = None
    task: dict | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["passed_checks"] = list(self.passed_checks or [])
        data["failed_checks"] = list(self.failed_checks or [])
        data["warnings"] = list(self.warnings or [])
        return data


def build_task_packet(
    task_id: str = "",
    expected_status: str = "",
    require_result: str = "false",
    require_checkpoint: str = "false",
) -> dict:
    if not task_id:
        return {"ok": False, "error": "task_id required"}

    try:
        rows = _resolve_rows(task_id)
        if not rows:
            packet = TaskPacket(
                task_id=task_id,
                blocked=True,
                verification_score=0.0,
                failed_checks=["task_not_found"],
                warnings=[],
                summary=f"Task not found: {task_id}",
            )
            return {"ok": True, "blocked": True, "packet": packet.to_dict(), "summary": packet.summary}

        row = rows[0]
        task = _parse_task(row)
        current_status = str(row.get("status") or "")
        expected_status = str(expected_status or "").strip()
        require_result_bool = _coerce_bool(require_result)
        require_checkpoint_bool = _coerce_bool(require_checkpoint)

        passed = ["task_found", f"current_status={current_status or 'unknown'}"]
        failed: list[str] = []
        warnings: list[str] = []

        if expected_status:
            if current_status == expected_status:
                passed.append(f"status_matches:{expected_status}")
            else:
                failed.append(f"status_mismatch:{current_status or 'missing'}!= {expected_status}")

        if require_result_bool or current_status in {"done", "failed"}:
            if row.get("result"):
                passed.append("result_present")
            else:
                failed.append("missing_result")

        if require_checkpoint_bool or current_status in {"in_progress", "done", "failed"}:
            if row.get("checkpoint") or row.get("checkpoint_draft"):
                passed.append("checkpoint_present")
            else:
                failed.append("missing_checkpoint")

        if row.get("error"):
            passed.append("error_present")

        try:
            ts_str = row.get("updated_at") or row.get("created_at") or ""
            if ts_str:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                if current_status == "in_progress" and ts < now - timedelta(hours=24):
                    warnings.append("in_progress_stale>24h")
                if current_status == "pending" and ts < now - timedelta(days=7):
                    warnings.append("pending_stale>7d")
        except Exception:
            pass

        score = len(passed) / max(1, len(passed) + len(failed))
        score = max(0.0, round(score - min(0.15, 0.02 * len(warnings)), 2))
        blocked = bool(failed) or score < 0.8
        packet = TaskPacket(
            task_id=task_id,
            row_id=row.get("id"),
            title=task.get("title") or "",
            current_status=current_status,
            expected_status=expected_status,
            kind="verification",
            next_step=_safe_text(row.get("next_step") or "", 120),
            source=_safe_text(row.get("source") or "", 80),
            priority=row.get("priority"),
            result_present=bool(row.get("result")),
            checkpoint_present=bool(row.get("checkpoint") or row.get("checkpoint_draft")),
            error_present=bool(row.get("error")),
            verification_score=score,
            blocked=blocked,
            passed_checks=passed,
            failed_checks=failed,
            warnings=warnings,
            summary=(
                f"BLOCKED: task verification failed for {task_id}"
                if blocked else
                f"CLEAR: task {task_id} verified"
            ),
            row={
                "id": row.get("id"),
                "priority": row.get("priority"),
                "source": row.get("source"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            },
            task=task,
        )
        return {
            "ok": True,
            "task_id": task_id,
            "row_id": row.get("id"),
            "current_status": current_status,
            "expected_status": expected_status or None,
            "verification_score": score,
            "blocked": blocked,
            "passed_checks": passed,
            "failed_checks": failed,
            "warnings": warnings,
            "packet": packet.to_dict(),
            "row": packet.row,
            "message": packet.summary,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "blocked": True}


def t_task_packet(
    task_id: str = "",
    expected_status: str = "",
    require_result: str = "false",
    require_checkpoint: str = "false",
) -> dict:
    return build_task_packet(
        task_id=task_id,
        expected_status=expected_status,
        require_result=require_result,
        require_checkpoint=require_checkpoint,
    )


def build_task_error_packet(
    task_id: str = "",
    error: str = "",
    phase: str = "runtime",
    summary: str = "",
    retryable: str = "false",
    next_step: str = "owner_review",
    checkpoint: str | dict = "",
) -> dict:
    if not task_id or not error:
        return {"ok": False, "error": "task_id and error required"}

    try:
        rows = _resolve_rows(task_id)
        if not rows:
            return {
                "ok": False,
                "blocked": True,
                "error": f"task not found: {task_id}",
                "summary": f"Task not found while recording error: {task_id}",
            }

        row = rows[0]
        task = _parse_task(row)
        retryable_bool = _coerce_bool(retryable)
        now = _utcnow()

        packet = TaskPacket(
            task_id=task_id,
            row_id=row.get("id"),
            title=task.get("title") or "",
            current_status=str(row.get("status") or ""),
            expected_status="failed",
            kind="error",
            phase=_safe_text(phase, 80),
            error=_safe_text(error, 500),
            retryable=retryable_bool,
            next_step=_safe_text(next_step or "owner_review", 120),
            source=_safe_text(row.get("source") or "", 80),
            priority=row.get("priority"),
            result_present=True,
            checkpoint_present=bool(checkpoint) or bool(row.get("checkpoint") or row.get("checkpoint_draft")),
            error_present=True,
            verification_score=1.0,
            blocked=False,
            passed_checks=["task_found", "failure_recorded"],
            failed_checks=[],
            warnings=[],
            summary=_safe_text(summary or error, 240),
            row={
                "id": row.get("id"),
                "status": row.get("status"),
                "source": row.get("source"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            },
            task=task,
        )

        checkpoint_payload = checkpoint if isinstance(checkpoint, dict) and checkpoint else None
        if checkpoint_payload is None:
            checkpoint_payload = {
                "task_id": task_id,
                "row_id": row.get("id"),
                "phase": packet.phase,
                "error": packet.error,
                "retryable": retryable_bool,
                "next_step": packet.next_step,
                "status": "failed",
                "ts": now,
            }
        patch = {
            "status": "failed",
            "error": packet.error,
            "result": json.dumps(packet.to_dict(), default=str)[:4000],
            "next_step": packet.next_step,
            "checkpoint": checkpoint_payload,
            "checkpoint_at": now,
            "checkpoint_draft": json.dumps(checkpoint_payload, default=str)[:4000],
            "updated_at": now,
        }

        ok = sb_patch("task_queue", f"id=eq.{row['id']}", patch)
        verification = build_task_packet(
            task_id=task_id,
            expected_status="failed",
            require_result="true",
            require_checkpoint="true" if checkpoint else "false",
        )
        return {
            "ok": bool(ok) and bool(verification.get("ok") and not verification.get("blocked")),
            "task_id": task_id,
            "row_id": row.get("id"),
            "phase": packet.phase,
            "retryable": retryable_bool,
            "next_step": packet.next_step,
            "packet": packet.to_dict(),
            "verification_packet": verification,
            "verified": bool(verification.get("ok") and not verification.get("blocked")),
            "message": f"Task failure recorded for {task_id}",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "blocked": True}


def t_task_error_packet(
    task_id: str = "",
    error: str = "",
    phase: str = "runtime",
    summary: str = "",
    retryable: str = "false",
    next_step: str = "owner_review",
    checkpoint: str | dict = "",
) -> dict:
    return build_task_error_packet(
        task_id=task_id,
        error=error,
        phase=phase,
        summary=summary,
        retryable=retryable,
        next_step=next_step,
        checkpoint=checkpoint,
    )

