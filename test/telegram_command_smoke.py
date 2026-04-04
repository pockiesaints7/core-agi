#!/usr/bin/env python3
"""
CORE Telegram command smoke.

Exercises every Telegram command branch locally with external side effects
stubbed out. This is focused on production readiness of the Telegram owner
surface: formatting, command routing, and non-crashing execution.
"""

from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core_github
import core_main
import core_orch_main
import core_repo_map
import core_tools


COUNTS = {
    "task_queue_pending": 2,
    "task_queue_in_progress": 1,
    "task_queue_done": 19,
    "task_queue_failed": 0,
    "evolution_pending": 4,
    "evolution_applied": 1,
    "evolution_rejected": 0,
    "knowledge_base": 1850,
    "mistakes": 95,
    "sessions": 1750,
    "repo_components": 146,
    "repo_component_chunks": 770,
    "repo_component_edges": 755,
    "repo_scan_runs": 12,
}

MEMORY_COUNTS = {
    "knowledge_base": 1850,
    "mistakes": 95,
    "behavioral_rules": 11,
    "hot_reflections": 28,
    "output_reflections": 41,
    "conversation_episodes": 87,
    "pattern_frequency": 53,
}

TASK_STATUS = {
    "enabled": True,
    "running": False,
    "interval_seconds": 60,
    "batch_limit": 2,
    "sources": ["db_only", "behavioral_rule"],
    "pending": 2,
    "in_progress": 1,
    "track_counts": {"ops": 2, "learning": 1},
    "last_summary": {"finished_at": "2026-04-05T10:00:00Z"},
}

RESEARCH_STATUS = {
    "enabled": True,
    "pending": 1,
    "completed_tasks": 3,
    "follow_up_queued": 2,
    "last_summary": {"finished_at": "2026-04-05T10:05:00Z"},
    "last_run_at": "2026-04-05T10:05:00Z",
}

CODE_STATUS = {
    "enabled": True,
    "running": False,
    "interval_seconds": 300,
    "batch_limit": 1,
    "pending_code_tasks": 1,
    "pending_review_proposals": 2,
    "track_counts": {"core": 2},
    "last_summary": {"finished_at": "2026-04-05T10:10:00Z"},
}

INTEGRATION_STATUS = {
    "enabled": True,
    "running": False,
    "interval_seconds": 300,
    "batch_limit": 1,
    "pending_integration_tasks": 1,
    "pending_review_proposals": 1,
    "track_counts": {"integration": 1},
    "last_summary": {"finished_at": "2026-04-05T10:15:00Z"},
}

EVOLUTION_STATUS = {
    "enabled": True,
    "running": False,
    "interval_seconds": 600,
    "batch_limit": 3,
    "pending_evolutions": 4,
    "synthesized_evolutions": 2,
    "pending_improvement_tasks": 1,
    "track_counts": {"hardening": 2},
    "backlog_monitor": {"trend": "stable", "window": "24h", "growth": 0, "task_growth": 0},
    "last_summary": {
        "finished_at": "2026-04-05T10:20:00Z",
        "details": [
            {
                "evolution_id": 42,
                "change_type": "code",
                "task_created": True,
                "task_group": "core",
                "work_track": "ops",
            }
        ],
    },
}

SEMANTIC_STATUS = {
    "enabled": True,
    "running": False,
    "interval_seconds": 900,
    "batch_limit": 20,
    "last_run_at": "2026-04-05T10:25:00Z",
    "projected_domains": {"knowledge_base": 8, "sessions": 5},
}

HEALTH = {
    "overall": "ok",
    "components": {
        "supabase": "ok",
        "groq": "ok",
        "telegram": "ok",
        "github": "ok",
    },
    "training_pipeline": {
        "pipeline_ok": True,
        "blocking_flags": [],
        "informational_flags": ["trading_real_signal_idle"],
    },
    "trading_readiness": {
        "ready": True,
        "counts": {
            "rules": 11,
            "knowledge_base": 21,
            "seed_sources": 10,
            "seed_concepts": 8,
        },
        "blockers": [],
    },
}

STATE_PACKET = {
    "verification": {
        "verified": True,
        "verification_score": 1.0,
        "warnings": ["stale pointer"],
    }
}

PROPOSAL_STATUS = {
    "enabled": True,
    "pending": 3,
    "pending_owner_only": 2,
    "cluster_review_ready_rows": 2,
    "route_counts": {"owner_only": 2, "auto": 1},
}

REVIEW_ROWS = [
    {
        "id": 101,
        "change_type": "code",
        "confidence": 0.91,
        "change_summary": "Harden Telegram handler reporting",
    },
    {
        "id": 102,
        "change_type": "integration",
        "confidence": 0.72,
        "change_summary": "Sync deployment verification packet",
    },
]


class ImmediateThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.daemon = daemon

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


def _msg(text: str) -> dict:
    return {"chat": {"id": "owner"}, "text": text}


def _owner_digest() -> tuple[str, dict]:
    return (
        "<b>CORE Owner Summary</b>\n"
        "Overall: WORKING | learning active | evolving active\n"
        "Working Now: healthy and ready",
        {"overall": "WORKING"},
    )


def _notify_capture(store: list[str]):
    def _inner(message, cid=None):
        store.append(str(message))
        return True

    return _inner


def _orchestrator_stub(messages: list[str]):
    def _inner(msg):
        text = str((msg or {}).get("text") or "")
        messages.append(f"ORCH handled: {text}")
        return None

    return _inner


def _fake_http_post(calls: list[tuple[str, dict | None, dict | None]]):
    def _inner(url, json=None, data=None, timeout=10):
        calls.append((url, json, data))
        return SimpleNamespace(is_success=True, text="ok", status_code=200)

    return _inner


def main() -> int:
    failures: list[str] = []
    messages: list[str] = []
    telegram_api_calls: list[tuple[str, dict | None, dict | None]] = []

    cases = [
        ("/start", "CORE Online"),
        ("/help", "CORE Telegram Commands"),
        ("/summary", "CORE Owner Summary"),
        ("/status", "Configured autonomy"),
        ("/health", "Trading readiness: ready"),
        ("/queues", "Owner review backlog"),
        ("/task", "Task Autonomy"),
        ("/tasks", "Task Autonomy"),
        ("/research", "Research Status"),
        ("/research run 2", "Research Autonomy"),
        ("/code", "Code Status"),
        ("/code run 1", "Code Autonomy"),
        ("/integration", "Integration Status"),
        ("/integration run 1", "Integration Autonomy"),
        ("/autonomy", "Autonomy Overview"),
        ("/evolution", "Evolution Autonomy"),
        ("/evolutions run 2", "Evolution Autonomy"),
        ("/review", "Proposal Router"),
        ("/proposals", "Proposal Router"),
        ("/memory", "Memory"),
        ("/semantic", "Semantic Projection"),
        ("/semantic run 5", "Run result: processed"),
        ("/repo", "Repo Map Status"),
        ("/repo sync", "Sync: ok"),
        ("/repo alpha", "Query: alpha"),
        ("/audit", "Manual Work Audit"),
        ("/gaps run", "Manual Work Audit"),
        ("/deploycheck", "Deployment"),
        ("/project list", "Projects"),
        ("/project alpha", "Project Context"),
        ("/restart", "Restart"),
        ("/kill", "Active sessions marked aborted"),
        ("show me what changed", "ORCH handled: show me what changed"),
    ]

    with ExitStack() as stack:
        stack.enter_context(patch.object(core_main, "notify", side_effect=_notify_capture(messages)))
        stack.enter_context(patch.object(core_main, "_is_owner_chat", return_value=True))
        stack.enter_context(patch.object(core_main, "get_system_counts", return_value=COUNTS))
        stack.enter_context(patch.object(core_main, "_memory_counts", return_value=MEMORY_COUNTS))
        stack.enter_context(patch.object(core_main, "get_resume_task", return_value="Resuming: telegram hardening"))
        stack.enter_context(patch.object(core_main, "autonomy_status", return_value=TASK_STATUS))
        stack.enter_context(patch.object(core_main, "research_autonomy_status", return_value=RESEARCH_STATUS))
        stack.enter_context(patch.object(core_main, "code_autonomy_status", return_value=CODE_STATUS))
        stack.enter_context(patch.object(core_main, "integration_autonomy_status", return_value=INTEGRATION_STATUS))
        stack.enter_context(patch.object(core_main, "evolution_autonomy_status", return_value=EVOLUTION_STATUS))
        stack.enter_context(patch.object(core_main, "semantic_projection_status", return_value=SEMANTIC_STATUS))
        stack.enter_context(
            patch.object(
                core_main,
                "run_semantic_projection_cycle",
                return_value={**SEMANTIC_STATUS, "processed": 7, "projected": 5, "skipped": 2},
            )
        )
        stack.enter_context(
            patch.object(
                core_main,
                "run_research_autonomy_cycle",
                return_value={"processed": 2, "completed": 2, "failed": 0, "pending": 1, "follow_up_queued": 1},
            )
        )
        stack.enter_context(
            patch.object(
                core_main,
                "run_code_autonomy_cycle",
                return_value={"processed": 1, "proposed": 1, "duplicates": 0, "deferred": 0, "failures": 0, "pending_now": 1, "pending_proposals_now": 2},
            )
        )
        stack.enter_context(
            patch.object(
                core_main,
                "run_integration_autonomy_cycle",
                return_value={"processed": 1, "proposed": 1, "duplicates": 0, "deferred": 0, "failures": 0, "pending_now": 1, "pending_proposals_now": 1},
            )
        )
        stack.enter_context(patch.object(core_main, "run_evolution_autonomy_cycle", return_value=EVOLUTION_STATUS))
        stack.enter_context(
            patch.object(
                core_main,
                "run_repo_map_cycle",
                return_value={
                    "ok": True,
                    "duration_sec": 1,
                    "summary": {
                        "files_total": 8,
                        "files_changed": 2,
                        "components_upserted": 4,
                        "chunks_upserted": 12,
                        "edges_upserted": 7,
                        "removed": 0,
                    },
                },
            )
        )
        stack.enter_context(patch.object(core_main, "repo_map_status", return_value={"enabled": True, "counts": COUNTS}))
        stack.enter_context(
            patch.object(core_main, "render_repo_map_status_report", return_value="<b>Repo Map Status</b>\nready")
        )
        stack.enter_context(patch.object(core_main, "proposal_router_status", return_value=PROPOSAL_STATUS))
        stack.enter_context(patch.object(core_main, "_fetch_pending_reviews", return_value=REVIEW_ROWS))
        stack.enter_context(
            patch.object(
                core_main,
                "render_proposal_router_dashboard",
                return_value="<b>Proposal Router</b>\nowner queue healthy",
            )
        )
        stack.enter_context(
            patch.object(core_main, "render_research_status_report", return_value="<b>Research Status</b>\nready")
        )
        stack.enter_context(patch.object(core_main, "render_code_status_report", return_value="<b>Code Status</b>\nready"))
        stack.enter_context(
            patch.object(core_main, "render_integration_status_report", return_value="<b>Integration Status</b>\nready")
        )
        stack.enter_context(patch.object(core_main, "build_owner_digest_message", side_effect=_owner_digest))
        stack.enter_context(
            patch.object(core_main, "_render_deployment_report", return_value="<b>Deployment</b>\ncommit=smoke")
        )
        stack.enter_context(
            patch.object(core_main, "build_core_gap_audit", return_value={"ok": True, "gaps": [], "summary": {"gap_count": 0}})
        )
        stack.enter_context(
            patch.object(core_main, "format_core_gap_audit", return_value="<b>Manual Work Audit</b>\nall clear")
        )
        stack.enter_context(
            patch.object(
                core_main,
                "format_core_gap_audit_status",
                return_value="enabled | gaps 0 | critical 0 | warning 0 | last_run 2026-04-05T10:30:00Z",
            )
        )
        stack.enter_context(
            patch.object(core_main, "autonomy_digest_status", return_value={"interval_seconds": 43200, "last_digest_at": "2026-04-05T10:30:00Z"})
        )
        stack.enter_context(
            patch.object(
                core_tools,
                "t_ping_health",
                return_value=HEALTH,
            )
        )
        stack.enter_context(patch.object(core_tools, "t_state_packet", return_value=STATE_PACKET))
        stack.enter_context(
            patch.object(
                core_tools,
                "t_get_training_pipeline",
                return_value={"pipeline_ok": True, "health_flags": []},
            )
        )
        stack.enter_context(
            patch.object(
                core_tools,
                "t_project_list",
                return_value={"ok": True, "projects": [{"name": "Alpha", "project_id": "alpha", "status": "active"}]},
            )
        )
        stack.enter_context(patch.object(core_tools, "t_project_prepare", return_value={"ok": True, "prepared": ["alpha"]}))
        stack.enter_context(
            patch.object(
                core_repo_map,
                "build_repo_component_packet",
                return_value={"components": [{"name": "Alpha"}], "chunks": [1, 2], "edges": [1], "summary": "Alpha packet"},
            )
        )
        stack.enter_context(
            patch.object(core_repo_map, "build_repo_graph_packet", return_value={"count_nodes": 3, "count_edges": 2})
        )
        stack.enter_context(patch.object(core_orch_main, "handle_telegram_message", side_effect=_orchestrator_stub(messages)))
        stack.enter_context(patch("threading.Thread", ImmediateThread))
        stack.enter_context(patch("time.sleep", return_value=None))
        mock_subprocess = stack.enter_context(patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="ok")))
        mock_http_patch = stack.enter_context(patch("httpx.patch", return_value=SimpleNamespace(status_code=200, text="ok")))

        for idx, (text, expected) in enumerate(cases, start=1):
            before = len(messages)
            core_main.handle_msg(_msg(text), update_id=idx)
            delta = messages[before:]
            if not delta:
                failures.append(f"{text}: no Telegram output captured")
                continue
            if not any(expected in item for item in delta):
                failures.append(f"{text}: expected substring {expected!r} not found in outputs {delta!r}")

        help_text = core_main._render_command_catalog()
        start_text = core_main._build_startup_brief("Resuming: telegram hardening", COUNTS, None, TASK_STATUS, EVOLUTION_STATUS)
        if "/summary" not in help_text:
            failures.append("/help catalog missing /summary")
        if "/summary" not in start_text or "Owner digest cadence" not in start_text:
            failures.append("/start brief missing owner summary guidance")
        commands = [item["command"] for item in core_main.CORE_TELEGRAM_COMMANDS]
        if len(commands) != len(set(commands)):
            failures.append("CORE_TELEGRAM_COMMANDS contains duplicate primary commands")
        if "summary" not in commands:
            failures.append("CORE_TELEGRAM_COMMANDS missing summary command")

        with patch.object(core_github, "TELEGRAM_TOKEN", "test-token"), patch.object(
            core_github.httpx, "post", side_effect=_fake_http_post(telegram_api_calls)
        ):
            ok_commands = core_github.set_telegram_commands(core_main.CORE_TELEGRAM_COMMANDS)
            ok_profile = core_github.set_telegram_profile(
                short_description=core_main.CORE_TELEGRAM_SHORT_DESCRIPTION,
                description=core_main.CORE_TELEGRAM_DESCRIPTION,
            )
        if not ok_commands:
            failures.append("set_telegram_commands returned false")
        if not ok_profile:
            failures.append("set_telegram_profile returned false")
        if not any("setMyCommands" in url for url, _json, _data in telegram_api_calls):
            failures.append("Telegram command menu API was not exercised")
        if not any("setMyShortDescription" in url for url, _json, _data in telegram_api_calls):
            failures.append("Telegram short description API was not exercised")
        if not any("setMyDescription" in url for url, _json, _data in telegram_api_calls):
            failures.append("Telegram description API was not exercised")
        command_call = next((payload for url, payload, _data in telegram_api_calls if "setMyCommands" in url), None)
        if not command_call or any(sorted(item.keys()) != ["command", "description"] for item in command_call.get("commands", [])):
            failures.append("Telegram command payload was not sanitized to command/description only")

        if mock_subprocess.call_count != 1:
            failures.append(f"/restart expected 1 subprocess.run call, got {mock_subprocess.call_count}")
        if mock_http_patch.call_count != 1:
            failures.append(f"/kill expected 1 httpx.patch call, got {mock_http_patch.call_count}")

    total = len(cases) + 8
    passed = total - len(failures)
    if failures:
        print(f"telegram_command_smoke: {passed}/{total} checks passed")
        for item in failures:
            print(f"- {item}")
        return 1

    print(f"telegram_command_smoke: {passed}/{total} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
