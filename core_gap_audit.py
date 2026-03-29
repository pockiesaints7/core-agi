"""core_gap_audit.py — CORE-wide manual work detector.

This module consolidates architectural and capability gap checks across CORE:
- tool taxonomy drift
- repo map health and freshness
- capability model weaknesses
- session quality regressions
- queue/backlog pressure that CORE cannot self-resolve

It is intentionally CORE-native and not tied to the trading-bot runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from core_config import _sbh_count_svc, sb_get
from core_github import notify
from core_repo_map import repo_map_status
from core_orch_context import _tool_family_for_name

_LOCK = threading.Lock()
_STATE = {
    "running": False,
    "last_run_at": "",
    "last_error": "",
    "last_signature": "",
    "last_report": {},
    "last_notified_at": "",
    "last_notified_signature": "",
}

AUDIT_ENABLED = os.getenv("CORE_GAP_AUDIT_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
AUDIT_INTERVAL_S = max(300, int(os.getenv("CORE_GAP_AUDIT_INTERVAL_S", "3600")))
AUDIT_NOTIFY_ON_WARNING = os.getenv("CORE_GAP_AUDIT_NOTIFY_ON_WARNING", "true").strip().lower() not in {"0", "false", "no", "off"}
AUDIT_QUEUE_WARN_TASK = max(200, int(os.getenv("CORE_GAP_AUDIT_QUEUE_WARN_TASK", "1000")))
AUDIT_QUEUE_WARN_EVO = max(50, int(os.getenv("CORE_GAP_AUDIT_QUEUE_WARN_EVO", "250")))
AUDIT_QUEUE_WARN_OWNER = max(25, int(os.getenv("CORE_GAP_AUDIT_QUEUE_WARN_OWNER", "75")))
AUDIT_REPO_STALE_HOURS = max(6, int(os.getenv("CORE_GAP_AUDIT_REPO_STALE_HOURS", "24")))


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _count_table(table: str, extra: str = "") -> int:
    try:
        import httpx
        from core_config import SUPABASE_URL
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/{table}?select=id&limit=1{extra}",
            headers=_sbh_count_svc(),
            timeout=10,
        )
        cr = r.headers.get("content-range", "*/0")
        return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return -1


def _tool_family_counts() -> dict[str, int]:
    try:
        from core_tools import TOOLS
    except Exception:
        return {}
    counts: Counter[str] = Counter()
    for name, tool in TOOLS.items():
        desc = ""
        if isinstance(tool, dict):
            desc = tool.get("desc") or ""
        counts[_tool_family_for_name(name, desc)] += 1
    return dict(counts)


def _capability_rows() -> list[dict[str, Any]]:
    try:
        return sb_get(
            "capability_model",
            "select=domain,capability,reliability,tool_count,avg_fail_rate,last_calibrated,notes&order=reliability.asc&limit=50",
            svc=True,
        ) or []
    except Exception:
        return []


def _gap_rows() -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    counts = _live_counts()
    family_counts = _tool_family_counts()
    repo = repo_map_status()
    try:
        from core_tools import t_state_packet
        state_packet = t_state_packet(session_id="default", strict=False)
    except Exception as exc:
        state_packet = {"ok": False, "error": str(exc)}

    quality_alert = (state_packet.get("quality_alert") or {}) if isinstance(state_packet, dict) else {}
    weak_domains = list((state_packet.get("weak_capability_domains") or []) if isinstance(state_packet, dict) else [])
    repo_counts = (repo.get("counts") or {}) if isinstance(repo, dict) else {}
    repo_latest = (repo.get("latest_run") or {}) if isinstance(repo, dict) else {}
    capability_rows = _capability_rows()
    weak_capabilities = [
        row for row in capability_rows
        if float(row.get("reliability") or 1.0) < 0.60
    ]

    if family_counts.get("other", 0) > 0:
        gaps.append({
            "severity": "critical",
            "source": "tool_taxonomy",
            "title": "New tool family needs taxonomy coverage",
            "evidence": f"other tools={family_counts.get('other', 0)}",
            "manual_action": "Update the tool taxonomy so the new capability family is explicit.",
        })

    if not repo.get("enabled", True):
        gaps.append({
            "severity": "warning",
            "source": "repo_map",
            "title": "Repo map is disabled",
            "evidence": "repo_map_status.enabled=false",
            "manual_action": "Re-enable repo map scanning if CORE should keep a live semantic map.",
        })
    if repo.get("last_error"):
        gaps.append({
            "severity": "warning",
            "source": "repo_map",
            "title": "Repo map reported an error",
            "evidence": str(repo.get("last_error"))[:220],
            "manual_action": "Inspect the repo-map scan error and repair the failing file or schema path.",
        })
    last_run_at = repo.get("last_run_at") or ""
    if last_run_at:
        try:
            dt = datetime.fromisoformat(last_run_at)
            if datetime.utcnow() - dt > timedelta(hours=AUDIT_REPO_STALE_HOURS):
                gaps.append({
                    "severity": "warning",
                    "source": "repo_map",
                    "title": "Repo map is stale",
                    "evidence": f"last_run_at={last_run_at}",
                    "manual_action": "Run a repo-map sync so CORE can reason from the current codebase graph.",
                })
        except Exception:
            pass
    if repo_counts.get("repo_components", 0) <= 0 or repo_counts.get("repo_component_chunks", 0) <= 0:
        gaps.append({
            "severity": "critical",
            "source": "repo_map",
            "title": "Repo map has no semantic coverage",
            "evidence": json.dumps(repo_counts, default=str),
            "manual_action": "Seed or rescan the repo map so CORE has component/chunk coverage.",
        })
    if repo_latest and repo_latest.get("status") not in {None, "ok"}:
        gaps.append({
            "severity": "warning",
            "source": "repo_map",
            "title": "Last repo-map run was not clean",
            "evidence": f"status={repo_latest.get('status')} error={repo_latest.get('error') or ''}",
            "manual_action": "Inspect the latest repo-map run and repair the files or schema that failed.",
        })

    if quality_alert.get("alert"):
        gaps.append({
            "severity": "warning",
            "source": "quality_metrics",
            "title": "Session quality is declining",
            "evidence": f"trend={quality_alert.get('trend')} 7d_avg={quality_alert.get('7d_avg')}",
            "manual_action": "Review recent weak sessions and tighten routing, memory, or tool policy.",
        })

    if weak_domains:
        for domain in weak_domains[:8]:
            gaps.append({
                "severity": "warning",
                "source": "capability_model",
                "title": f"Weak capability domain: {domain}",
                "evidence": f"reliability<0.60 for domain={domain}",
                "manual_action": f"Improve or calibrate the '{domain}' domain before relying on it for autonomous work.",
            })

    if isinstance(state_packet, dict) and not state_packet.get("ok", True):
        gaps.append({
            "severity": "warning",
            "source": "state_packet",
            "title": "State packet is degraded",
            "evidence": str(state_packet.get("error") or "state packet unavailable")[:220],
            "manual_action": "Repair session/state continuity so CORE can trust its own memory snapshot.",
        })
    if isinstance(state_packet, dict):
        verification = state_packet.get("verification") or {}
        if verification and not verification.get("verified", True):
            gaps.append({
                "severity": "warning",
                "source": "state_packet",
                "title": "State continuity verification failed",
                "evidence": f"score={verification.get('verification_score')} warnings={len(verification.get('warnings') or [])}",
                "manual_action": "Fix the continuity warnings before treating the session snapshot as trustworthy.",
            })

    if counts.get("task_queue_pending", 0) >= AUDIT_QUEUE_WARN_TASK:
        gaps.append({
            "severity": "warning",
            "source": "task_queue",
            "title": "Task backlog is high",
            "evidence": f"pending={counts.get('task_queue_pending', 0)}",
            "manual_action": "Drain or batch the task queue; the backlog is now large enough to deserve manual attention.",
        })
    if counts.get("evolution_pending", 0) >= AUDIT_QUEUE_WARN_EVO:
        gaps.append({
            "severity": "warning",
            "source": "evolution_queue",
            "title": "Evolution backlog is high",
            "evidence": f"pending={counts.get('evolution_pending', 0)}",
            "manual_action": "Inspect evolution routing or batch-close the backlog so it does not keep growing.",
        })
    if counts.get("owner_review_pending", 0) >= AUDIT_QUEUE_WARN_OWNER:
        gaps.append({
            "severity": "warning",
            "source": "owner_review",
            "title": "Owner-review backlog is high",
            "evidence": f"pending={counts.get('owner_review_pending', 0)}",
            "manual_action": "Cluster or batch owner-only items so manual review stays manageable.",
        })

    # Capability model gaps worth surfacing even when the state packet has not flagged them.
    if weak_capabilities:
        top = weak_capabilities[:5]
        gaps.append({
            "severity": "info" if not gaps else "warning",
            "source": "capability_model",
            "title": "Low-reliability capability domains",
            "evidence": ", ".join(
                f"{row.get('domain')}={float(row.get('reliability') or 0):.2f}"
                for row in top
            ),
            "manual_action": "Use the weakest capability domains as the next training / calibration target.",
        })

    return gaps


def _live_counts() -> dict[str, int]:
    return {
        "knowledge_base": _count_table("knowledge_base"),
        "mistakes": _count_table("mistakes"),
        "sessions": _count_table("sessions"),
        "task_queue_pending": _count_table("task_queue", "&status=eq.pending"),
        "task_queue_in_progress": _count_table("task_queue", "&status=eq.in_progress"),
        "task_queue_done": _count_table("task_queue", "&status=eq.done"),
        "task_queue_failed": _count_table("task_queue", "&status=eq.failed"),
        "evolution_pending": _count_table("evolution_queue", "&status=eq.pending"),
        "evolution_applied": _count_table("evolution_queue", "&status=eq.applied"),
        "evolution_rejected": _count_table("evolution_queue", "&status=eq.rejected"),
        "owner_review_pending": _count_table("evolution_queue", "&review_scope=eq.owner_only&status=eq.pending"),
        "repo_components": _count_table("repo_components", "&active=eq.true"),
        "repo_component_chunks": _count_table("repo_component_chunks", "&active=eq.true"),
        "repo_component_edges": _count_table("repo_component_edges", "&active=eq.true"),
        "repo_scan_runs": _count_table("repo_scan_runs"),
    }


def build_core_gap_audit(force: bool = False) -> dict[str, Any]:
    """Return a structured audit packet showing manual work CORE cannot self-resolve."""
    with _LOCK:
        _STATE["running"] = True
    try:
        gaps = _gap_rows()
        counts = _live_counts()
        repo = repo_map_status()
        family_counts = _tool_family_counts()
        summary = {
            "gap_count": len(gaps),
            "critical_count": sum(1 for g in gaps if g.get("severity") == "critical"),
            "warning_count": sum(1 for g in gaps if g.get("severity") == "warning"),
            "info_count": sum(1 for g in gaps if g.get("severity") == "info"),
        }
        signature_src = {
            "summary": summary,
            "counts": counts,
            "repo": {
                "enabled": repo.get("enabled"),
                "running": repo.get("running"),
                "last_error": repo.get("last_error"),
                "last_run_at": repo.get("last_run_at"),
            },
            "families": family_counts,
            "gaps": [
                {
                    "severity": g.get("severity"),
                    "source": g.get("source"),
                    "title": g.get("title"),
                    "manual_action": g.get("manual_action"),
                }
                for g in gaps
            ],
        }
        signature = hashlib.sha256(json.dumps(signature_src, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        report = {
            "ok": True,
            "enabled": AUDIT_ENABLED,
            "force": force,
            "generated_at": _utcnow(),
            "summary": summary,
            "counts": counts,
            "repo_map": repo,
            "tool_family_counts": family_counts,
            "gaps": gaps,
            "manual_actions": [g.get("manual_action") for g in gaps if g.get("manual_action")],
            "signature": signature,
        }
        with _LOCK:
            _STATE.update({
                "running": False,
                "last_run_at": report["generated_at"],
                "last_error": "",
                "last_signature": signature,
                "last_report": report,
            })
        return report
    except Exception as exc:
        with _LOCK:
            _STATE.update({
                "running": False,
                "last_run_at": _utcnow(),
                "last_error": str(exc)[:500],
            })
        return {"ok": False, "error": str(exc), "enabled": AUDIT_ENABLED}


def format_core_gap_audit(report: dict[str, Any]) -> str:
    gaps = report.get("gaps") or []
    counts = report.get("counts") or {}
    families = report.get("tool_family_counts") or {}
    lines = [
        "<b>CORE Manual Work Audit</b>",
        f"Status: {'enabled' if report.get('enabled') else 'disabled'} | gaps={len(gaps)} | critical={report.get('summary', {}).get('critical_count', 0)} | warning={report.get('summary', {}).get('warning_count', 0)}",
        f"Repo map: components {counts.get('repo_components', 0)} | chunks {counts.get('repo_component_chunks', 0)} | edges {counts.get('repo_component_edges', 0)} | scans {counts.get('repo_scan_runs', 0)}",
        f"Queues: task pending {counts.get('task_queue_pending', 0)} | evolution pending {counts.get('evolution_pending', 0)} | owner-review pending {counts.get('owner_review_pending', 0)}",
        f"Tool families: other={families.get('other', 0)} | knowledge={families.get('knowledge', 0)} | state={families.get('state', 0)} | self_improve={families.get('self_improve', 0)}",
    ]
    if gaps:
        lines.append("")
        lines.append("<b>Manual actions</b>")
        for gap in gaps[:8]:
            lines.append(
                f"- <b>{gap.get('severity', 'info').upper()}</b> [{gap.get('source', 'unknown')}] "
                f"{str(gap.get('title') or '')[:120]} — {_escape(str(gap.get('manual_action') or ''), 180)}"
            )
    else:
        lines.append("")
        lines.append("No manual work gaps detected.")
    return "\n".join(lines)


def _escape(text: str, limit: int = 180) -> str:
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")[:limit]
    )


def notify_core_gap_audit(force: bool = False, chat_id: str = "") -> dict[str, Any]:
    """Run the audit and notify Telegram if gaps are present or force=True."""
    report = build_core_gap_audit(force=force)
    if not report.get("ok"):
        return report
    gaps = report.get("gaps") or []
    should_notify = force or bool(gaps) and (AUDIT_NOTIFY_ON_WARNING or any(g.get("severity") == "critical" for g in gaps))
    if should_notify:
        signature = report.get("signature", "")
        with _LOCK:
            if not force and signature and _STATE.get("last_notified_signature") == signature:
                report["notified"] = False
                report["notify_skipped"] = "duplicate_signature"
                return report
        text = format_core_gap_audit(report)
        ok = notify(text, cid=chat_id or None)
        report["notified"] = bool(ok)
        report["notify_ok"] = bool(ok)
        report["notify_chat_id"] = chat_id or ""
        if ok:
            with _LOCK:
                _STATE["last_notified_at"] = _utcnow()
                _STATE["last_notified_signature"] = signature
    else:
        report["notified"] = False
    return report


def core_gap_audit_status() -> dict[str, Any]:
    with _LOCK:
        return {
            "enabled": AUDIT_ENABLED,
            "running": _STATE.get("running", False),
            "last_run_at": _STATE.get("last_run_at", ""),
            "last_error": _STATE.get("last_error", ""),
            "last_signature": _STATE.get("last_signature", ""),
            "last_notified_at": _STATE.get("last_notified_at", ""),
            "last_notified_signature": _STATE.get("last_notified_signature", ""),
            "last_report": _STATE.get("last_report", {}),
            "interval_s": AUDIT_INTERVAL_S,
        }


def core_gap_audit_loop() -> None:
    """Background loop that notifies owner when new manual work gaps appear."""
    if not AUDIT_ENABLED:
        return
    while True:
        try:
            notify_core_gap_audit(force=False)
        except Exception as exc:
            with _LOCK:
                _STATE["last_error"] = str(exc)[:500]
        time.sleep(AUDIT_INTERVAL_S)

