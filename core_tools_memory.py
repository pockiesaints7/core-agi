"""core_tools_memory.py — reasoning packet + unified memory search + StateEvaluator.

This module is intentionally kept free of imports from core_tools.py (facade) and
other tool-family modules to avoid circular imports.
"""

from __future__ import annotations

import json
import re as _re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from core_config import SUPABASE_URL, _sbh_count_svc, sb_get, sb_patch


def _group_memory_hits(rows: list) -> dict:
    grouped: dict = {}
    for row in rows or []:
        table = row.get("semantic_table") or row.get("_table") or "knowledge_base"
        grouped.setdefault(table, []).append(row)
    return grouped


def t_search_memory(query: str = "", domain: str = "", limit: int = 10, tables: str = "") -> dict:
    """Unified semantic memory search across KB + native semantic tables."""
    try:
        if not query:
            return {"ok": False, "error": "query required", "results": []}
        lim = max(1, min(int(limit) if limit else 10, 50))
        table_list = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
        from core_semantic import search_many

        rows = search_many(query, tables=table_list, limit=lim, domain=domain) or []
        grouped = _group_memory_hits(rows)
        try:
            kb_ids = [
                str(r["id"])
                for r in grouped.get("knowledge_base", [])
                if r.get("id") and r["id"] != 1
            ]
            if kb_ids:
                sb_patch(
                    "knowledge_base",
                    f"id=in.({','.join(kb_ids)})",
                    {"last_accessed": datetime.utcnow().isoformat()},
                )
        except Exception:
            pass
        return {
            "ok": True,
            "query": query,
            "domain": domain or "",
            "count": len(rows),
            "results": rows,
            "by_table": {k: len(v) for k, v in grouped.items()},
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "results": []}


def t_reasoning_packet(
    query: str = "",
    domain: str = "",
    limit: str = "10",
    tables: str = "",
    per_table: str = "2",
) -> dict:
    """Build the canonical reasoning packet for a query (single unified memory read)."""
    try:
        if not query:
            return {"ok": False, "error": "query required"}
        try:
            lim = max(1, min(int(limit), 50))
        except Exception:
            lim = 10
        try:
            pt = max(1, min(int(per_table), 5))
        except Exception:
            pt = 2
        table_list = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
        from core_reasoning_packet import build_reasoning_packet

        return build_reasoning_packet(
            query=query,
            domain=domain,
            tables=table_list,
            limit=lim,
            per_table=pt,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_generate_synthetic_data(
    context: str = "",
    goal: str = "",
    principles: str = "",
    domain: str = "general",
    state_hint: str = "",
    limit: str = "8",
) -> dict:
    """Generate bounded synthetic training samples for memory modules.

    The output includes a PrincipleUtilityScore field so downstream memory or
    policy components can quickly rank the usefulness of the sampled principles.
    """
    try:
        if not context and not goal and not principles:
            return {"ok": False, "error": "context, goal, or principles required"}
        try:
            lim = max(1, min(int(limit or 8), 16))
        except Exception:
            lim = 8

        context_text = " ".join([part.strip() for part in [context, goal, state_hint] if str(part or "").strip()]).strip()
        context_tokens = [tok for tok in _re.split(r"[^A-Za-z0-9_]+", context_text.lower()) if len(tok) >= 3]
        principle_list = [part.strip() for part in str(principles or "").replace("\n", ",").split(",") if part.strip()]
        if not principle_list:
            principle_list = ["verify_before_close", "evidence_before_action", "prefer_small_safe_changes"]

        synthetic_rows = []
        principle_scores = []
        for idx, principle in enumerate(principle_list[:lim]):
            principle_tokens = [tok for tok in _re.split(r"[^A-Za-z0-9_]+", principle.lower()) if len(tok) >= 3]
            overlap = len(set(context_tokens) & set(principle_tokens))
            utility = round(min(1.0, 0.25 + (0.12 * overlap) + (0.06 if "verify" in principle.lower() else 0.0)), 3)
            principle_scores.append({
                "principle": principle,
                "PrincipleUtilityScore": utility,
                "index": idx,
                "overlap": overlap,
            })
            synthetic_rows.append({
                "id": f"synthetic-{idx+1}",
                "context": context_text[:240],
                "goal": goal[:160],
                "principle": principle,
                "PrincipleUtilityScore": utility,
                "source": "memory_synthesis",
                "domain": domain,
                "state_hint": state_hint,
            })

        principle_scores.sort(key=lambda item: (item["PrincipleUtilityScore"], item["principle"]), reverse=True)
        best = principle_scores[0] if principle_scores else {}
        utility_mean = round(sum(item["PrincipleUtilityScore"] for item in principle_scores) / len(principle_scores), 3) if principle_scores else 0.0
        summary = f"synthetic_data=ok | best_principle={best.get('principle') or 'none'} | utility={utility_mean:.2f} | samples={len(synthetic_rows)}"
        return {
            "ok": True,
            "domain": domain,
            "state_hint": state_hint,
            "context": context_text[:300],
            "goal": goal[:200],
            "principles": principle_list[:lim],
            "principle_scores": principle_scores,
            "synthetic_rows": synthetic_rows,
            "PrincipleUtilityScore": utility_mean,
            "best_principle": best.get("principle"),
            "summary": summary,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _safe_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _safe_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _count_rows(table: str, filters: str = "") -> int:
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?select=id&limit=1"
        if filters:
            url += f"&{filters}"
        r = httpx.get(url, headers=_sbh_count_svc(), timeout=10)
        cr = r.headers.get("content-range", "*/0")
        return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return -1


def _latest_agentic_session(session_id: str = "default") -> dict:
    try:
        filters = f"session_id=eq.{session_id}" if session_id else ""
        q = "select=id,session_id,state,step_index,current_step,completed_steps,action_log,last_updated,goal,status,chat_id,created_at&order=created_at.desc&limit=1"
        rows = sb_get("agentic_sessions", f"{filters}&{q}" if filters else q, svc=True) or []
        if not rows and session_id in ("", "default"):
            rows = sb_get(
                "agentic_sessions",
                "select=id,session_id,state,step_index,current_step,completed_steps,action_log,last_updated,goal,status,chat_id,created_at&order=created_at.desc&limit=1",
                svc=True,
            ) or []
        if not rows:
            return {"ok": False, "found": False, "row": None}
        row = rows[0]
        row["state"] = _safe_dict(row.get("state"))
        row["completed_steps"] = _safe_list(row.get("completed_steps"))
        row["action_log"] = _safe_list(row.get("action_log"))
        return {"ok": True, "found": True, "row": row}
    except Exception as e:
        return {"ok": False, "found": False, "error": str(e), "row": None}


def _latest_checkpoint() -> dict:
    try:
        rows = sb_get(
            "sessions",
            "select=id,summary,checkpoint_data,checkpoint_ts,created_at&order=created_at.desc&limit=5",
            svc=True,
        ) or []
        for row in rows:
            checkpoint = _safe_dict(row.get("checkpoint_data"))
            if checkpoint:
                return {
                    "ok": True,
                    "found": True,
                    "session_id": row.get("id"),
                    "checkpoint": checkpoint,
                    "checkpoint_ts": row.get("checkpoint_ts"),
                    "created_at": row.get("created_at"),
                }
        return {"ok": True, "found": False, "checkpoint": None}
    except Exception as e:
        return {"ok": False, "found": False, "error": str(e), "checkpoint": None}


def _collect_state_updates(limit: int = 20) -> dict:
    try:
        lim = max(1, min(int(limit or 20), 50))
    except Exception:
        lim = 20
    try:
        rows = sb_get(
            "sessions",
            f"select=summary,created_at&summary=like.*%5Bstate_update%5D*&order=created_at.desc&limit={lim}",
            svc=True,
        ) or []
    except Exception:
        rows = []

    updates: dict[str, str] = {}
    ordered: list[dict] = []
    for row in rows:
        raw = row.get("summary") or ""
        if not isinstance(raw, str) or "[state_update]" not in raw:
            continue
        payload = raw.split("[state_update]", 1)[-1].strip()
        if ": " in payload:
            key, value = payload.split(": ", 1)
        else:
            key, value = payload, ""
        key = key.strip()
        value = value.strip()
        if key and key not in updates:
            updates[key] = value
        ordered.append({
            "key": key,
            "value": value,
            "created_at": row.get("created_at"),
        })
    return {"latest": updates, "rows": ordered, "count": len(ordered)}


def _parse_utc_timestamp(value) -> tuple[datetime | None, bool]:
    """Parse an ISO-like timestamp into a UTC datetime."""
    if value in (None, "", {}, []):
        return None, False
    text = str(value).strip()
    if not text:
        return None, False
    text = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        try:
            dt = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None, False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt, True


def _state_update_timestamp_guard(state_updates: dict, rows: list | None = None) -> dict:
    """Normalize timestamp-like state updates and flag future timestamps."""
    state_updates = state_updates if isinstance(state_updates, dict) else {}
    rows = rows if isinstance(rows, list) else []
    now = datetime.now(timezone.utc)
    checked_keys: list[str] = []
    future_keys: list[str] = []
    future_key_set: set[str] = set()
    normalized_values: dict[str, object] = {}
    warnings: list[str] = []

    for key, value in state_updates.items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        checked_keys.append(key_text)
        dt, parsed = _parse_utc_timestamp(value)
        if parsed and ("_ts" in key_text or "timestamp" in key_text.lower()):
            if dt and dt > now:
                if key_text not in future_key_set:
                    future_key_set.add(key_text)
                    future_keys.append(key_text)
                    warnings.append(f"{key_text}_future_timestamp_clamped")
                normalized_values[key_text] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                continue
            if dt:
                normalized_values[key_text] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                continue
        normalized_values[key_text] = value

    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        value = row.get("value")
        dt, parsed = _parse_utc_timestamp(value)
        if parsed and ("_ts" in key or "timestamp" in key.lower()) and dt and dt > now:
            key_name = key or "unknown"
            if key_name not in future_key_set:
                future_key_set.add(key_name)
                future_keys.append(key_name)
                warnings.append(f"{key_name}_future_timestamp_clamped")

    return {
        "checked_keys": checked_keys,
        "future_keys": future_keys,
        "warnings": warnings,
        "normalized_values": normalized_values,
        "future_count": len(future_keys),
    }


def _latest_session_snapshot_raw() -> dict:
    try:
        rows = sb_get(
            "sessions",
            "select=summary,created_at&summary=like.*%5Bstate_update%5D+session_snapshot:*&order=created_at.desc&limit=1",
            svc=True,
        ) or []
        if not rows:
            return {"ok": False, "found": False, "snapshot": None, "created_at": None}
        raw = rows[0].get("summary", "") or ""
        prefix = "[state_update] session_snapshot: "
        payload = raw[len(prefix):].strip() if raw.startswith(prefix) else raw
        try:
            snapshot = json.loads(payload)
        except Exception:
            snapshot = {"raw": payload}
        return {
            "ok": True,
            "found": True,
            "snapshot": snapshot,
            "created_at": rows[0].get("created_at"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "found": False, "snapshot": None}


def _compare_session_continuity(
    latest_session: dict,
    agentic_row: dict,
    checkpoint_row: dict,
    snapshot_row: dict,
    state_updates: dict,
) -> dict:
    """Compare the latest continuity surfaces and surface drift explicitly."""
    latest_session = latest_session if isinstance(latest_session, dict) else {}
    agentic_row = agentic_row if isinstance(agentic_row, dict) else {}
    checkpoint_row = checkpoint_row if isinstance(checkpoint_row, dict) else {}
    snapshot_row = snapshot_row if isinstance(snapshot_row, dict) else {}
    state_updates = state_updates if isinstance(state_updates, dict) else {}

    drift = []
    passed = []
    warnings = []

    resume_task = snapshot_row.get("resume_task") if isinstance(snapshot_row.get("resume_task"), dict) else {}
    latest_resume_task = _safe_dict(latest_session.get("resume_task"))
    checkpoint = _safe_dict(snapshot_row.get("checkpoint") or checkpoint_row)

    if latest_session:
        passed.append("latest_session_present")
    else:
        drift.append("latest_session_missing")

    if agentic_row:
        passed.append("agentic_session_present")
    else:
        warnings.append("agentic_session_missing")

    if checkpoint:
        passed.append("checkpoint_present")
    else:
        warnings.append("checkpoint_missing")

    if snapshot_row:
        passed.append("session_snapshot_present")
    else:
        warnings.append("session_snapshot_missing")

    if latest_resume_task and resume_task:
        if str(latest_resume_task.get("id") or "") != str(resume_task.get("id") or ""):
            drift.append("resume_task_id_changed")
        else:
            passed.append("resume_task_consistent")
    elif latest_resume_task or resume_task:
        warnings.append("resume_task_partial")

    latest_count = latest_session.get("counts") if isinstance(latest_session.get("counts"), dict) else {}
    snapshot_count = snapshot_row.get("counts") if isinstance(snapshot_row.get("counts"), dict) else {}
    if latest_count and snapshot_count:
        if latest_count != snapshot_count:
            drift.append("counts_differ")
        else:
            passed.append("counts_consistent")

    state_update_keys = sorted((state_updates or {}).keys())
    if state_update_keys:
        passed.append("state_updates_present")
    else:
        warnings.append("state_updates_missing")

    verification_score = 1.0
    if drift:
        verification_score -= min(0.45, 0.15 * len(drift))
    if warnings:
        verification_score -= min(0.3, 0.05 * len(warnings))
    verification_score = round(max(0.0, verification_score), 3)
    blocked = bool(drift and verification_score < 0.7)
    return {
        "verified": verification_score >= 0.7,
        "blocked": blocked,
        "verification_score": verification_score,
        "passed_checks": passed,
        "failed_checks": drift,
        "warnings": warnings,
        "summary": (
            f"continuity={'ok' if verification_score >= 0.7 else 'degraded'} | "
            f"drift={len(drift)} | warnings={len(warnings)} | state_updates={len(state_update_keys)}"
        ),
    }


@dataclass
class StatePacket:
    session_id: str
    latest_session: dict
    agentic_session: dict
    checkpoint: dict
    session_snapshot: dict
    session_continuity: dict
    state_updates: dict
    state_update_rows: list
    state_update_integrity: dict
    counts: dict
    verification: dict

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "latest_session": self.latest_session,
            "agentic_session": self.agentic_session,
            "checkpoint": self.checkpoint,
            "session_snapshot": self.session_snapshot,
            "session_continuity": self.session_continuity,
            "state_updates": self.state_updates,
            "state_update_rows": self.state_update_rows,
            "state_update_integrity": self.state_update_integrity,
            "counts": self.counts,
            "verification": self.verification,
        }


def _build_state_packet(session_id: str = "default", strict: bool = False) -> StatePacket:
    latest_session_rows = sb_get(
        "sessions",
        "select=id,summary,actions,created_at,interface,checkpoint_data,checkpoint_ts,resume_task,quality_score,domain&order=created_at.desc&limit=1",
        svc=True,
    ) or []
    latest_session = latest_session_rows[0] if latest_session_rows else {}
    latest_session["actions"] = _safe_list(latest_session.get("actions"))
    latest_session["checkpoint_data"] = _safe_dict(latest_session.get("checkpoint_data"))

    agentic = _latest_agentic_session(session_id=session_id)
    agentic_row = agentic.get("row") or {}
    checkpoint = _latest_checkpoint()
    checkpoint_row = checkpoint.get("checkpoint") or {}
    state_updates = _collect_state_updates(limit=20)
    state_update_integrity = _state_update_timestamp_guard(
        state_updates.get("latest", {}),
        state_updates.get("rows", []),
    )
    snapshot = _latest_session_snapshot_raw()
    snapshot_row = _safe_dict(snapshot.get("snapshot"))
    session_continuity = _compare_session_continuity(
        latest_session=latest_session,
        agentic_row=agentic_row,
        checkpoint_row=checkpoint_row,
        snapshot_row=snapshot_row,
        state_updates=state_update_integrity.get("normalized_values", state_updates.get("latest", {})),
    )

    counts = {
        "sessions": _count_rows("sessions"),
        "agentic_sessions": _count_rows("agentic_sessions"),
        "task_pending": _count_rows("task_queue", "status=eq.pending"),
        "task_in_progress": _count_rows("task_queue", "status=eq.in_progress"),
        "task_done": _count_rows("task_queue", "status=eq.done"),
        "task_failed": _count_rows("task_queue", "status=eq.failed"),
        "evolution_pending": _count_rows("evolution_queue", "status=eq.pending"),
        "evolution_applied": _count_rows("evolution_queue", "status=eq.applied"),
        "evolution_rejected": _count_rows("evolution_queue", "status=eq.rejected"),
    }

    passed_checks = []
    failed_checks = []
    warnings = []

    if latest_session:
        passed_checks.append("latest_session_found")
    else:
        failed_checks.append("latest_session_missing")

    if agentic.get("found") and isinstance(agentic_row.get("state"), dict):
        passed_checks.append("agentic_state_dict")
    else:
        failed_checks.append("agentic_state_missing")

    if checkpoint.get("found") and isinstance(checkpoint_row, dict):
        passed_checks.append("checkpoint_available")
    else:
        warnings.append("checkpoint_missing_or_empty")

    if state_updates.get("count", 0) > 0:
        passed_checks.append("state_updates_present")
    else:
        warnings.append("no_state_updates_found")

    if state_update_integrity.get("future_count", 0) > 0:
        warnings.append("future_state_timestamps_clamped")

    if counts.get("sessions", -1) >= 0 and counts.get("agentic_sessions", -1) >= 0:
        passed_checks.append("counts_available")
    else:
        warnings.append("count_lookup_issue")

    coverage = len(passed_checks) + len(failed_checks)
    verification_score = round(len(passed_checks) / max(1, coverage), 3)
    blocked = bool(strict and failed_checks)
    verified = verification_score >= 0.6 and not blocked
    summary = (
        f"session={'ok' if latest_session else 'missing'} | "
        f"agentic={'ok' if agentic.get('found') else 'missing'} | "
        f"checkpoint={'ok' if checkpoint.get('found') else 'missing'} | "
        f"state_updates={state_updates.get('count', 0)}"
    )

    verification = {
        "verified": verified,
        "blocked": blocked,
        "verification_score": verification_score,
        "passed_checks": passed_checks,
        "failed_checks": failed_checks,
        "warnings": warnings + state_update_integrity.get("warnings", []),
        "summary": summary,
    }

    return StatePacket(
        session_id=session_id,
        latest_session=latest_session,
        agentic_session=agentic_row,
        checkpoint=checkpoint_row,
        session_snapshot=snapshot_row,
        session_continuity=session_continuity,
        state_updates=state_updates.get("latest", {}),
        state_update_rows=state_updates.get("rows", []),
        state_update_integrity=state_update_integrity,
        counts=counts,
        verification=verification,
    )


def t_state_packet(
    session_id: str = "default",
    strict: str = "false",
) -> dict:
    """Canonical state packet for continuity, checkpoints, and verification."""
    try:
        packet = _build_state_packet(
            session_id=session_id or "default",
            strict=str(strict).strip().lower() in ("true", "1", "yes"),
        ).to_dict()
        return {"ok": True, **packet}
    except Exception as e:
        return {"ok": False, "error": str(e), "session_id": session_id or "default"}


def t_state_consistency_check(
    session_id: str = "default",
    strict: str = "false",
) -> dict:
    """Lightweight verification wrapper over the canonical state packet."""
    try:
        packet = t_state_packet(session_id=session_id or "default", strict=strict)
        if not packet.get("ok"):
            return packet
        verification = packet.get("verification") or {}
        return {
            "ok": True,
            "session_id": packet.get("session_id") or (session_id or "default"),
            "verified": verification.get("verified", False),
            "blocked": verification.get("blocked", False),
            "verification_score": verification.get("verification_score", 0.0),
            "passed_checks": verification.get("passed_checks", []),
            "failed_checks": verification.get("failed_checks", []),
            "warnings": verification.get("warnings", []),
            "summary": verification.get("summary", ""),
            "session_continuity": packet.get("session_continuity") or {},
            "state_packet": packet,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "session_id": session_id or "default"}


@dataclass
class SystemVerificationPacket:
    session_id: str
    state_packet: dict
    task_verification: dict
    changelog_verification: dict
    counts: dict
    verification: dict

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "state_packet": self.state_packet,
            "task_verification": self.task_verification,
            "changelog_verification": self.changelog_verification,
            "counts": self.counts,
            "verification": self.verification,
        }


def _build_system_verification_packet(
    session_id: str = "default",
    strict: bool = False,
    require_checkpoint: bool = False,
    task_sample_limit: int = 5,
    changelog_limit: int = 5,
) -> SystemVerificationPacket:
    """Aggregate CORE verification surfaces into one system-wide packet."""
    state = _build_state_packet(session_id=session_id or "default", strict=strict).to_dict()
    state_ver = state.get("verification") or {}
    latest_session = state.get("latest_session") or {}
    session_snapshot = state.get("session_snapshot") or {}
    session_continuity = state.get("session_continuity") or {}
    counts = state.get("counts") or {}

    task_counts = {
        "task_pending": int(counts.get("task_pending") or 0),
        "task_in_progress": int(counts.get("task_in_progress") or 0),
        "task_done": int(counts.get("task_done") or 0),
        "task_failed": int(counts.get("task_failed") or 0),
    }
    evo_counts = {
        "evolution_pending": int(counts.get("evolution_pending") or 0),
        "evolution_applied": int(counts.get("evolution_applied") or 0),
        "evolution_rejected": int(counts.get("evolution_rejected") or 0),
    }
    task_total = sum(task_counts.values())
    evo_total = sum(evo_counts.values())
    task_balance = 0.0 if task_total == 0 else round((task_counts["task_done"] + task_counts["task_failed"]) / task_total, 3)
    evo_balance = 0.0 if evo_total == 0 else round((evo_counts["evolution_applied"] + evo_counts["evolution_rejected"]) / evo_total, 3)

    # Sample a few task rows and verify the most recent one if possible.
    try:
        task_rows = sb_get(
            "task_queue",
            f"select=id,task,status,result,checkpoint,priority,source,updated_at&order=updated_at.desc&limit={max(1, min(int(task_sample_limit or 5), 20))}",
            svc=True,
        ) or []
    except Exception:
        task_rows = []
    latest_task = task_rows[0] if task_rows else {}
    task_verification = {
        "ok": True,
        "latest_task_id": latest_task.get("id"),
        "latest_task_status": latest_task.get("status"),
        "latest_task_source": latest_task.get("source"),
        "latest_task_has_result": bool(latest_task.get("result")),
        "latest_task_has_checkpoint": bool(latest_task.get("checkpoint")),
        "task_counts": task_counts,
    }
    if latest_task and require_checkpoint and not latest_task.get("checkpoint"):
        task_verification["blocked"] = True
        task_verification["warnings"] = ["latest_task_missing_checkpoint"]
    else:
        task_verification["blocked"] = False

    # Changelog verification is kept local here to avoid facade import cycles.
    try:
        lim = max(1, min(int(changelog_limit or 5), 10))
    except Exception:
        lim = 5
    try:
        rows = sb_get(
            "changelog",
            f"select=id,version,change_type,component,title,description,before_state,after_state,triggered_by,created_at&order=created_at.desc&limit={lim}",
            svc=True,
        ) or []
        normalized = []
        missing_triggered = 0
        missing_fields = 0
        completeness_total = 0.0
        for row in rows:
            title = str(row.get("title") or row.get("description") or "").strip()
            comp = str(row.get("component") or "general").strip()
            ctype = str(row.get("change_type") or "unknown").strip()
            ver = str(row.get("version") or "?").strip()
            row_missing = []
            for key in ("version", "change_type", "component", "title", "description", "before_state", "after_state", "triggered_by", "created_at"):
                if not str(row.get(key) or "").strip():
                    row_missing.append(key)
            if row_missing:
                missing_fields += len(row_missing)
            if not row.get("triggered_by"):
                missing_triggered += 1
            completeness = round((9 - len(row_missing)) / 9, 2)
            completeness_total += completeness
            normalized.append({
                **row,
                "_display_line": f"{ver} | {ctype} | {comp} | {title or 'Untitled changelog entry'}",
                "_missing_fields": row_missing,
                "_row_completeness": completeness,
            })
        tracking_score = 0.0
        if rows:
            tracking_score += 0.6
        if missing_triggered == 0:
            tracking_score += 0.2
        if missing_fields == 0:
            tracking_score += 0.1
        if len(rows) >= 2:
            tracking_score += 0.1
        tracking_score = max(0.0, min(1.0, round(tracking_score, 2)))
        changelog_verification = {
            "ok": True,
            "tracking_state": "healthy" if rows and missing_fields == 0 and missing_triggered == 0 else ("degraded" if rows else "empty"),
            "stalled": not rows,
            "tracking_score": tracking_score,
            "packet": {
                "total_rows": len(rows),
                "today_rows": sum(1 for row in rows if str(row.get("created_at") or "").startswith(datetime.utcnow().date().isoformat())),
                "verified_rows": len(rows) - missing_triggered,
                "missing_triggered_by_rows": missing_triggered,
                "missing_fields_rows": sum(1 for row in normalized if row.get("_missing_fields")),
                "missing_fields_total": missing_fields,
                "row_completeness": round(completeness_total / max(1, len(normalized)), 2) if normalized else 0.0,
                "rows": rows,
                "normalized_rows": normalized,
            },
            "warnings": [f"missing_triggered_by:{row.get('id')}" for row in rows if not row.get("triggered_by")],
            "blocked": False,
            "message": f"CHANGELOG: {('healthy' if rows and missing_fields == 0 and missing_triggered == 0 else ('degraded' if rows else 'empty'))}",
        }
    except Exception as exc:
        changelog_verification = {
            "ok": False,
            "error": str(exc),
            "tracking_state": "error",
            "tracking_score": 0.0,
            "blocked": True,
            "packet": {},
            "warnings": [str(exc)],
        }

    verification_score = 1.0
    if not state_ver.get("verified"):
        verification_score -= 0.20
    if state_ver.get("blocked"):
        verification_score -= 0.10
    if task_verification.get("blocked"):
        verification_score -= 0.20
    if not changelog_verification.get("ok", False):
        verification_score -= 0.15
    else:
        verification_score = (verification_score + float(changelog_verification.get("tracking_score") or 0.0)) / 2.0
    if not session_continuity.get("verified", False):
        verification_score -= 0.10
    verification_score = round(max(0.0, min(1.0, verification_score)), 3)
    blocked = bool(
        state_ver.get("blocked")
        or task_verification.get("blocked")
        or changelog_verification.get("blocked")
        or verification_score < 0.75
    )

    return SystemVerificationPacket(
        session_id=session_id or "default",
        state_packet=state,
        task_verification=task_verification,
        changelog_verification=changelog_verification,
        counts={
            **counts,
            "task_total": task_total,
            "task_balance": task_balance,
            "evolution_total": evo_total,
            "evolution_balance": evo_balance,
        },
        verification={
            "verified": verification_score >= 0.75 and not blocked,
            "blocked": blocked,
            "verification_score": verification_score,
            "passed_checks": [
                "state_verified" if state_ver.get("verified") else "state_partial",
                "session_continuity_verified" if session_continuity.get("verified") else "session_continuity_degraded",
                "task_verification_present",
                "changelog_verification_present",
            ],
            "failed_checks": [
                name for name, cond in [
                    ("state_blocked", bool(state_ver.get("blocked"))),
                    ("task_blocked", bool(task_verification.get("blocked"))),
                    ("changelog_blocked", bool(changelog_verification.get("blocked"))),
                ] if cond
            ],
            "warnings": [
                *list(state_ver.get("warnings") or []),
                *list(changelog_verification.get("warnings") or []),
                *list(task_verification.get("warnings") or []),
            ],
            "summary": (
                f"system_verification={'ok' if verification_score >= 0.75 and not blocked else 'degraded'} | "
                f"state={state_ver.get('verification_score', 0.0):.2f} | "
                f"changelog={(changelog_verification.get('tracking_score') if isinstance(changelog_verification, dict) else 0.0) or 0.0:.2f} | "
                f"tasks={task_balance:.2f}"
            ),
        },
    )


def t_system_verification_packet(
    session_id: str = "default",
    strict: str = "false",
    require_checkpoint: str = "false",
    task_sample_limit: str = "5",
    changelog_limit: str = "5",
) -> dict:
    """Canonical system-wide verification packet for CORE."""
    try:
        pkt = _build_system_verification_packet(
            session_id=session_id or "default",
            strict=str(strict).strip().lower() in ("true", "1", "yes"),
            require_checkpoint=str(require_checkpoint).strip().lower() in ("true", "1", "yes"),
            task_sample_limit=int(task_sample_limit or 5),
            changelog_limit=int(changelog_limit or 5),
        ).to_dict()
        return {"ok": True, **pkt}
    except Exception as e:
        return {"ok": False, "error": str(e), "session_id": session_id or "default"}


class StateEvaluator:
    """Evaluate an environment or system state using unified memory context."""

    def __init__(
        self,
        query: str,
        domain: str = "general",
        tables: list | None = None,
        limit: int = 10,
        per_table: int = 2,
        state_hint: str = "",
    ):
        self.query = (query or "").strip()
        self.domain = (domain or "general").strip()
        self.tables = tables
        self.limit = max(1, min(int(limit or 10), 50))
        self.per_table = max(1, min(int(per_table or 2), 5))
        self.state_hint = (state_hint or "").strip()

    @staticmethod
    def _risk_markers(text: str) -> int:
        text = (text or "").lower()
        markers = [
            "error",
            "failed",
            "fail",
            "broken",
            "degraded",
            "stale",
            "blocked",
            "collision",
            "conflict",
            "missing",
            "invalid",
            "unauthorized",
            "crash",
            "traceback",
            "timeout",
        ]
        return sum(1 for m in markers if m in text)

    def evaluate(self) -> dict:
        from core_reasoning_packet import build_reasoning_packet

        packet = build_reasoning_packet(
            self.query,
            domain=self.domain,
            tables=self.tables,
            limit=self.limit,
            per_table=self.per_table,
        )
        pkt = packet.get("packet") or {}
        hits = pkt.get("top_hits") or []
        by_table = pkt.get("memory_by_table") or {}
        focus = pkt.get("focus", "")
        context = pkt.get("context", "")

        table_support = len([k for k, v in by_table.items() if int(v or 0) > 0])
        evidence_count = len(hits)
        risk_markers = self._risk_markers(
            " ".join(
                [
                    self.query,
                    self.state_hint,
                    focus,
                    context,
                    " ".join(h.get("title", "") for h in hits[:5]),
                    " ".join(h.get("body", "") for h in hits[:5]),
                ]
            )
        )

        coherence_score = round(min(1.0, 0.35 + (table_support * 0.08) + (evidence_count * 0.03)), 3)
        evidence_score = round(min(1.0, 0.25 + (evidence_count * 0.08)), 3)
        risk_score = round(min(1.0, 0.12 * risk_markers), 3)
        readiness_score = round(
            max(
                0.0,
                min(
                    1.0,
                    (coherence_score * 0.45) + (evidence_score * 0.35) - (risk_score * 0.4),
                ),
            ),
            3,
        )
        confidence = round(
            max(
                0.0,
                min(
                    1.0,
                    (evidence_score * 0.5) + (coherence_score * 0.3) + ((1.0 - risk_score) * 0.2),
                ),
            ),
            3,
        )
        recommendation = (
            "proceed"
            if readiness_score >= 0.72 and risk_score <= 0.24
            else "defer"
            if risk_score >= 0.48
            else "reassess"
        )

        return {
            "ok": True,
            "query": self.query,
            "domain": self.domain,
            "packet_focus": focus,
            "context": context,
            "memory_by_table": by_table,
            "evidence_count": evidence_count,
            "table_support": table_support,
            "coherence_score": coherence_score,
            "evidence_score": evidence_score,
            "risk_score": risk_score,
            "readiness_score": readiness_score,
            "confidence": confidence,
            "recommendation": recommendation,
            "state_hint": self.state_hint,
            "top_hits": hits[:10],
        }


def t_evaluate_state(
    query: str = "",
    domain: str = "general",
    tables: str = "",
    limit: str = "10",
    per_table: str = "2",
    state_hint: str = "",
) -> dict:
    """Evaluate a state/query and return a compact scorecard."""
    try:
        if not query:
            return {"ok": False, "error": "query required"}
        try:
            lim = max(1, min(int(limit), 50))
        except Exception:
            lim = 10
        try:
            pt = max(1, min(int(per_table), 5))
        except Exception:
            pt = 2
        table_list = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
        return StateEvaluator(
            query=query,
            domain=domain,
            tables=table_list,
            limit=lim,
            per_table=pt,
            state_hint=state_hint,
        ).evaluate()
    except Exception as e:
        return {"ok": False, "error": str(e)}
