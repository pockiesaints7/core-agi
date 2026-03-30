"""core_tools.py â€” CORE AGI MCP tool implementations
All t_* functions, TOOLS registry, _mcp_tool_schema, handle_jsonrpc.
Part of v6.0 split architecture: core_config, core_github, core_train, core_tools, core_main.

Import chain:
  core_tools imports: core_config, core_github, core_train
  core_main imports: core_tools (TOOLS, handle_jsonrpc)

NOTE: This IS the live implementation. Entry point = core_main.py (Procfile confirmed).
core.py has been deleted â€” it was legacy monolith."""
import base64
import difflib
import json
import os
import re as _re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any

import httpx

from core_config import (
    GITHUB_REPO, KB_MINE_BATCH_SIZE, KB_MINE_RATIO_THRESHOLD,
    COLD_HOT_THRESHOLD, COLD_KB_GROWTH_THRESHOLD, PATTERN_EVO_THRESHOLD,
    KNOWLEDGE_AUTO_CONFIDENCE, MCP_PROTOCOL_VERSION, SUPABASE_URL, SUPABASE_REF, SUPABASE_PAT,
    L, gemini_chat, groq_chat, sb_get, sb_post, sb_post_critical, sb_patch, sb_upsert, sb_delete,
)
from core_config import _sbh, _sbh_count_svc
from core_github import _ghh, _gh_blob_read, _gh_blob_write, gh_read, gh_write, notify
from core_train import apply_evolution, reject_evolution, bulk_reject_evolutions, run_cold_processor
from core_task_taxonomy import (
    t_task_mode_packet,
    t_spreadsheet_work_packet,
    t_document_work_packet,
    t_presentation_work_packet,
    t_review_work_packet,
    t_repo_review_packet,
    t_document_review_packet,
    t_spreadsheet_review_packet,
    t_presentation_review_packet,
)

# Alias â€” used in t_core_py_rollback and t_deploy_and_wait
notify_owner = notify

# BASE_URL and MCP_SECRET for tools that call the VM's own endpoints
# VM runs on Oracle Cloud — public domain via duckdns, port 8081
BASE_URL = os.environ.get("PUBLIC_URL",
           f"https://{os.environ.get('PUBLIC_DOMAIN', 'core-agi.duckdns.org')}")
MCP_SECRET = os.environ.get("MCP_SECRET", "")


# -- Helpers needed locally ---------------------------------------------------
def get_latest_session():
    rows = sb_get("sessions", f"select={_sel_force('sessions', ['summary','actions','created_at'])}&order=created_at.desc&limit=1", svc=True)
    return rows[0] if rows else {}

def get_system_counts():
    counts = {}
    for table in [
        "knowledge_base",
        "mistakes",
        "sessions",
        "task_queue",
        "hot_reflections",
        "evolution_queue",
        "repo_components",
        "repo_component_chunks",
        "repo_component_edges",
        "repo_scan_runs",
        "reflection_events",
        "reflection_event_stages",
    ]:
        try:
            r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/{table}?select=id&limit=1",
                headers=_sbh_count_svc(), timeout=10
            )
            counts[table] = int(r.headers.get("content-range", "*/0").split("/")[-1])
        except Exception:
            counts[table] = -1
    return counts

def get_current_step() -> str:
    try:
        content = gh_read("SESSION.md")
        for line in content.splitlines():
            if line.startswith("## Current Step") or "## Step" in line or "Current Step" in line:
                return line.strip()
        return "(step unknown â€” read SESSION.md)"
    except Exception as e:
        return f"(step read error: {e})"


# =============================================================================
# _SB_SCHEMA -- single source of truth for ALL Supabase tables.
# Used by: t_sb_query (read guard), t_sb_insert/patch/upsert/delete (write guard),
#          t_task_add, t_add_knowledge, t_kb_update, _validate_write.
# Never duplicate this. Update here when DDL changes.
# =============================================================================
_SB_SCHEMA = {
    # --- TOMBSTONE: never query these ---
    "_tombstone": {
        "master_prompt", "patterns", "training_sessions",
        "training_sessions_v2", "training_flags", "session_learning", "agent_registry",
        "knowledge_blocks", "agi_mistakes", "stack_registry", "vault_logs", "vault"
    },
    # --- PROTECTED: no deletes allowed ---
    "_protected": {
        "sessions", "mistakes", "hot_reflections", "cold_reflections",
        "pattern_frequency", "changelog", "evolution_queue",
        "repo_components", "repo_component_chunks", "repo_component_edges", "repo_scan_runs"
    },
    # --- TABLE DEFINITIONS ---
    "tables": {
        "knowledge_base": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "integer", "domain": "text", "topic": "text", "content": "text",
                        "source": "text", "confidence": "text_enum", "tags": "text[]",
                        "instruction": "text", "source_type": "text", "source_ref": "text",
                        "active": "boolean", "created_at": "timestamptz", "updated_at": "timestamptz"},
            "required": ["domain", "topic"],
            "enums": {"confidence": ["low", "medium", "high", "proven"]},
            "fat_columns": ["content", "instruction"],
            "safe_select": "id,domain,topic,confidence,source,created_at",
            "on_conflict": "domain,topic",
            "notes": "Use id=gt.1. contradiction check runs in t_add_knowledge before insert."
        },
        "behavioral_rules": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "bigint", "trigger": "text", "pointer": "text", "full_rule": "text",
                        "domain": "text", "priority": "integer", "active": "boolean",
                        "tested": "boolean", "source": "text", "confidence": "float8",
                        "expires_at": "timestamptz", "created_at": "timestamptz"},
            "required": ["trigger", "domain"],
            "enums": {"domain": ["auth","code","failure_recovery","github","groq","local_pc",
                                  "postgres","powershell","project","railway","reasoning",
                                  "supabase","telegram","universal","zapier"]},
            "fat_columns": ["full_rule", "pointer"],
            "safe_select": "id,domain,trigger,confidence,active,source,created_at",
            "notes": "Use id=gt.1. 120+ active rules -- always paginate."
        },
        "hot_reflections": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "bigint", "task_summary": "text", "domain": "text",
                        "quality_score": "float8", "reflection_text": "text",
                        "new_patterns": "text", "new_mistakes": "text", "gaps_identified": "text",
                        "processed_by_cold": "boolean", "source": "text", "created_at": "timestamptz"},
            "required": ["domain"],
            "enums": {},
            "fat_columns": ["new_patterns", "new_mistakes", "gaps_identified", "task_summary", "reflection_text"],
            "safe_select": "id,domain,quality_score,source,processed_by_cold,created_at",
            "notes": "Use id=gt.1. PROTECTED -- no deletes."
        },
        "reflection_events": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {
                "id": "bigint", "event_id": "uuid", "source": "text",
                "source_domain": "text", "source_branch": "text", "source_service": "text",
                "event_type": "text", "trace_id": "text", "decision_id": "text",
                "position_id": "text", "session_id": "uuid", "symbol": "text",
                "strategy": "text", "strategy_family": "text", "regime_at_entry": "text",
                "bias_at_entry": "text", "verdict": "text", "pnl_usd": "float8",
                "pnl_pct": "float8", "funding_usd": "float8", "capital_usd": "float8",
                "confidence": "float8", "close_reason": "text", "output_text": "text",
                "context": "jsonb", "producer_created_at": "timestamptz",
                "core_received_at": "timestamptz", "status": "text",
                "l11_session_id": "uuid", "critic_at": "timestamptz", "causal_at": "timestamptz",
                "reflect_at": "timestamptz", "meta_at": "timestamptz",
                "last_error": "text", "artifacts": "jsonb", "prompt_target": "text",
                "prompt_version": "integer", "current_stage": "text",
                "current_stage_status": "text", "updated_at": "timestamptz"
            },
            "required": ["event_id", "source", "source_domain", "source_branch", "source_service", "status"],
            "enums": {},
            "fat_columns": ["output_text", "context", "artifacts", "last_error"],
            "safe_select": "event_id,source_domain,source_branch,source_service,status,trace_id,decision_id,position_id,core_received_at,updated_at",
            "on_conflict": "event_id",
            "notes": "Canonical reflection audit ledger for every L11 source. Use this before legacy artifact lookups."
        },
        "reflection_event_stages": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {
                "id": "bigint", "event_id": "uuid", "stage_name": "text",
                "status": "text", "source": "text", "details": "jsonb",
                "error": "text", "started_at": "timestamptz", "completed_at": "timestamptz",
                "updated_at": "timestamptz"
            },
            "required": ["event_id", "stage_name", "status"],
            "enums": {},
            "fat_columns": ["details", "error"],
            "safe_select": "id,event_id,stage_name,status,source,started_at,completed_at,updated_at",
            "on_conflict": "event_id,stage_name",
            "notes": "Per-stage audit trail for reflection_events."
        },
        "mistakes": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "integer", "context": "text", "what_failed": "text",
                        "root_cause": "text", "correct_approach": "text", "domain": "text",
                        "how_to_avoid": "text", "severity": "text", "tags": "text[]",
                        "created_at": "timestamptz"},
            "required": ["domain", "what_failed"],
            "enums": {"severity": ["low", "medium", "high", "critical"]},
            "fat_columns": ["context", "correct_approach", "how_to_avoid"],
            "safe_select": "id,domain,what_failed,severity,root_cause,created_at",
            "notes": "Use id=gt.1. PROTECTED -- no deletes."
        },
        "task_queue": {
            "pk": "id", "pk_type": "uuid",
            "columns": {"id": "uuid", "task": "text", "status": "text", "priority": "integer",
                        "result": "text", "error": "text", "source": "text", "chat_id": "text",
                        "next_step": "text", "blocked_by": "text", "checkpoint": "jsonb",
                        "checkpoint_at": "timestamptz", "checkpoint_draft": "text",
                        "project_id": "text", "created_at": "timestamptz", "updated_at": "timestamptz"},
            "required": ["task", "status"],
            "enums": {"status": ["pending", "in_progress", "done", "failed"],
                      "source": ["mcp_session", "self_assigned", "core_v6_registry", "bulk_apply", "improvement"]},
            "fat_columns": ["task", "checkpoint", "checkpoint_draft", "result"],
            "safe_select": "id,status,priority,source,next_step,blocked_by,created_at",
            "notes": "UUID PK -- no id=gt.1 rule. task column is string-encoded JSON blob -- never select=*."
        },
        "evolution_queue": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "bigint", "change_type": "text", "change_summary": "text",
                        "diff_content": "text", "pattern_key": "text", "confidence": "float8",
                        "status": "text", "source": "text", "impact": "text",
                        "recommendation": "text", "applied_at": "timestamptz", "created_at": "timestamptz",
                        "approval_tier": "text", "tier_applied_at": "timestamptz",
                        "rejected_by_owner": "boolean"},
            "required": ["change_type", "status"],
            "enums": {"status": ["pending", "pending_desktop", "applied", "rejected", "synthesized"],
                      "change_type": ["knowledge", "code", "new_tool", "script_template", "behavior", "backlog"]},
            "fat_columns": ["diff_content", "change_summary"],
            "safe_select": "id,change_type,status,confidence,pattern_key,source,created_at",
            "notes": "Use id=gt.1. PROTECTED -- no deletes."
        },
        "sessions": {
            "pk": "id", "pk_type": "int4",
            "columns": {"id": "integer", "summary": "text", "actions": "ARRAY",
                        "project_id": "integer", "created_at": "timestamptz",
                        "interface": "text", "checkpoint_data": "jsonb",
                        "checkpoint_ts": "timestamptz", "resume_task": "text",
                        "domain": "text", "quality_score": "float8",
                        "next_session": "text", "interrupted": "boolean",
                        "tools_called": "jsonb"},
            "required": [],
            "enums": {},
            "fat_columns": ["actions", "checkpoint_data", "tools_called"],
            "safe_select": "id,summary,domain,quality_score,created_at,resume_task",
            "notes": "Use id=gt.1. quality column is quality_score. actions is ARRAY type."
        },
        "pattern_frequency": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "bigint", "pattern_key": "text", "frequency": "integer",
                        "domain": "text", "description": "text", "auto_applied": "boolean",
                        "last_seen": "timestamptz", "stale": "boolean"},
            "required": ["pattern_key"],
            "enums": {},
            "fat_columns": ["description"],
            "safe_select": "id,pattern_key,frequency,domain,auto_applied,last_seen,stale",
            "on_conflict": "pattern_key",
            "notes": "Use id=gt.1. PROTECTED -- no deletes."
        },
        "changelog": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "bigint", "action": "text", "detail": "text",
                        "domain": "text", "created_at": "timestamptz"},
            "required": ["action"],
            "enums": {},
            "fat_columns": ["detail"],
            "safe_select": "id,action,domain,created_at",
            "notes": "Use id=gt.1. PROTECTED -- no deletes."
        },
        "cold_reflections": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "bigint", "period_start": "timestamptz", "period_end": "timestamptz",
                        "hot_count": "integer", "patterns_found": "integer",
                        "evolutions_queued": "integer", "auto_applied": "integer",
                        "summary_text": "text", "created_at": "timestamptz"},
            "required": [],
            "enums": {},
            "fat_columns": ["summary_text"],
            "safe_select": "id,period_start,period_end,hot_count,patterns_found,evolutions_queued,auto_applied,created_at",
            "notes": "Use id=gt.1. PROTECTED -- no deletes."
        },
        "quality_metrics": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "bigint", "session_id": "uuid", "quality_score": "float8",
                        "tasks_completed": "integer", "mistakes_made": "integer",
                        "owner_corrections": "integer", "assumptions_caught": "integer",
                        "domain": "text", "notes": "text", "created_at": "timestamptz"},
            "required": ["quality_score"],
            "enums": {},
            "fat_columns": ["notes"],
            "safe_select": "id,session_id,quality_score,tasks_completed,mistakes_made,domain,created_at",
            "notes": "Use id=gt.1."
        },
        "system_map": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "bigint", "layer": "text", "component": "text",
                        "name": "text", "item_type": "text", "role": "text",
                        "responsibility": "text", "description": "text",
                        "key_facts": "jsonb", "is_volatile": "boolean",
                        "status": "text", "notes": "text",
                        "last_verified": "timestamptz", "last_updated": "timestamptz",
                        "updated_by": "text"},
            "required": ["layer", "component"],
            "enums": {"status": ["active", "degraded", "tombstone"],
                      "item_type": ["tool", "file", "table", "service", "doc", "folder", "config", "module", "script"]},
            "fat_columns": ["description", "notes", "key_facts", "responsibility"],
            "safe_select": "id,layer,component,name,item_type,status,last_updated",
            "notes": "Use id=gt.1. 47+ rows covering all CORE layers. Auto-reconciled every 6h by background_researcher."
        },
        "script_templates": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "bigint", "name": "text", "description": "text",
                        "trigger_pattern": "text", "code": "text",
                        "use_count": "integer", "created_at": "timestamptz"},
            "required": ["name"],
            "enums": {},
            "fat_columns": ["code"],
            "safe_select": "id,name,description,trigger_pattern,use_count,created_at",
            "on_conflict": "name",
            "notes": "Use id=gt.1."
        },
        "session_goals": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "bigint", "goal": "text", "domain": "text",
                        "progress": "text", "status": "text",
                        "created_at": "timestamptz", "updated_at": "timestamptz"},
            "required": ["goal"],
            "enums": {"status": ["active", "completed", "paused"]},
            "fat_columns": ["progress"],
            "safe_select": "id,goal,domain,status,created_at,updated_at",
            "notes": "P2-03: Cross-session goal tracker. Use get_active_goals at session_start."
        },
        "owner_profile": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "bigint", "dimension": "text", "value": "text",
                        "confidence": "float8", "evidence": "text", "domain": "text",
                        "source": "text", "active": "boolean", "times_observed": "integer",
                        "last_seen": "timestamptz", "created_at": "timestamptz",
                        "updated_at": "timestamptz"},
            "required": ["dimension", "value"],
            "enums": {"dimension": ["communication_style", "decision_pattern", "recurring_concern",
                                     "working_habit", "preference", "trigger", "frustration"]},
            "fat_columns": ["value", "evidence"],
            "safe_select": "id,dimension,value,confidence,domain,times_observed,last_seen",
            "on_conflict": "dimension,value",
            "notes": "P3-02: Vux behavioral model. Use id=gt.1. Grows via cold processor."
        },
        "capability_model": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "bigint", "domain": "text", "capability": "text",
                        "reliability": "float8", "tool_count": "integer",
                        "avg_fail_rate": "float8", "strong_tools": "jsonb",
                        "weak_tools": "jsonb", "last_calibrated": "timestamptz",
                        "notes": "text", "created_at": "timestamptz"},
            "required": ["domain", "capability"],
            "enums": {},
            "fat_columns": ["strong_tools", "weak_tools", "notes"],
            "safe_select": "id,domain,capability,reliability,tool_count,avg_fail_rate,last_calibrated",
            "on_conflict": "domain",
            "notes": "P3-07: CORE self-model. Calibrated weekly. Unique on domain."
        },
        "conversation_episodes": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {"id": "bigint", "chat_id": "text", "summary": "text",
                        "embedding": "vector", "turn_start": "timestamptz",
                        "turn_end": "timestamptz", "topic_tags": "text[]",
                        "created_at": "timestamptz"},
            "required": ["chat_id", "summary"],
            "enums": {},
            "fat_columns": ["embedding", "summary"],
            "safe_select": "id,chat_id,turn_start,turn_end,topic_tags,created_at",
            "notes": (
                "P3-04: Compressed conversation episode memory with vector embeddings. "
                "REQUIRES: CREATE TABLE conversation_episodes + vector(768) column + ivfflat index. "
                "Populated by _compress_history when conversation exceeds HISTORY_COMPRESS_AT turns."
            ),
        },
        "repo_components": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {
                "id": "bigint", "repo": "text", "path": "text", "file_name": "text", "file_ext": "text",
                "language": "text", "item_type": "text", "runtime_role": "text", "summary": "text",
                "purpose_summary": "text", "symbols": "jsonb", "imports": "jsonb", "links": "jsonb",
                "file_hash": "text", "content_hash": "text", "line_count": "integer", "char_count": "integer",
                "chunk_count": "integer", "edge_count": "integer", "status": "text", "active": "boolean",
                "embedding": "vector", "last_scanned_at": "timestamptz", "created_at": "timestamptz",
                "updated_at": "timestamptz"
            },
            "required": ["repo", "path"],
            "enums": {},
            "fat_columns": ["summary", "purpose_summary", "symbols", "imports", "links"],
            "safe_select": "id,repo,path,file_name,file_ext,language,item_type,runtime_role,summary,active,created_at,updated_at",
            "on_conflict": "path",
            "notes": "Semantic repo map component registry. Auto-sync source of truth for file meaning and wiring."
        },
        "repo_component_chunks": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {
                "id": "bigint", "repo": "text", "component_path": "text", "chunk_index": "integer",
                "chunk_type": "text", "start_line": "integer", "end_line": "integer", "summary": "text",
                "content": "text", "chunk_hash": "text", "token_estimate": "integer", "active": "boolean",
                "embedding": "vector", "last_scanned_at": "timestamptz", "created_at": "timestamptz",
                "updated_at": "timestamptz"
            },
            "required": ["repo", "component_path", "chunk_index"],
            "enums": {},
            "fat_columns": ["summary", "content"],
            "safe_select": "id,repo,component_path,chunk_index,chunk_type,start_line,end_line,summary,active,created_at,updated_at",
            "on_conflict": "component_path,chunk_index",
            "notes": "Semantic repo map chunks for retrieval."
        },
        "repo_component_edges": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {
                "id": "bigint", "repo": "text", "source_path": "text", "target_path": "text",
                "relation": "text", "source_symbol": "text", "target_symbol": "text",
                "evidence": "text", "weight": "float8", "active": "boolean", "embedding": "vector",
                "last_scanned_at": "timestamptz", "created_at": "timestamptz", "updated_at": "timestamptz"
            },
            "required": ["repo", "source_path", "target_path", "relation"],
            "enums": {},
            "fat_columns": ["evidence"],
            "safe_select": "id,repo,source_path,target_path,relation,weight,active,created_at,updated_at",
            "on_conflict": "source_path,target_path,relation,source_symbol,target_symbol",
            "notes": "Semantic repo map dependency edges."
        },
        "repo_scan_runs": {
            "pk": "id", "pk_type": "bigserial",
            "columns": {
                "id": "bigint", "repo": "text", "root_path": "text", "trigger": "text", "status": "text",
                "files_total": "integer", "files_changed": "integer", "components_upserted": "integer",
                "chunks_upserted": "integer", "edges_upserted": "integer", "duration_sec": "float8",
                "summary": "text", "error": "text", "payload": "jsonb", "created_at": "timestamptz"
            },
            "required": ["repo", "root_path", "trigger", "status"],
            "enums": {"status": ["ok", "partial", "error"]},
            "fat_columns": ["summary", "error", "payload"],
            "safe_select": "id,repo,trigger,status,files_total,files_changed,created_at",
            "notes": "Semantic repo map scan ledger."
        },
    }
}

from core_config import build_live_schema
_live = build_live_schema(SUPABASE_REF, SUPABASE_PAT)
if _live:
    _tombstones = _SB_SCHEMA.get("_tombstone", set())
    _tables     = _SB_SCHEMA.get("tables", {})
    for table, live_data in _live.items():
        if table in _tombstones:
            continue
        if table in _tables:
            _tables[table]["columns"] = live_data["columns"]
            # Also update safe_select to only include columns that actually exist
            existing_safe = _tables[table].get("safe_select", "")
            if existing_safe:
                valid_cols = [c for c in existing_safe.split(",") if c.strip() in live_data["columns"]]
                if valid_cols:
                    _tables[table]["safe_select"] = ",".join(valid_cols)
    print(f"[SCHEMA] Patched {len(_live)} tables in _SB_SCHEMA with live columns")

def _sb_schema(table: str) -> dict:
    """Return schema entry for a table, or empty dict if unknown."""
    return _SB_SCHEMA["tables"].get(table, {})


def _sel(table: str, extra_cols: list = None) -> str:
    """Return safe SELECT string from _SB_SCHEMA. Excludes fat_columns.
    extra_cols that are fat_columns are silently dropped — use _sel_force if needed.
    Falls back to 'id,created_at' for unknown tables."""
    schema = _SB_SCHEMA.get("tables", {}).get(table, {})
    safe = schema.get("safe_select", "id,created_at")
    if not extra_cols:
        return safe
    fat = set(schema.get("fat_columns", []))
    known = set(schema.get("columns", {}).keys())
    extra_valid = [c for c in extra_cols if c not in fat and (not known or c in known)]
    if not extra_valid:
        return safe
    base_cols = safe.split(",")
    all_cols = list(dict.fromkeys(base_cols + extra_valid))
    return ",".join(all_cols)


def _sel_force(table: str, cols: list) -> str:
    """SELECT string including specific columns regardless of fat status.
    Use when you genuinely need a fat column (e.g. content, reflection_text).
    Validates against live schema — drops unknown columns to prevent 400 errors."""
    schema = _SB_SCHEMA.get("tables", {}).get(table, {})
    known = set(schema.get("columns", {}).keys())
    if known:
        valid = [c for c in cols if c in known]
        return ",".join(valid) if valid else ",".join(cols)
    return ",".join(cols)

def _load_schema_registry():
    """Compat shim -- returns _SB_SCHEMA in old format for _validate_write."""
    # Rebuild old format on the fly from unified schema
    tables = {}
    for tname, tdef in _SB_SCHEMA["tables"].items():
        tables[tname] = {
            "pk_type": tdef.get("pk_type", ""),
            "columns": tdef.get("columns", {}),
            "required": tdef.get("required", []),
            "enums": tdef.get("enums", {}),
        }
    return {"tables": tables}

def _validate_write(table: str, data: dict) -> list:
    """Validate data dict against _SB_SCHEMA before any Supabase write.
    Returns list of error strings (empty = OK). Logs violations to Railway stdout.
    Checks: tombstone table, unknown table, unknown columns, required fields, enum values."""
    errors = []
    # Block tombstone tables
    if table in _SB_SCHEMA.get("_tombstone", set()):
        errors.append(f"TOMBSTONE_TABLE: '{table}' has been retired -- never query or write to it")
        print(f"[SCHEMA VIOLATION] {table}: TOMBSTONE blocked")
        return errors
    schema = _sb_schema(table)
    if not schema:
        # Unknown table — block with helpful hint instead of proceeding unvalidated
        known = sorted(_SB_SCHEMA.get("tables", {}).keys())
        errors.append(
            f"UNKNOWN_TABLE: '{table}' not in schema registry. "
            f"Known tables: {known}. "
            f"Common mistake: 'tasks' → 'task_queue'"
        )
        print(f"[SCHEMA VIOLATION] {table}: UNKNOWN_TABLE blocked")
        return errors
    known_cols = schema.get("columns", {})
    enums      = schema.get("enums", {})
    required   = schema.get("required", [])
    for col in data:
        if col not in known_cols:
            errors.append(f"UNKNOWN_COLUMN: '{col}' not in {table}. Known: {sorted(known_cols.keys())}")
    for col in required:
        if col not in data or data[col] is None:
            errors.append(f"MISSING_REQUIRED: '{col}' required in {table}")
    for col, allowed in enums.items():
        if col in data and data[col] is not None:
            if str(data[col]) not in [str(v) for v in allowed]:
                errors.append(f"INVALID_ENUM: '{col}'='{data[col]}' -- allowed: {allowed}")
    if errors:
        for e in errors: print(f"[SCHEMA VIOLATION] {table}: {e}")
    return errors

def _validate_read(table: str, select: str, filters: str = "") -> tuple:
    """Validate a read operation. Returns (safe_select, warnings_list).
    Blocks tombstone tables, auto-downgrades select=* on fat-column tables,
    validates requested columns, warns about missing id=gt.1 on bigserial PKs."""
    warnings = []
    if table in _SB_SCHEMA.get("_tombstone", set()):
        return None, [f"TOMBSTONE_TABLE: '{table}' is retired -- do not query"]
    schema = _sb_schema(table)
    if not schema:
        warnings.append(f"table '{table}' not in schema registry -- proceeding blind")
        return select, warnings
    sel = select.strip() if select and select.strip() else "*"
    fat = schema.get("fat_columns", [])
    safe = schema.get("safe_select", "*")
    if sel == "*" and fat:
        warnings.append(
            f"select=* auto-downgraded on '{table}' (fat: {fat}). "
            f"Using safe_select: '{safe}'. Pass explicit columns to override."
        )
        sel = safe
    else:
        requested = [c.strip() for c in sel.split(",") if c.strip()]
        known = set(schema["columns"].keys())
        bad = [c for c in requested if c not in known and "->" not in c and "(" not in c]
        fat_req = [c for c in requested if c in fat]
        if bad:
            warnings.append(f"unknown columns: {bad}. table '{table}' has: {sorted(known)}")
        if fat_req:
            warnings.append(
                f"fat column(s) selected: {fat_req} -- risk of response overflow at scale. "
                f"Recommended: '{safe}'"
            )
    pk_type = schema.get("pk_type", "")
    if pk_type == "bigserial" and filters and "id=gt." not in filters and "id=eq." not in filters:
        warnings.append(f"hint: '{table}' bigserial PK -- consider adding id=gt.1 to skip probe row")
    return sel, warnings


# -- MCP tool implementations -------------------------------------------------
def t_state(include_operating_context: str = "false"):
    """Read full system state. operating_context.json is NOT loaded by default (slow GitHub read).
    Pass include_operating_context=true to load it explicitly."""
    session = get_latest_session()
    counts  = get_system_counts()
    # Fetch open tasks from both main registries -- pending OR in_progress, ordered by priority
    pending = sb_get(
        "task_queue",
        "select=id,task,status,priority,source&source=in.(core_v6_registry,mcp_session)&status=in.(pending,in_progress)&order=priority.desc&limit=20"
    ) or []
    load_oc = str(include_operating_context).strip().lower() in ("true", "1", "yes")
    if load_oc:
        try:    operating_context = json.loads(gh_read("operating_context.json"))
        except Exception as e: operating_context = {"error": f"failed to load: {e}"}
    else:
        operating_context = None
    # E.5: cache SESSION.md -- it is static, no need for a GitHub fetch on every t_state call
    # Try to read from Supabase state_key first (written at last successful fetch)
    session_md = None
    try:
        cached_rows = sb_get("sessions",
            "select=summary&summary=like.*session_md_cache*&order=id.desc&limit=1",
            svc=True) or []
        if cached_rows:
            raw_cache = cached_rows[0].get("summary", "")
            prefix = "[state_update] session_md_cache: "
            if raw_cache.startswith(prefix):
                session_md = raw_cache[len(prefix):].strip()
    except Exception:
        pass
    if not session_md:
        try:
            session_md = gh_read("SESSION.md")[:5000]
            # Write to cache so future calls skip GitHub
            try:
                sb_post("sessions", {
                    "summary": f"[state_update] session_md_cache: {session_md}",
                    "actions": ["session_md cached from GitHub"],
                    "interface": "mcp",
                })
            except Exception:
                pass
        except Exception as e:
            session_md = f"SESSION.md unavailable: {e}"
    return {"last_session": session.get("summary", "No sessions yet."),
            "last_actions": session.get("actions", []),
            "last_session_ts": session.get("created_at", ""),
            "counts": counts, "pending_tasks": pending,
            "operating_context": operating_context,
            "operating_context_included": load_oc,
            "session_md": session_md}


def t_session_state_packet(session_id: str = "default", strict: str = "false", limit: str = "8") -> dict:
    """Return the canonical state packet plus the most relevant state_update signals."""
    try:
        lim = max(1, min(int(limit or 8), 20))
    except Exception:
        lim = 8
    try:
        packet = t_state_packet(session_id=session_id or "default", strict=strict)
        if not packet.get("ok"):
            return packet
        tracked_keys = [
            "last_real_signal_ts",
            "last_meta_learning_ts",
            "last_meta_training_ts",
            "last_causal_discovery_ts",
            "last_temporal_hwm_ts",
            "last_joint_training_ts",
            "last_research_ts",
            "last_public_source_ts",
            "last_router_policy_ts",
            "last_backup_ts",
            "simulation_task",
        ]
        latest_updates = packet.get("state_updates") or {}
        for key in tracked_keys:
            value = latest_updates.get(key)
            if value in (None, "", {}, []):
                try:
                    value = _latest_state_update_value(key).get("value")
                except Exception:
                    value = None
            if value not in (None, "", {}, []):
                latest_updates[key] = value
        recent_rows = (packet.get("state_update_rows") or [])[:lim]
        recent_updates = {}
        for row in recent_rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").strip()
            value = row.get("value")
            if key and key not in recent_updates and value not in (None, "", {}, []):
                recent_updates[key] = value
        tracked_updates = {}
        for key in tracked_keys:
            value = latest_updates.get(key)
            if value in (None, "", {}, []):
                try:
                    value = _latest_state_update_value(key).get("value")
                except Exception:
                    value = None
            if value not in (None, "", {}, []):
                tracked_updates[key] = value
        combined_updates = {**recent_updates, **tracked_updates}
        integrity = packet.get("state_update_integrity") or {}
        diversity_keys = sorted(set(combined_updates.keys()) | set(latest_updates.keys()))
        urgency_signals = []
        for key, value in combined_updates.items():
            text = f"{key} {value}".lower()
            if any(term in text for term in ("urgent", "priority", "escalat", "block", "deadline", "critical")):
                urgency_signals.append(key)
        importance_keys = []
        for key, value in combined_updates.items():
            text = f"{key} {value}".lower()
            if "verification" in text or "state_update" in text or "timestamp" in text or "_ts" in key:
                importance_keys.append(key)
        future_session_guidance = "capture_more_diverse_state_updates"
        if any("verification" in str(key).lower() for key in diversity_keys):
            future_session_guidance = "preserve_verification_context_for_future_sessions"
        elif urgency_signals:
            future_session_guidance = "surface_high_priority_updates_early"
        if integrity.get("future_count", 0) > 0:
            future_session_guidance = "clamp_future_state_timestamps_before_use"
        session_state = {
            "session_id": packet.get("session_id") or (session_id or "default"),
            "summary": (
                f"state_updates={len(combined_updates)} | "
                f"rows={len(recent_rows)} | "
                f"verified={bool((packet.get('verification') or {}).get('verified'))} | "
                f"diversity={len(diversity_keys)} | urgent={len(urgency_signals)} | "
                f"future_ts={integrity.get('future_count', 0)}"
            ),
            "state_update_keys": sorted(combined_updates.keys()),
            "state_update_values": combined_updates,
            "tracked_state_update_values": tracked_updates,
            "state_update_rows": recent_rows,
            "state_update_integrity": integrity,
            "state_update_signal_summary": {
                "signal_count": len(combined_updates),
                "frequency_hint": len(recent_rows),
                "importance_keys": importance_keys[:12],
                "urgent_keys": urgency_signals[:12],
                "last_real_signal_ts": combined_updates.get("last_real_signal_ts") or latest_updates.get("last_real_signal_ts") or "",
            },
            "session_diversity": {
                "distinct_keys": len(diversity_keys),
                "distinct_key_names": diversity_keys[:24],
                "urgency_signals": urgency_signals[:12],
                "future_session_guidance": future_session_guidance,
                "future_timestamp_keys": integrity.get("future_keys", [])[:12],
                "signal_frequency": len(recent_rows),
            },
            "latest_session": packet.get("latest_session") or {},
            "agentic_session": packet.get("agentic_session") or {},
            "checkpoint": packet.get("checkpoint") or {},
            "session_snapshot": packet.get("session_snapshot") or {},
            "session_continuity": packet.get("session_continuity") or {},
            "verification": packet.get("verification") or {},
        }
        return {
            "ok": True,
            "session_id": session_state["session_id"],
            "session_state": session_state,
            "state_packet": packet,
            "summary": session_state["summary"],
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "session_id": session_id or "default"}

def t_health():
    from core_config import GROQ_API_KEY, TELEGRAM_TOKEN
    from concurrent.futures import ThreadPoolExecutor, as_completed
    h = {"ts": datetime.utcnow().isoformat(), "components": {}}
    checks = {
        "supabase": lambda: sb_get("sessions", "select=id&limit=1"),
        "groq":     lambda: httpx.get("https://api.groq.com/openai/v1/models",
                             headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=5).raise_for_status(),
        "telegram": lambda: httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=5).raise_for_status(),
        "github":   lambda: gh_read("README.md"),
    }
    def _run(name, fn):
        try: fn(); return name, "ok"
        except Exception as e: return name, f"error:{e}"
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_run, name, fn): name for name, fn in checks.items()}
        for f in as_completed(futures):
            name, result = f.result()
            h["components"][name] = result
    h["overall"] = "ok" if all(v == "ok" for v in h["components"].values()) else "degraded"
    return h

def t_external_service_preflight(targets: str = "supabase,groq,telegram,github") -> dict:
    """Run a focused pre-flight health check for external services before risky writes/deploys."""
    try:
        wanted = [t.strip().lower() for t in (targets or "").split(",") if t.strip()]
        wanted = wanted or ["supabase", "groq", "telegram", "github"]
        health = t_health()
        components = health.get("components", {}) or {}
        checked = {name: components.get(name, "unknown") for name in wanted}
        blocked = [name for name, status in checked.items() if status != "ok"]
        return {
            "ok": not blocked,
            "targets": wanted,
            "checked": checked,
            "blocked": blocked,
            "overall": "ok" if not blocked else "degraded",
            "health": health,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "targets": [], "checked": {}, "blocked": ["preflight_error"], "overall": "degraded"}

def _require_external_service_preflight(targets: str, operation: str) -> dict | None:
    """Shared guard for risky write paths.

    Returns None when the requested services are healthy. Returns a structured
    error payload when the preflight fails so callers can stop before mutating
    external state.
    """
    preflight = t_external_service_preflight(targets)
    if preflight.get("ok"):
        return None
    return {"ok": False, "error": f"{operation} preflight failed", "preflight": preflight}

def t_constitution():
    try:
        with open("constitution.txt") as f: txt = f.read()
    except: txt = gh_read("constitution.txt")
    return {"constitution": txt, "immutable": True}

def t_search_kb(query="", domain="", limit=10):
    """Search knowledge_base via semantic vector search (primary).
    AGI-05: increments access_count + updates last_accessed on every hit (fire-and-forget)."""
    lim = int(limit) if limit else 10
    if not query:
        qs = f"select=id,domain,topic,instruction,content,confidence&limit={lim}&id=gt.1"
        if domain and domain not in ("all", ""):
            qs += f"&domain=eq.{domain}"
        rows = sb_get("knowledge_base", qs)
    else:
        from core_semantic import search as sem_search
        filters = f"&domain=eq.{domain}" if domain and domain not in ("all", "") else ""
        rows = sem_search("knowledge_base", query, limit=lim, filters=filters)

    # C.2: batch access tracking
    try:
        if rows:
            now_ts = datetime.utcnow().isoformat()
            ids = [str(r["id"]) for r in rows if r.get("id") and r["id"] != 1]
            if ids:
                id_list = ",".join(ids)
                try:
                    sb_patch("knowledge_base", f"id=in.({id_list})",
                             {"last_accessed": now_ts})
                except Exception:
                    pass
    except Exception:
        pass
    return rows


def _group_memory_hits(rows: list) -> dict:
    grouped = {}
    for row in rows or []:
        table = row.get("semantic_table") or row.get("_table") or "knowledge_base"
        grouped.setdefault(table, []).append(row)
    return grouped


from core_tools_graph import (
    CausalGraph,
    CausalGraphInference,
    DynamicRelationalGraph,
    t_causal_graph,
    t_causal_graph_inference,
    t_dynamic_relational_graph,
)
from core_tools_memory import (
    StateEvaluator,
    t_system_verification_packet,
    t_evaluate_state,
    t_generate_synthetic_data,
    t_state_consistency_check,
    t_state_packet,
    t_reasoning_packet,
    t_search_memory,
)
from core_tools_world_model import (
    CausalMappingModule,
    AdaptiveTemporalFilter,
    DynamicReplayBuffer,
    DynamicGatingLayer,
    GatingNetwork,
    HierarchicalSearchController,
    HierarchicalSearchTree,
    PrincipleSearchModule,
    MetaContextualRouter,
    MetaLearner,
    MonteCarloTreeSearch,
    PredictiveStateRepresentation,
    StateReconciliationBuffer,
    SimulatedCritic,
    TemporalAttention,
    TemporalHierarchicalWorldModel,
    WorldModelInterface,
    WorldModel,
    t_adaptive_temporal_filter,
    t_causal_graph_data_generator,
    t_causal_mapping_module,
    t_dynamic_gating_layer,
    t_dynamic_replay_buffer,
    t_dynamic_router,
    t_hierarchical_search_controller,
    t_hierarchical_search_tree,
    t_meta_learner,
    t_meta_contextual_router,
    t_monte_carlo_tree_search,
    t_module_assessment_packet,
    t_hierarchical_gated_neuro_symbolic_world_model,
    t_domain_invariant_feature_packet,
    t_principle_search_module,
    t_predict_with_uncertainty,
    t_predictive_state_representation,
    t_state_reconciliation_buffer,
    t_simulated_critic,
    t_temporal_attention,
    t_temporal_hierarchical_world_model,
    t_world_model_interface,
    t_world_model,
    t_gating_network,
)
from core_tools_governance import (
    ToolRelianceAdvisor,
    t_tool_reliance_assessor,
)
from core_tools_code_reader import (
    build_code_reading_packet,
    t_code_read_packet,
)
from core_causal_principle_discovery import (
    t_causal_principle_discovery,
)
from core_repo_map import (
    apply_repo_map_schema,
    build_repo_component_packet,
    build_repo_graph_packet,
    render_repo_map_status_report,
    repo_map_loop,
    repo_map_status,
    run_repo_map_cycle,
    t_repo_component_packet,
    t_repo_graph_packet,
    t_repo_map_status,
    t_repo_map_sync,
)
from core_public_evidence import t_public_evidence_packet
def t_owner_review_cluster_packet(limit: int = 5, persist: str = "true") -> dict:
    """Canonical owner-review cluster packet, exposed through core_tools for agentic routing."""
    from core_proposal_router import owner_review_cluster_packet as _owner_review_cluster_packet

    persist_bool = str(persist).strip().lower() not in {"0", "false", "no", "off", ""}
    return _owner_review_cluster_packet(limit=int(limit or 5), persist=persist_bool)


def t_owner_review_cluster_close(
    cluster_id: str = "",
    cluster_key: str = "",
    outcome: str = "applied",
    reason: str = "",
    reviewed_by: str = "owner",
    dry_run: str = "false",
) -> dict:
    """Batch-close an owner-review cluster through the router, exposed via the core tool registry."""
    from core_proposal_router import owner_review_cluster_close as _owner_review_cluster_close

    dry_run_bool = str(dry_run).strip().lower() in {"1", "true", "yes", "on"}
    return _owner_review_cluster_close(
        cluster_id=cluster_id,
        cluster_key=cluster_key,
        outcome=outcome,
        reason=reason,
        reviewed_by=reviewed_by,
        dry_run=dry_run_bool,
    )

from core_tools_task import (
    TaskPacket,
    TaskVerificationBundle,
    build_task_error_packet,
    build_task_packet,
    build_task_tracking_packet,
    t_task_error_packet,
    t_task_packet,
    t_task_verification_bundle,
    t_task_tracking_packet,
)
class MetaRepresentation:
    """Serializable meta representation for passing structured state between modules.

    This class is intentionally lightweight:
    - JSON-friendly (to_dict/from_dict)
    - mergeable (overlay/union)
    - no external deps (numpy/torch not required)
    """

    def __init__(
        self,
        name: str = "",
        version: int = 1,
        features: dict | None = None,
        metadata: dict | None = None,
        embedding: list[float] | None = None,
    ):
        self.name = (name or "").strip() or "meta_representation"
        try:
            self.version = int(version or 1)
        except Exception:
            self.version = 1
        self.features = features if isinstance(features, dict) else {}
        self.metadata = metadata if isinstance(metadata, dict) else {}
        self.embedding = embedding if isinstance(embedding, list) else None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "features": self.features,
            "metadata": self.metadata,
            "embedding": self.embedding,
        }

    @classmethod
    def from_dict(cls, value) -> "MetaRepresentation":
        if isinstance(value, MetaRepresentation):
            return value
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                value = {"name": value}
        if not isinstance(value, dict):
            value = {"name": str(value)}
        return cls(
            name=value.get("name") or "",
            version=value.get("version") or 1,
            features=value.get("features") if isinstance(value.get("features"), dict) else {},
            metadata=value.get("metadata") if isinstance(value.get("metadata"), dict) else {},
            embedding=value.get("embedding") if isinstance(value.get("embedding"), list) else None,
        )

    def merge(self, other, strategy: str = "overlay") -> "MetaRepresentation":
        """Merge another MetaRepresentation into this one.

        strategy:
        - overlay: other.features overwrites self.features
        - union:   keep existing keys, only add missing keys from other
        """
        other_obj = MetaRepresentation.from_dict(other)
        strategy = (strategy or "overlay").strip().lower()
        merged = MetaRepresentation(
            name=self.name,
            version=max(self.version, other_obj.version),
            features=dict(self.features),
            metadata=dict(self.metadata),
            embedding=self.embedding,
        )
        if strategy == "union":
            for k, v in (other_obj.features or {}).items():
                if k not in merged.features:
                    merged.features[k] = v
        else:
            merged.features.update(other_obj.features or {})
        merged.metadata.update(other_obj.metadata or {})
        if merged.embedding is None and other_obj.embedding is not None:
            merged.embedding = other_obj.embedding
        return merged


def t_meta_representation(
    op: str = "new",
    name: str = "",
    version: str = "1",
    features: str = "",
    metadata: str = "",
    a: str = "",
    b: str = "",
    strategy: str = "overlay",
) -> dict:
    """Create/merge/validate a MetaRepresentation payload.

    op=new:    build from name/version/features/metadata
    op=merge:  merge payload a with payload b using strategy overlay|union
    op=validate: parse payload a and return normalized form
    """
    try:
        op = (op or "new").strip().lower()

        def _parse_obj(v: str) -> dict:
            if not v:
                return {}
            if isinstance(v, dict):
                return v
            if isinstance(v, str):
                try:
                    parsed = json.loads(v)
                    return parsed if isinstance(parsed, dict) else {"value": parsed}
                except Exception:
                    return {"value": v}
            return {"value": str(v)}

        if op == "merge":
            left = MetaRepresentation.from_dict(a)
            merged = left.merge(b, strategy=strategy)
            return {"ok": True, "op": "merge", "meta": merged.to_dict()}
        if op == "validate":
            meta = MetaRepresentation.from_dict(a)
            return {"ok": True, "op": "validate", "meta": meta.to_dict()}

        # op=new (default)
        try:
            ver = int(version or 1)
        except Exception:
            ver = 1
        meta = MetaRepresentation(
            name=name,
            version=ver,
            features=_parse_obj(features),
            metadata=_parse_obj(metadata),
        )
        return {"ok": True, "op": "new", "meta": meta.to_dict()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_task_similarity_metric(task_a: str = "", task_b: str = "") -> dict:
    """Compare two task JSON blobs or plain text blobs."""
    try:
        from core_train import task_similarity_metric as _task_similarity_metric

        def _parse(v):
            if not v:
                return {}
            if isinstance(v, dict):
                return v
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except Exception:
                    return {"title": v}
            return {"title": str(v)}

        a = _parse(task_a)
        b = _parse(task_b)
        return {"ok": True, "similarity": _task_similarity_metric(a, b)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_novelty_assessment(experience: str = "", reference_memory: str = "", limit: str = "25") -> dict:
    """Assess novelty of an experience against recent memory/task representations."""
    try:
        from core_train import novelty_assessment_module as _novelty_assessment_module
        try:
            lim = max(5, min(int(limit or 25), 50))
        except Exception:
            lim = 25

        refs = []
        if reference_memory:
            try:
                parsed = json.loads(reference_memory)
                if isinstance(parsed, list):
                    refs = [item for item in parsed if item]
                elif isinstance(parsed, dict):
                    refs = [parsed]
            except Exception:
                refs = []

        def _parse(v):
            if not v:
                return {}
            if isinstance(v, dict):
                return v
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except Exception:
                    return {"title": v}
            return {"title": str(v)}

        return _novelty_assessment_module(_parse(experience), reference_memory=refs or None, limit=lim)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_consolidation_manager(limit: str = "25", similarity_threshold: str = "0.62") -> dict:
    """Cluster similar queued tasks and return a consolidation summary."""
    try:
        from core_train import ConsolidationManager as _ConsolidationManager
        lim = max(1, min(int(limit or 25), 50))
        thresh = max(0.1, min(float(similarity_threshold or 0.62), 0.95))
        return _ConsolidationManager(similarity_threshold=thresh).run(limit=lim)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_active_learning_strategy(strategy_name: str = "novelty_priority", budget: str = "5", limit: str = "25", similarity_threshold: str = "0.62") -> dict:
    """Select high-value tasks for active learning using a pluggable strategy."""
    try:
        from core_train import ActiveLearningStrategy as _ActiveLearningStrategy
        bud = max(1, min(int(budget or 5), 25))
        lim = max(1, min(int(limit or 25), 50))
        thresh = max(0.1, min(float(similarity_threshold or 0.62), 0.95))
        return _ActiveLearningStrategy(strategy_name=strategy_name, budget=bud, similarity_threshold=thresh).run(limit=lim)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_get_mistakes(domain="", limit=10):
    try: lim = int(limit) if limit else 10
    except: lim = 10
    # C.3: add id=gt.1 filter (consistent with all other bigserial table queries)
    qs = f"select={_sel_force('mistakes', ['domain','context','what_failed','correct_approach','severity','root_cause','how_to_avoid'])}&order=created_at.desc&limit={lim}&id=gt.1"
    if domain and domain not in ("all", ""): qs += f"&domain=eq.{domain}"
    return sb_get("mistakes", qs, svc=True)

def t_update_state(key="", value="", reason=""):
    try:
        preflight = _require_external_service_preflight("supabase", "update_state")
        if preflight:
            return preflight
        key_text = str(key or "").strip()
        value_text = str(value or "").strip()
        if key_text and ("_ts" in key_text or "timestamp" in key_text.lower()):
            try:
                from datetime import datetime, timezone
                raw = value_text.replace("Z", "+00:00") if value_text.endswith("Z") else value_text
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
                now = datetime.now(timezone.utc)
                if dt > now:
                    value_text = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass
        note = reason or ""
        if value_text != str(value or ""):
            note = "clamped future timestamp" if not note else f"{note}; clamped future timestamp"
        ok = sb_post("sessions", {"summary": f"[state_update] {key_text}: {value_text}",
                              "actions": [f"{key_text}={value_text} - {note}".strip(" -")], "interface": "mcp"})
        return {"ok": ok, "key": key_text or key, "value": value_text or value}

    except Exception as e:
        return {"ok": False, "error": str(e)}


def _kb_normalize_provenance(source_type: str = "", source_ref: str = "") -> tuple[str, str]:
    """Return canonical KB provenance fields."""
    source_type = (source_type or "").strip().lower()
    source_ref = (source_ref or "").strip()
    allowed = {"manual", "ingested", "evolved", "session"}
    alias_map = {
        "mcp_session": "session",
        "mcp": "session",
        "claude_desktop": "manual",
        "core_discovered": "evolved",
        "cold_processor": "evolved",
        "training_phase": "ingested",
        "training_plan": "ingested",
        "meta_learning": "ingested",
    }
    source_type = alias_map.get(source_type, source_type or "session")
    if source_type not in allowed:
        source_type = "session"
    if not source_ref:
        source_ref = "mcp_session"
    return source_type, source_ref


def _kb_entry_verification_packet(
    domain: str = "",
    topic: str = "",
    instruction: str = "",
    content: str = "",
    confidence: str = "",
    source_type: str = "",
    source_ref: str = "",
    require_exact_match: bool = True,
) -> dict:
    """Verify a KB row exists and matches the canonical write contract."""
    try:
        rows = sb_get(
            "knowledge_base",
            f"select=id,domain,topic,instruction,content,confidence,source,source_type,source_ref,active,updated_at&domain=eq.{domain}&topic=eq.{topic}&order=id.desc&limit=3",
            svc=True,
        ) or []
        if not rows:
            return {
                "ok": True,
                "blocked": True,
                "verified": False,
                "verification_score": 0.0,
                "passed_checks": ["kb_row_missing"],
                "failed_checks": ["kb_row_missing"],
                "warnings": [],
                "summary": f"{domain}/{topic}: KB row missing",
            }

        row = rows[0]
        canon_source_type, canon_source_ref = _kb_normalize_provenance(source_type, source_ref)
        passed = ["kb_row_found"]
        failed = []
        warnings = []

        if (row.get("domain") or "") != domain:
            failed.append("domain_mismatch")
        else:
            passed.append("domain_match")
        if (row.get("topic") or "") != topic:
            failed.append("topic_mismatch")
        else:
            passed.append("topic_match")

        if instruction and (row.get("instruction") or "").strip() != instruction.strip():
            if require_exact_match:
                failed.append("instruction_mismatch")
            else:
                warnings.append("instruction_drift")
        elif instruction:
            passed.append("instruction_match")

        if content and (row.get("content") or "").strip() != content.strip():
            if require_exact_match:
                failed.append("content_mismatch")
            else:
                warnings.append("content_drift")
        elif content:
            passed.append("content_match")

        row_conf = str(row.get("confidence") or "")
        if confidence and row_conf and row_conf != str(confidence):
            warnings.append(f"confidence_mismatch:{row_conf}->{confidence}")
        elif confidence:
            passed.append("confidence_match")

        row_source_type = (row.get("source_type") or "").strip().lower()
        row_source_ref = (row.get("source_ref") or "").strip()
        if row_source_type != canon_source_type:
            failed.append("source_type_mismatch")
        else:
            passed.append("source_type_match")
        if row_source_ref != canon_source_ref:
            failed.append("source_ref_mismatch")
        else:
            passed.append("source_ref_match")

        verified = len(failed) == 0
        score = 1.0
        score -= 0.35 if failed else 0.0
        score -= 0.05 * len(warnings)
        score = max(0.0, round(score, 2))
        blocked = not verified or score < 0.8

        return {
            "ok": True,
            "blocked": blocked,
            "verified": verified,
            "verification_score": score,
            "passed_checks": passed,
            "failed_checks": failed,
            "warnings": warnings,
            "row": {
                "id": row.get("id"),
                "domain": row.get("domain"),
                "topic": row.get("topic"),
                "source_type": row.get("source_type"),
                "source_ref": row.get("source_ref"),
                "confidence": row.get("confidence"),
                "active": row.get("active"),
                "updated_at": row.get("updated_at"),
            },
            "summary": (
                f"{domain}/{topic}: {'verified' if verified else 'unverified'} "
                f"(warnings={len(warnings)}, failed={len(failed)})"
            ),
        }
    except Exception as exc:
        return {
            "ok": True,
            "blocked": True,
            "verified": False,
            "verification_score": 0.0,
            "passed_checks": [],
            "failed_checks": ["kb_verification_exception"],
            "warnings": [str(exc)],
            "summary": f"{domain}/{topic}: KB verification error",
    }


def _kb_refresh_freshness_markers(
    source_type: str = "",
    source_ref: str = "",
    domain: str = "",
    topic: str = "",
) -> dict:
    """Project KB provenance into state markers so freshness is visible system-wide."""
    try:
        markers = {}
        now_ts = datetime.utcnow().isoformat()
        canon_source_type, canon_source_ref = _kb_normalize_provenance(source_type, source_ref)
        reason = f"kb:{domain}/{topic}:{canon_source_type}:{canon_source_ref}"
        if canon_source_type == "ingested":
            markers["last_research_ts"] = now_ts
            markers["last_public_source_ts"] = now_ts
        elif canon_source_type == "session":
            markers["last_real_signal_ts"] = now_ts
        if not markers:
            return {"ok": True, "updated": False, "markers": {}, "summary": "kb_freshness: noop"}
        applied = {}
        for key, value in markers.items():
            try:
                res = t_update_state(key=key, value=value, reason=reason)
            except Exception as exc:
                res = {"ok": False, "error": str(exc)}
            applied[key] = res
        return {
            "ok": True,
            "updated": True,
            "markers": markers,
            "applied": applied,
            "summary": f"kb_freshness: updated={len(markers)}",
        }
    except Exception as exc:
        return {"ok": False, "updated": False, "error": str(exc), "summary": f"kb_freshness error: {exc}"}


def t_kb_entry_packet(domain: str = "", topic: str = "", instruction: str = "",
                      content: str = "", confidence: str = "medium",
                      source_type: str = "", source_ref: str = "") -> dict:
    """Canonical verification packet for KB writes."""
    if not domain or not topic:
        return {"ok": False, "error": "domain and topic are required"}
    canon_source_type, canon_source_ref = _kb_normalize_provenance(source_type, source_ref)
    return _kb_entry_verification_packet(
        domain=domain,
        topic=topic,
        instruction=instruction,
        content=content,
        confidence=confidence,
        source_type=canon_source_type,
        source_ref=canon_source_ref,
    )
def t_add_knowledge(domain="", topic="", instruction="", content="", tags="", confidence="medium", source_type="", source_ref=""):
    """Add knowledge entry. instruction = behavioral directive for CORE (primary). content = supporting detail. At least one required. source_type=manual|ingested|evolved|session. source_ref=URL or session_id."""
    if not instruction and not content:
        return {"ok": False, "error": "At least one of instruction or content is required"}
    preflight = _require_external_service_preflight("supabase", "add_knowledge")
    if preflight:
        return preflight
    # Normalize early so duplicate handling and verification use the same canonical provenance.
    if isinstance(tags, list):
        tags_list = [str(t).strip() for t in tags if t]
    elif isinstance(tags, str) and tags.strip():
        tags_list = [t.strip() for t in tags.split(",") if t.strip()]
    else:
        tags_list = []
    VALID_CONFIDENCE = {"low", "medium", "high", "proven"}
    if str(confidence) not in VALID_CONFIDENCE:
        try:
            v = float(confidence)
            confidence = "proven" if v >= 0.9 else "high" if v >= 0.7 else "medium" if v >= 0.4 else "low"
            print(f"[SCHEMA] confidence coerced from float {v} -> '{confidence}'")
        except (TypeError, ValueError):
            confidence = "medium"
            print(f"[SCHEMA] confidence invalid, defaulting to 'medium'")
    canon_source_type, canon_source_ref = _kb_normalize_provenance(source_type, source_ref)
    # TASK-27.B: Contradiction + duplicate check before insert
    try:
        existing = sb_get("knowledge_base",
            f"select={_sel_force('knowledge_base', ['instruction','content'])}&domain=eq.{domain}&topic=eq.{topic}&limit=1",
            svc=True)
        if existing:
            ex = existing[0]
            ex_instr    = (ex.get("instruction") or "").strip().lower()
            new_instr   = (instruction or "").strip().lower()
            ex_content  = (ex.get("content") or "").strip()
            new_content = (content or "").strip()
            # Check 1: instruction conflict -- block and alert
            if ex_instr and new_instr and ex_instr != new_instr:
                notify(f"[KB CONFLICT] {domain}/{topic}\nExisting: {ex_instr[:120]}\nProposed: {new_instr[:120]}\nBlocked -- use kb_update.")
                return {"ok": False, "conflict": True, "conflict_type": "instruction",
                        "action": "blocked",
                        "existing_instruction": ex.get("instruction", "")[:200],
                        "existing_content": ex_content[:200],
                            "message": "Instruction contradicts existing KB entry. Use kb_update to overwrite."}
            # Check 2: exact duplicate -- skip silently
            if ex_instr == new_instr and ex_content.lower() == new_content.lower():
                _kb_refresh_freshness_markers(
                    source_type=canon_source_type,
                    source_ref=canon_source_ref,
                    domain=domain,
                    topic=topic,
                )
                verification = _kb_entry_verification_packet(
                    domain=domain,
                    topic=topic,
                    instruction=instruction or "",
                    content=content or "",
                    confidence=confidence,
                    source_type=canon_source_type,
                    source_ref=canon_source_ref,
                )
                return {
                    "ok": True,
                    "action": "skipped_duplicate",
                    "topic": topic,
                    "verified": bool(verification.get("verified")),
                    "verification_packet": verification,
                }
            # Check 3: content conflict (substantial differing content, no instruction change)
            if ex_content and new_content and ex_content.lower() != new_content.lower():
                if len(ex_content) > 50 and len(new_content) > 50:
                    notify(f"[KB CONTENT CONFLICT] {domain}/{topic}\nExisting: {ex_content[:120]}\nProposed: {new_content[:120]}\nBlocked -- use kb_update.")
                    return {"ok": False, "conflict": True, "conflict_type": "content",
                            "action": "blocked",
                            "existing_instruction": ex.get("instruction", "")[:200],
                            "existing_content": ex_content[:200],
                    "message": "Content differs from existing KB entry. Use kb_update to overwrite."}
    except Exception:
        pass  # Non-fatal -- proceed with insert if check fails
    row = {
        "domain": domain,
        "topic": topic,
        "instruction": instruction or None,
        "content": content or "",
        "confidence": confidence,
        "tags": tags_list,
        "source": "mcp_session",
        "source_type": canon_source_type,
        "source_ref": canon_source_ref,
    }
    # Schema validation before write
    errs = _validate_write("knowledge_base", row)
    if errs:
        return {"ok": False, "topic": topic, "error": f"Schema violation: {errs}"}
    try:
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/knowledge_base", headers=_sbh(True), json=row, timeout=15)
        if not r.is_success:
            err_text = f"Supabase {r.status_code}: {r.text[:300]}"
            err_blob = f"{r.status_code} {r.text}".lower()
            duplicate_hint = (
                r.status_code == 409
                or "duplicate key value violates unique constraint" in err_blob
                or "duplicate" in err_blob
                or "unique violation" in err_blob
                or "on_conflict" in err_blob
            )
            if duplicate_hint:
                retry = t_kb_update(
                    domain=domain,
                    topic=topic,
                    instruction=instruction or "",
                    content=content or "",
                    confidence=confidence,
                    source_type=canon_source_type,
                    source_ref=canon_source_ref,
                )
                if retry.get("ok"):
                    retry.setdefault("action", "upserted_via_kb_update")
                    retry.setdefault("topic", topic)
                    return retry
                return {
                    "ok": False,
                    "topic": topic,
                    "error": retry.get("error") or err_text,
                    "action": "kb_update_retry_failed",
                }
            return {"ok": False, "topic": topic, "error": err_text}
        verification = _kb_entry_verification_packet(
            domain=domain,
            topic=topic,
            instruction=instruction or "",
            content=content or "",
            confidence=confidence,
            source_type=canon_source_type,
            source_ref=canon_source_ref,
        )
        freshness = _kb_refresh_freshness_markers(
            source_type=canon_source_type,
            source_ref=canon_source_ref,
            domain=domain,
            topic=topic,
        )
        return {
            "ok": bool(verification.get("ok") and verification.get("verified") and not verification.get("blocked")),
            "topic": topic,
            "verified": bool(verification.get("verified")),
            "verification_packet": verification,
            "freshness_packet": freshness,
        }
    except Exception as e:
        return {"ok": False, "topic": topic, "error": str(e)}

def t_set_simulation(instruction: str) -> dict:
    """Set a custom simulation task for the background researcher.
    CORE crafts the Groq prompts from your instruction and stores them.
    The background researcher loops on this every 60 min until you change it.
    Call with empty instruction to reset to default 1M user simulation.
    """
    try:
        preflight = _require_external_service_preflight("supabase", "set_simulation")
        if preflight:
            return preflight
        instruction = (instruction or "").strip()
        if not instruction:
            # Clear custom simulation -- reset to default
            ok = sb_post("sessions", {
                "summary": "[state_update] simulation_task: null",
                "actions": ["simulation_task cleared -- reset to default 1M user simulation"],
                "interface": "mcp"
            })
            notify("Simulation reset to default (1M user population simulation)")
            return {"ok": ok, "cleared": True, "message": "Reset to default simulation"}

        # Craft system prompt
        system_prompt = (
            "You are a senior researcher at an AGI self-improvement lab. "
            "Your job is to analyze CORE's live runtime context and extract high-signal patterns "
            "that will improve CORE's behavior, reasoning, and architecture. "
            "Be specific, grounded in the context provided, and adversarial where needed. "
            "Output MUST be valid JSON: "
            '{"domain": "code|db|bot|mcp|training|kb|general", '
            '"patterns": ["pattern1", "pattern2", "pattern3"], '
            '"gaps": "1-2 sentences on what CORE is structurally missing", '
            '"summary": "1 sentence research finding"} '
            "Output ONLY valid JSON, no preamble."
        )

        # Craft user prompt -- dynamic context injected at runtime by _run_simulation_batch
        # NOTE: instruction appears once only -- no redundant suffix
        user_prompt_template = (
            f"{instruction}\n\n"
            "CORE live context (use this as your research data):\n"
            "{{RUNTIME_CONTEXT}}\n\n"
            "Run your research. Output only valid JSON."
        )

        task = {
            "instruction": instruction,
            "system_prompt": system_prompt,
            "user_prompt_template": user_prompt_template,
            "set_at": __import__('datetime').datetime.utcnow().isoformat(),
        }

        ok = sb_post("sessions", {
            "summary": f"[state_update] simulation_task: {json.dumps(task)}",
            "actions": [f"simulation_task set: {instruction[:200]}"],
            "interface": "mcp"
        })
        if ok:
            notify(f"Simulation task set\nScenario: {instruction[:200]}\nBackground researcher will use this every 60 min.")
        return {"ok": ok, "instruction": instruction, "message": "Simulation task stored. Background researcher will pick it up on next cycle."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_log_mistake(context="", what_failed="", correct_approach="", domain="general", root_cause="", how_to_avoid="", severity="medium"):
    # A.2: dedup guard -- skip if identical what_failed+domain logged in last 24h
    try:
        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        _wf = (what_failed or "")[:40].replace("'", "")
        from core_semantic import search as _sem
        existing = _sem("mistakes", _wf, limit=1, threshold=0.92,
            filters=f"&domain=eq.{domain}") or []
        if existing:
            return {"ok": True, "action": "skipped_duplicate", "hint": "identical mistake already logged in last 24h"}
    except Exception:
        pass  # dedup failure is non-fatal -- proceed with write
    preflight = _require_external_service_preflight("supabase", "log_mistake")
    if preflight:
        return preflight
    ok = sb_post("mistakes", {"domain": domain, "context": context, "what_failed": what_failed,
                              "correct_approach": correct_approach, "root_cause": root_cause or what_failed,
                              "how_to_avoid": how_to_avoid or correct_approach, "severity": severity, "tags": []})
    return {"ok": ok}

def t_read_file(path, repo="", start_line="", end_line=""):
    """Read a GitHub file. Optional start_line/end_line for range. Cap 8000 chars with truncated flag. Use gh_read_lines for large files."""
    try:
        raw = gh_read(path, repo or GITHUB_REPO)
        lines = raw.splitlines(keepends=True)
        total = len(lines)
        if start_line or end_line:
            s = max(0, int(start_line) - 1) if start_line else 0
            e = int(end_line) if end_line else total
            lines = lines[s:e]
            raw = "".join(lines)
        truncated = len(raw) > 8000
        result = {"ok": True, "content": raw[:8000], "total_line_count": total, "truncated": truncated}
        # D.2: prominent truncation warning -- CORE must not use truncated content for patches
        if truncated:
            result["truncation_warning"] = (
                "TRUNCATED at 8000 chars. Do NOT use this output to build old_str for patches -- "
                "content is incomplete. Use gh_read_lines(start_line, end_line) to get the exact "
                "section you need before patching."
            )
        return result
    except Exception as e: return {"ok": False, "error": str(e)}

def t_write_file(path="", content="", message="", repo=""):
    """Write file to GitHub repo - FULL OVERWRITE. Use for NEW files only.
    GUARD: blocked for core_main.py and core_tools.py - use patch_file or gh_search_replace for surgical edits."""
    preflight = _require_external_service_preflight("github", "write_file")
    if preflight:
        return preflight
    # D.4: expand blocked set -- core_train.py and core_config.py also critical, full overwrite risk
    blocked = {"core_main.py", "core_tools.py", "core_train.py", "core_config.py"}
    clean_path = path.strip().lstrip("/")
    if (repo or GITHUB_REPO) == GITHUB_REPO and clean_path in blocked:
        return {
            "ok": False,
            "error": f"BLOCKED: write_file cannot overwrite {clean_path} (full overwrite = corruption risk). "
                     "Use patch_file or gh_search_replace for surgical edits."
        }
    if clean_path.endswith(".py"):
        import tempfile, py_compile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
            f.write(content); tmp = f.name
        try:
            py_compile.compile(tmp, doraise=True)
        except py_compile.PyCompileError as e:
            os.unlink(tmp)
            return {"ok": False, "error": f"Syntax error: {e}"}
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    ok = gh_write(path, content, message, repo or GITHUB_REPO)
    if ok: notify(f"MCP write: `{clean_path}`")
    return {"ok": ok, "path": path}

def t_notify(message, level="info"):
    icons = {"info": "i", "warn": "!", "alert": "ALERT", "ok": "OK"}
    return {"ok": notify(f"{icons.get(level, '>')} CORE\n{message}")}

# _TABLE_SCHEMAS removed -- unified into _SB_SCHEMA above.
# Legacy reference kept as alias for any internal code still using it.
_TABLE_SCHEMAS = _SB_SCHEMA["tables"]  # type: ignore
# A.4: _TABLE_SCHEMAS_REMOVED deleted -- was ~100 lines of orphaned dead code, never referenced.

_SB_SCHEMA["tables"].update({
    "memory": {
        "pk": "key", "pk_type": "text",
        "columns": {
            "key": "text", "category": "text", "value": "text",
            "created_at": "timestamptz", "updated_at": "timestamptz",
        },
        "required": ["key"],
        "enums": {},
        "fat_columns": ["value"],
        "safe_select": "key,category,value,created_at,updated_at",
        "on_conflict": "key",
        "notes": "Personal memory key/value store used by brain and task-state helpers.",
    },
    "playbook": {
        "pk": "id", "pk_type": "bigserial",
        "columns": {
            "id": "bigint", "topic": "text", "method": "text", "why_best": "text",
            "supersedes": "text", "previous_method": "text", "version": "integer",
            "tags": "text[]", "created_at": "timestamptz", "updated_at": "timestamptz",
        },
        "required": ["topic"],
        "enums": {},
        "fat_columns": ["method", "why_best", "supersedes", "previous_method", "tags"],
        "safe_select": "id,topic,method,version,created_at,updated_at",
        "on_conflict": "topic",
        "notes": "Procedure playbook used by brain-first routing and memory export tools.",
    },
    "output_reflections": {
        "pk": "id", "pk_type": "bigserial",
        "columns": {
            "id": "bigint", "session_id": "bigint", "source": "text",
            "critique_score": "float8", "verdict": "text", "gap": "text",
            "gap_domain": "text", "new_behavior": "text", "evo_worthy": "boolean",
            "prompt_patch": "text", "created_at": "timestamptz",
        },
        "required": [],
        "enums": {},
        "fat_columns": ["gap", "new_behavior", "prompt_patch"],
        "safe_select": "id,session_id,source,critique_score,verdict,gap_domain,created_at",
        "notes": "Reflection output ledger produced by reviewer/critic flows.",
    },
    "agentic_sessions": {
        "pk": "id", "pk_type": "bigserial",
        "columns": {
            "id": "bigint", "session_id": "text", "state": "jsonb", "step_index": "integer",
            "current_step": "text", "completed_steps": "jsonb", "action_log": "jsonb",
            "last_updated": "timestamptz", "goal": "text", "status": "text",
            "chat_id": "text", "created_at": "timestamptz",
        },
        "required": ["session_id"],
        "enums": {},
        "fat_columns": ["state", "completed_steps", "action_log"],
        "safe_select": "id,session_id,step_index,current_step,last_updated,status,created_at",
        "notes": "Resumable agentic session state for loop tracking and action logs.",
    },
    "project_context": {
        "pk": "id", "pk_type": "bigserial",
        "columns": {
            "id": "bigint", "project_id": "text", "prepared_by": "text",
            "context_md": "text", "consumed": "boolean", "prepared_at": "timestamptz",
            "consumed_at": "timestamptz", "created_at": "timestamptz",
        },
        "required": ["project_id"],
        "enums": {},
        "fat_columns": ["context_md"],
        "safe_select": "id,project_id,prepared_by,consumed,prepared_at,consumed_at",
        "notes": "Prepared project context waiting for Claude Desktop to consume.",
    },
    "backlog": {
        "pk": "id", "pk_type": "bigserial",
        "columns": {
            "id": "bigint", "title": "text", "type": "text", "priority": "integer",
            "description": "text", "domain": "text", "effort": "text", "impact": "text",
            "status": "text", "discovered_at": "timestamptz",
            "created_at": "timestamptz", "updated_at": "timestamptz",
        },
        "required": ["title"],
        "enums": {},
        "fat_columns": ["description", "impact"],
        "safe_select": "id,title,type,priority,domain,status,created_at,updated_at",
        "on_conflict": "title",
        "notes": "Core backlog used by the maintenance and research loops.",
    },
    "reasoning_log": {
        "pk": "id", "pk_type": "bigserial",
        "columns": {
            "id": "bigint", "session_id": "text", "domain": "text",
            "action_planned": "text", "preflight_result": "text",
            "assumptions_caught": "integer", "queries_triggered": "integer",
            "owner_confirm_needed": "boolean", "behavioral_rule_proposed": "boolean",
            "outcome": "text", "reasoning": "text", "created_at": "timestamptz",
        },
        "required": ["action_planned"],
        "enums": {},
        "fat_columns": ["preflight_result", "outcome", "reasoning"],
        "safe_select": "id,session_id,domain,action_planned,created_at",
        "notes": "Cognitive pre-flight log written by agentic reasoning tools.",
    },
    "projects": {
        "pk": "project_id", "pk_type": "text",
        "columns": {
            "project_id": "text", "name": "text", "folder_path": "text",
            "index_path": "text", "status": "text", "last_indexed": "timestamptz",
            "created_at": "timestamptz", "updated_at": "timestamptz",
        },
        "required": ["project_id", "name"],
        "enums": {"status": ["active", "degraded", "tombstone"]},
        "fat_columns": ["folder_path", "index_path"],
        "safe_select": "project_id,name,status,last_indexed,folder_path",
        "on_conflict": "project_id",
        "notes": "Project registry used by Telegram project tools and desktop context prep.",
    },
    "changelog": {
        "pk": "id", "pk_type": "bigserial",
        "columns": {
            "id": "bigint", "action": "text", "detail": "text", "domain": "text",
            "version": "text", "change_type": "text", "component": "text", "title": "text",
            "description": "text", "triggered_by": "text", "growth_flag_type": "text",
            "before_state": "text", "after_state": "text", "files_changed": "text[]",
            "session_id": "bigint", "created_at": "timestamptz",
        },
        "required": ["action"],
        "enums": {},
        "fat_columns": ["detail", "description", "before_state", "after_state", "files_changed"],
        "safe_select": "id,action,domain,version,change_type,component,created_at",
        "notes": "Changelog ledger written after deploys and major configuration changes.",
    },
})

def t_sb_query(table, filters="", limit=20, order="", select="*"):
    """Schema-aware Supabase read. Auto-blocks tombstone tables, auto-downgrades
    select=* on fat-column tables, validates columns, warns on unsafe patterns.
    Use dedicated tools first (search_kb, get_mistakes etc) -- this is the escape hatch.
    filters: PostgREST filter string e.g. 'status=eq.pending'
    order: sort column e.g. 'created_at.desc'
    select: columns e.g. 'id,status,priority' (avoid * on large tables)"""
    try: lim = int(limit) if limit else 20
    except: lim = 20
    # Block unknown tables upfront — no point hitting Supabase if table doesn't exist
    if table not in _SB_SCHEMA.get("_tombstone", set()) and table not in _SB_SCHEMA.get("tables", {}):
        known = sorted(_SB_SCHEMA.get("tables", {}).keys())
        return {"ok": False, "error": f"UNKNOWN_TABLE: '{table}' not in schema registry.",
                "hint": f"Known tables: {known}",
                "tip": "Check spelling — common mistake: 'tasks' should be 'task_queue'"}
    sel, warnings = _validate_read(table, select or "*", filters or "")
    # Hard block on tombstone
    if sel is None:
        return {"ok": False, "error": warnings[0] if warnings else "tombstone table",
                "schema_warnings": warnings}
    qs = f"select={sel}"
    if filters and filters.strip(): qs += f"&{filters.strip()}"
    if order and order.strip():     qs += f"&order={order.strip()}"
    qs += f"&limit={lim}"
    try:
        result = sb_get(table, qs, svc=True)
    except Exception as e:
        err_str = str(e)
        # Parse Supabase error for actionable hint
        hint = ""
        if "400" in err_str:
            hint = "Bad request — check filter syntax and column names"
        elif "404" in err_str:
            hint = f"Table '{table}' not found in Supabase — check table name"
        elif "column" in err_str.lower():
            hint = "Unknown column in select or filters — check column names against schema"
        return {"ok": False, "error": f"Supabase query failed: {err_str[:200]}",
                "table": table, "query": qs, "hint": hint}
    if warnings:
        if isinstance(result, list):
            return {"ok": True, "data": result, "count": len(result), "schema_warnings": warnings,
                    "table": table, "effective_select": sel}
        if isinstance(result, dict):
            result["schema_warnings"] = warnings
    if isinstance(result, list):
        return {"ok": True, "data": result, "count": len(result), "table": table}
    return result

def t_sb_insert(table="", data=""):
    """Schema-validated Supabase insert. Blocks tombstone tables, validates columns/enums/required fields.
    data: JSON string or dict. Returns ok + schema_warnings if any validation issues found."""
    if not table:
        return {"ok": False, "error": "table required"}
    if table in _SB_SCHEMA.get("_tombstone", set()):
        return {"ok": False, "error": f"TOMBSTONE: '{table}' is retired -- do not write to it",
                "hint": "Check active tables in _SB_SCHEMA"}
    if table not in _SB_SCHEMA.get("tables", {}):
        known = sorted(_SB_SCHEMA.get("tables", {}).keys())
        return {"ok": False, "error": f"UNKNOWN_TABLE: '{table}' not in schema registry.",
                "hint": f"Known tables: {known}",
                "tip": "Common mistake: 'tasks' should be 'task_queue'"}
    if isinstance(data, str):
        try: data = json.loads(data)
        except Exception as e:
            schema = _sb_schema(table)
            return {"ok": False, "error_code": "invalid_json", "message": str(e),
                    "hint": f"Expected fields for {table}: {sorted(schema.get('columns', {}).keys()) if schema else 'unknown table'}"}
    errs = _validate_write(table, data)
    if errs:
        schema = _sb_schema(table)
        return {"ok": False, "error": "schema_violation", "violations": errs,
                "required": schema.get("required", []),
                "hint": f"Valid columns for {table}: {sorted(schema.get('columns', {}).keys())}"}
    try:
        ok = sb_post(table, data)
        if not ok:
            return {"ok": False, "error_code": "insert_failed",
                    "message": f"Supabase insert rejected for '{table}'", "retry_hint": True}
        return {"ok": True, "table": table, "inserted_fields": sorted(data.keys())}
    except Exception as e:
        return {"ok": False, "error_code": "exception", "message": str(e), "retry_hint": True}

def t_sb_bulk_insert(table: str, rows: str) -> dict:
    """Insert multiple rows into Supabase in a single HTTP call."""
    try:
        if isinstance(rows, str):
            rows = json.loads(rows)
        if not isinstance(rows, list):
            return {"ok": False, "error": "rows must be a JSON array"}
        if len(rows) == 0:
            return {"ok": False, "error": "rows array is empty"}
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={**_sbh(True), "Prefer": "return=minimal"},
            json=rows,
            timeout=30
        )
        ok = r.is_success
        if not ok:
            print(f"[SB BULK] {table} failed: {r.status_code} {r.text[:200]}")
        return {"ok": ok, "table": table, "rows_attempted": len(rows),
                "status_code": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_debug_fn(fn_name: str, dry_run: str = "true", extra_args: str = None) -> dict:
    """Battle-tested debug harness for any CORE function.

    DOCTRINE: Every CORE function must be debuggable without touching production data.
    This tool runs a named function through a staged execution pipeline:
      Stage 0 - resolve:  confirm function exists and is callable
      Stage 1 - preflight: run all data fetches (Supabase, KB, etc) and return raw inputs
      Stage 2 - llm:      call Groq/LLM if applicable, return raw response before parsing
      Stage 3 - parse:    attempt JSON/text parse, return parsed result + any parse error
      Stage 4 - write:    attempt DB write (SKIPPED if dry_run=True)
    Each stage is labelled in the response. First failed stage tells you exactly where it broke.

    Args:
        fn_name:    name of function to debug (e.g. '_run_simulation_batch', '_extract_real_signal')
        dry_run:    if True (default), skip all DB writes -- safe for production debugging
        extra_args: optional dict of extra args to pass to the function (for t_* tools)

    Returns dict with:
        fn:         function name
        dry_run:    whether writes were skipped
        stages:     list of {stage, status, data, error, trace} -- one per stage executed
        final:      'ok' | 'failed_at_stage_N' | 'not_found'
        result:     final function return value if ran successfully
    """
    import traceback, importlib

    # MCP passes all args as strings -- coerce explicitly
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() not in ("false", "0", "no")
    if isinstance(extra_args, str):
        try:
            import json as _j; extra_args = _j.loads(extra_args)
        except Exception:
            extra_args = {}
    if not isinstance(extra_args, dict):
        extra_args = {}

    stages = []
    result = None

    def stage(name, fn, *args, **kwargs):
        """Run one stage, append to stages list, return (ok, value)."""
        try:
            val = fn(*args, **kwargs)
            stages.append({"stage": name, "status": "ok", "data": str(val)[:500]})
            return True, val
        except Exception as e:
            stages.append({"stage": name, "status": "error",
                           "error": str(e), "trace": traceback.format_exc()[:1000]})
            return False, None

    def _coerce_call_kwargs(value, default=None):
        if isinstance(value, dict) and value:
            return value
        if default is not None:
            return default
        return {}

    # Stage 0 -- resolve function
    # Use reload() to pick up functions added in the current deploy/session.
    # Cached imports miss functions registered after Railway started.
    target_fn = None
    found_in = None
    for mod_name in ["core_tools", "core_train", "core_config", "core_github", "core_main"]:
        try:
            mod = importlib.import_module(mod_name)
            try:
                importlib.reload(mod)  # force refresh -- catches new functions in same deploy
            except Exception:
                pass  # reload is best-effort; proceed with cached version if it fails
            if hasattr(mod, fn_name):
                target_fn = getattr(mod, fn_name)
                found_in = mod_name
                stages.append({"stage": "0_resolve", "status": "ok",
                               "data": f"found in {mod_name}"})
                break
        except Exception as e:
            stages.append({"stage": "0_resolve", "status": "error",
                           "error": f"{mod_name}: {e}"})

    if target_fn is None:
        return {"fn": fn_name, "dry_run": dry_run, "stages": stages,
                "final": "not_found", "result": None}

    # Stage 1..N -- if it's a known pipeline function, run staged breakdown
    # Otherwise fall back to direct call with dry_run awareness
    known_staged = {
        "_run_simulation_batch": "simulation",
        "_extract_real_signal":  "real_signal",
        "run_cold_processor":    "cold_processor",
        "gemini_chat":           "gemini",
        "groq_chat":             "groq",
    }

    if fn_name in known_staged:
        from core_config import sb_get, sb_post, GROQ_MODEL
        import json as _json

        # -- Gemini staged test --
        if known_staged[fn_name] == "gemini":
            def gemini_preflight():
                from core_config import _GEMINI_KEYS, _GEMINI_MODEL
                return {"key_count": len(_GEMINI_KEYS), "model": _GEMINI_MODEL,
                        "exhausted_keys": [i for i, k in enumerate(_GEMINI_KEYS) if not k]}
            ok, _ = stage("1_preflight", gemini_preflight)
            if not ok:
                return {"fn": fn_name, "dry_run": dry_run, "stages": stages, "final": "failed_at_stage_1", "result": None}
            if not dry_run:
                call_kwargs = _coerce_call_kwargs(extra_args, {"system": "test", "user": "reply OK", "max_tokens": 10})
                ok2, result = stage("2_execute", target_fn, **call_kwargs)
            else:
                stages.append({"stage": "2_execute", "status": "dry_run_skipped", "data": "pass dry_run=false to call Gemini API"})
                ok2, result = True, None
            return {"fn": fn_name, "dry_run": dry_run, "stages": stages, "final": "ok" if ok2 else "failed_at_stage_2", "result": result}

        # -- Groq staged test --
        if known_staged[fn_name] == "groq":
            def groq_preflight():
                from core_config import GROQ_API_KEY, GROQ_MODEL
                return {"model": GROQ_MODEL, "key_set": bool(GROQ_API_KEY)}
            ok, _ = stage("1_preflight", groq_preflight)
            if not ok:
                return {"fn": fn_name, "dry_run": dry_run, "stages": stages, "final": "failed_at_stage_1", "result": None}
            if not dry_run:
                call_kwargs = _coerce_call_kwargs(extra_args, {"system": "test", "user": "reply OK", "max_tokens": 10})
                ok2, result = stage("2_execute", target_fn, **call_kwargs)
            else:
                stages.append({"stage": "2_execute", "status": "dry_run_skipped", "data": "pass dry_run=false to call Groq API"})
                ok2, result = True, None
            return {"fn": fn_name, "dry_run": dry_run, "stages": stages, "final": "ok" if ok2 else "failed_at_stage_2", "result": result}

        # Stage 1 -- preflight data fetch (for pipeline functions)
        def do_preflight():
            mistakes = sb_get("mistakes", "select=domain,what_failed&order=id.desc&limit=5", svc=True)
            hots_count = len(sb_get("hot_reflections", "select=id&processed_by_cold=eq.0", svc=True) or [])
            return {"mistakes_count": len(mistakes), "unprocessed_hots": hots_count}
        ok, preflight = stage("1_preflight", do_preflight)
        if not ok:
            return {"fn": fn_name, "dry_run": dry_run, "stages": stages,
                    "final": "failed_at_stage_1", "result": None}

        # Stage 2 -- call function with write intercepted
        orig_sb_post = None
        if dry_run:
            import core_config as _cc
            orig_sb_post = _cc.sb_post
            _cc.sb_post = lambda t, d: stages.append({"stage": "4_write_intercepted",  # noqa
                "status": "dry_run_skipped", "data": f"table={t} keys={list(d.keys())}"}) or True

        try:
            ok2, result = stage("2_execute", target_fn)
        finally:
            if dry_run and orig_sb_post:
                import core_config as _cc
                _cc.sb_post = orig_sb_post

        final = "ok" if ok2 else "failed_at_stage_2"
    else:
        # A.1: Guard against non-dict extra_args before ** unpacking
        # extra_args could be non-dict if JSON parse failed (edge case after MCP string coercion)
        call_kwargs = extra_args if isinstance(extra_args, dict) else {}
        if call_kwargs:
            ok, result = stage("1_direct_call", target_fn, **call_kwargs)
        else:
            ok, result = stage("1_direct_call", target_fn)
        final = "ok" if ok else "failed_at_stage_1"

    return {"fn": fn_name, "dry_run": dry_run, "stages": stages, "final": final, "result": result}


def t_get_training_pipeline() -> dict:
    """Full training pipeline status: hot reflections, cold processor, patterns, quality trend, health flags.
    Used by session_start training_pipeline block and /tstatus Telegram command."""
    try:
        now = datetime.utcnow()
        health_flags = []

        # --- Hot reflections ---
        all_hots = sb_get("hot_reflections",
            "select=id,created_at,source,domain,quality_score,processed_by_cold"
            "&order=created_at.desc&limit=5", svc=True) or []
        unprocessed = [h for h in all_hots if not h.get("processed_by_cold")]
        last_real = next((h for h in all_hots if h.get("source") == "real"), None)
        last_sim  = next((h for h in all_hots if h.get("source") == "simulation"), None)

        # Total counts
        total_hots_rows = sb_get("hot_reflections", "select=id&order=id.desc&limit=1", svc=True) or []
        total_hots = total_hots_rows[0]["id"] if total_hots_rows else 0
        total_sim  = len(sb_get("hot_reflections", "select=id&source=eq.simulation", svc=True) or [])

        # Simulation health flag
        if total_sim == 0:
            health_flags.append("simulation_dead")

        # --- Cold processor ---
        cold_rows = sb_get("cold_reflections",
            f"select={_sel_force('cold_reflections', ['id','created_at','hot_count','patterns_found','evolutions_queued','auto_applied','summary_text'])}"
            "&order=created_at.desc&limit=5", svc=True) or []
        last_cold = cold_rows[0] if cold_rows else None
        total_cold_runs = len(cold_rows)  # approximate from recent 5

        cold_mins_ago = None
        if last_cold and last_cold.get("created_at"):
            try:
                ts = datetime.fromisoformat(last_cold["created_at"].replace("Z","").split("+")[0])
                cold_mins_ago = int((now - ts).total_seconds() / 60)
            except Exception:
                pass

        # Cold stale flag: hasn't run in 3+ hours and there are unprocessed hots
        if cold_mins_ago is not None and cold_mins_ago > 180 and len(unprocessed) > 0:
            health_flags.append(f"cold_stale_{cold_mins_ago}min")

        # Unprocessed backlog flag
        unprocessed_count = len(sb_get("hot_reflections",
            "select=id&processed_by_cold=eq.0", svc=True) or [])
        if unprocessed_count >= COLD_HOT_THRESHOLD:
            health_flags.append(f"unprocessed_backlog_{unprocessed_count}_threshold_{COLD_HOT_THRESHOLD}")

        # Zero patterns in last 5 cold runs
        if cold_rows and all(r.get("patterns_found", 0) == 0 for r in cold_rows[:5]):
            health_flags.append("zero_patterns_last_5_runs")

        # --- Patterns ---
        active_patterns = sb_get("pattern_frequency",
            "select=pattern_key,domain,frequency,auto_applied,last_seen"
            "&stale=eq.false&frequency=gte.2&order=frequency.desc&limit=1", svc=True) or []
        all_active = sb_get("pattern_frequency", "select=id&stale=eq.false", svc=True) or []
        stale_count = len(sb_get("pattern_frequency", "select=id&stale=eq.true", svc=True) or [])
        top_pattern = active_patterns[0] if active_patterns else None

        # --- Evolution queue ---
        pending_evo = sb_get("evolution_queue",
            "select=id&status=eq.pending", svc=True) or []
        applied_evo = sb_get("evolution_queue",
            "select=id&status=eq.applied&order=id.desc&limit=1", svc=True) or []

        # --- Quality trend (last 7d) ---
        cutoff = (now - timedelta(days=7)).isoformat()
        quality_rows = sb_get("hot_reflections",
            f"select=quality_score,created_at&source=eq.real&quality_score=not.is.null"
            f"&quality_score=lte.1.0&created_at=gte.{cutoff}&order=created_at.asc", svc=True) or []
        quality_avg = round(sum(r["quality_score"] for r in quality_rows) / len(quality_rows), 3) if quality_rows else None
        # Trend: compare first half vs second half
        quality_trend = "no_data"
        if len(quality_rows) >= 4:
            mid = len(quality_rows) // 2
            first = sum(r["quality_score"] for r in quality_rows[:mid]) / mid
            second = sum(r["quality_score"] for r in quality_rows[mid:]) / len(quality_rows[mid:])
            quality_trend = "improving" if second - first > 0.03 else ("declining" if first - second > 0.03 else "stable")
            if quality_trend == "declining":
                health_flags.append("quality_declining")

        return {
            "hot": {
                "total": total_hots,
                "unprocessed": unprocessed_count,
                "total_simulation": total_sim,
                "simulation_ok": total_sim > 0,
                "last_real": {
                    "id": last_real["id"], "ts": last_real["created_at"][:16],
                    "domain": last_real["domain"], "quality": last_real["quality_score"]
                } if last_real else None,
                "last_simulation": {
                    "id": last_sim["id"], "ts": last_sim["created_at"][:16],
                    "domain": last_sim["domain"], "quality": last_sim["quality_score"]
                } if last_sim else None,
            },
            "cold": {
                "last_run_ts": last_cold["created_at"][:16] if last_cold else None,
                "last_run_mins_ago": cold_mins_ago,
                "threshold": COLD_HOT_THRESHOLD,
                "last_hot_count": last_cold["hot_count"] if last_cold else 0,
                "last_patterns_found": last_cold["patterns_found"] if last_cold else 0,
                "last_evolutions_queued": last_cold["evolutions_queued"] if last_cold else 0,
                "last_auto_applied": last_cold["auto_applied"] if last_cold else 0,
                "recent_5_summaries": [r.get("summary_text","")[:80] for r in cold_rows[:5]],
            },
            "patterns": {
                "active_count": len(all_active),
                "stale_count": stale_count,
                "top": {
                    "key": top_pattern["pattern_key"][:80],
                    "domain": top_pattern["domain"],
                    "freq": top_pattern["frequency"],
                } if top_pattern else None,
            },
            "evolutions": {
                "pending": len(pending_evo),
                "applied": int(sb_get("evolution_queue", "select=id&status=eq.applied&order=id.desc&limit=1",
                    svc=True)[0]["id"]) if applied_evo else 0,
            },
            "quality": {
                "7d_avg": quality_avg,
                "trend": quality_trend,
                "sample_count": len(quality_rows),
            },
            "health_flags": health_flags,
            "pipeline_ok": len(health_flags) == 0,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "health_flags": ["pipeline_error"]}


def t_training_status():
    """Legacy wrapper -- use t_get_training_pipeline() for full status."""
    try:
        tp = t_get_training_pipeline()
        unprocessed_count = tp.get("hot", {}).get("unprocessed", 0)
        pending_evo_rows = sb_get("evolution_queue",
            f"select={_sel_force('evolution_queue', ['id','change_type','change_summary','confidence'])}&status=eq.pending&id=gt.1", svc=True) or []
        return {
            "status": "Training pipeline ACTIVE",
            "unprocessed_hot": unprocessed_count,
            "pending_evolutions": len(pending_evo_rows),
            "backlog_pending": -1,
            "evolutions": pending_evo_rows[:5],
            "cold_threshold": COLD_HOT_THRESHOLD,
            "kb_growth_threshold": COLD_KB_GROWTH_THRESHOLD,
            "kb_mine_ratio_threshold": KB_MINE_RATIO_THRESHOLD,
            "pattern_threshold": PATTERN_EVO_THRESHOLD,
            "auto_apply_conf": KNOWLEDGE_AUTO_CONFIDENCE,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}

def t_trigger_cold_processor():
    try:
        # H.4: size guard -- run_cold_processor() can return large patterns/evolutions arrays
        result = run_cold_processor()
        if isinstance(result, dict):
            for _k in ("patterns", "evolutions", "hot_items", "items"):
                if _k in result and isinstance(result[_k], list) and len(result[_k]) > 10:
                    result[_k] = result[_k][:10]
                    result[f"{_k}_truncated"] = True
        return result

    except Exception as e:
        return {"ok": False, "error": str(e)}
def t_backfill_patterns(batch_size: str = "20") -> dict:
    """TASK-20: Backfill pattern_frequency -> knowledge_base directly via Groq.
    batch_size: max patterns per run (default 20).
    """
    from core_train import _backfill_patterns
    try:
        inserted = _backfill_patterns(batch_size=int(batch_size))
        return {"ok": True, "inserted": inserted, "batch_size": int(batch_size)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_backfill_status() -> dict:
    """TASK-20: Backfill status - DEPRECATED. Use t_backfill_patterns directly (now synchronous)."""
    return {"ok": True, "note": "Backfill is now synchronous - use t_backfill_patterns directly", "status": "deprecated"}


def t_ingest_knowledge(topic: str, sources: str = "all", max_per_source: int = 20, since_days: int = 7) -> dict:
    """Trigger knowledge ingestion pipeline for a topic.
    Fetches from public sources (arxiv, docs, medium, reddit, hackernews, stackoverflow),
    deduplicates, scores by engagement, extracts AI concepts, writes to kb_sources/kb_articles/kb_concepts,
    injects hot_reflections for cold processor pickup so CORE evolves from internet knowledge.
    sources: comma-separated list or 'all'. max_per_source: cap per fetcher. since_days: recency filter.
    Returns: {topic, sources_used, raw_count, deduped_count, records_inserted, records_updated, concepts_found, hot_reflections_injected}
    """
    import asyncio
    try:
        from scraper.knowledge import ingest_knowledge
        from core_train import _ingest_to_hot_reflection
        from scraper.knowledge.concept_extractor import AI_CONCEPTS

        if sources == "all" or not sources:
            src_list = ["arxiv", "docs", "medium", "reddit", "hackernews", "stackoverflow"]
        else:
            src_list = [s.strip() for s in sources.split(",") if s.strip()]

        print(f"[t_ingest_knowledge] topic={topic} sources={src_list} max={max_per_source}")

        # Run async ingestion pipeline in new event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, ingest_knowledge(
                        topic=topic, sources=src_list,
                        max_per_source=max_per_source, since_days=since_days
                    ))
                    summary = future.result(timeout=300)
            else:
                summary = loop.run_until_complete(ingest_knowledge(
                    topic=topic, sources=src_list,
                    max_per_source=max_per_source, since_days=since_days
                ))
        except RuntimeError:
            summary = asyncio.run(ingest_knowledge(
                topic=topic, sources=src_list,
                max_per_source=max_per_source, since_days=since_days
            ))

        # Inject hot_reflections for cold processor
        hot_ok = False
        concepts_found = summary.get("concepts_found", 0)
        if concepts_found > 0:
            concepts = list(AI_CONCEPTS.keys())[:concepts_found]
            avg_eng = summary.get("avg_engagement", 50.0)
            source_str = ",".join(src_list)
            hot_ok = _ingest_to_hot_reflection(topic, source_str, concepts, avg_eng)

        return {"ok": True, "hot_reflections_injected": hot_ok, **summary}
    except Exception as e:
        print(f"[t_ingest_knowledge] error: {e}")
        return {"ok": False, "error": str(e)}


def t_listen() -> dict:
    """LISTEN MODE: Start background listen job directly via globals, no HTTP self-call.
    Call t_listen_result after ~2 minutes to fetch results once job is done.
    SOP: (1) call t_listen -> get job_id, (2) wait, (3) call t_listen_result -> synthesize chunks.
    """
    try:
        import sys
        import threading
        import uuid
        from datetime import datetime
        
        # Access core_main globals via sys.modules (avoids circular import)
        core_main = sys.modules.get('core_main')
        if not core_main:
            return {"ok": False, "error": "core_main not loaded"}
        
        _listen_job = core_main._listen_job
        _listen_lock = core_main._listen_lock
        _run_listen_job = core_main._run_listen_job
        
        with _listen_lock:
            if _listen_job.get("status") == "running":
                return {"ok": True, "job_id": _listen_job["id"], "status": "running", "note": "already running"}
            job_id = str(uuid.uuid4())[:8]
            core_main._listen_job = {"id": job_id, "status": "running", "chunks": [], "started_at": datetime.utcnow().isoformat(), "stop_reason": None}
        
        t = threading.Thread(target=_run_listen_job, args=(job_id,), daemon=True)
        t.start()
        print(f"[t_listen] job started: {job_id}")
        return {"ok": True, "job_id": job_id, "status": "running", "note": "Job started. Call t_listen_result in ~2 minutes to fetch results."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_listen_result() -> dict:
    """Fetch current listen job status and results directly via globals, no HTTP self-call.
    Returns status (running|done|error), stop_reason, cycles, patterns_found, evolutions_queued, chunks.
    If status=running, wait and call again. If status=done, synthesize chunks into tasks.
    """
    try:
        import sys
        
        # Access core_main globals via sys.modules (avoids circular import)
        core_main = sys.modules.get('core_main')
        if not core_main:
            return {"ok": False, "error": "core_main not loaded"}
        
        _listen_lock = core_main._listen_lock
        
        with _listen_lock:
            job = dict(core_main._listen_job)
        
        chunks = job.get("chunks", [])
        cold_runs = [c for c in chunks if isinstance(c, dict) and c.get("type") == "cold_run"]
        
        return {
            "ok": True,
            "job_id": job.get("id"),
            "status": job.get("status", "idle"),
            "stop_reason": job.get("stop_reason"),
            "cycles": len(cold_runs),
            "total_patterns_found": sum(c.get("patterns_found", 0) for c in cold_runs),
            "total_evolutions_queued": sum(c.get("evolutions_queued", 0) for c in cold_runs),
            "chunk_count": len(chunks),
            "chunks": chunks,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_list_evolutions(status="pending", limit="20") -> dict:
    """List evolution_queue entries by status.
    Returns items (capped at limit) PLUS total_count from DB — so caller always
    knows the real queue depth even when limit < total.
    status: pending|applied|rejected (default pending)
    limit: max items to return (default 20). Use 'all' to return up to 200."""
    try:
        import httpx as _hx
        from core_config import SUPABASE_URL, _sbh_count_svc
        # Resolve limit
        lim = 200 if str(limit).lower() == "all" else max(1, min(int(limit) if limit else 20, 200))
        rows = sb_get("evolution_queue",
                  f"select={_sel_force('evolution_queue', ['id','status','change_type','change_summary','confidence','pattern_key','created_at'])}&status=eq.{status}&id=gt.1&order=created_at.desc&limit={lim}",
                  svc=True)
        # Get real total count from DB — not just len(rows)
        total_count = lim  # fallback
        try:
            r = _hx.get(
                f"{SUPABASE_URL}/rest/v1/evolution_queue?select=id&status=eq.{status}&id=gt.1&limit=1",
                headers=_sbh_count_svc(), timeout=8
            )
            cr = r.headers.get("content-range", "*/0")
            total_count = int(cr.split("/")[-1]) if "/" in cr else len(rows)
        except Exception:
            total_count = len(rows)
        return {
            "ok": True,
            "status": status,
            "total_count": total_count,
            "returned": len(rows),
            "showing": f"{len(rows)} of {total_count}",
            "note": f"Use limit='all' to see all {total_count} entries" if total_count > lim else None,
            "evolutions": rows,
            "count": len(rows),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_bulk_reject_evolutions(change_type: str = "", ids: str = "", reason: str = "", include_synthesized: str = "false") -> dict:
    try:
        id_list = [int(i.strip()) for i in ids.split(",") if i.strip().isdigit()] if ids else []
        inc_syn = str(include_synthesized).lower() in ("true", "1", "yes")
        return bulk_reject_evolutions(change_type=change_type, ids=id_list or None, reason=reason, include_synthesized=inc_syn)


    except Exception as e:
        return {"ok": False, "error": str(e)}
def t_check_evolutions(limit: int = 20) -> dict:
    """Groq-powered evolution brief."""
    try:
        lim = int(limit) if limit else 20
        evolutions = sb_get("evolution_queue",
            f"select={_sel_force('evolution_queue', ['id','change_type','change_summary','confidence','source','recommendation','pattern_key','created_at'])}"
            f"&status=eq.pending&id=gt.1&order=confidence.desc&limit={lim}",
            svc=True)
        mistakes = sb_get("mistakes",
            f"select={_sel_force('mistakes', ['domain','context','what_failed','correct_approach','root_cause','how_to_avoid','severity'])}"
            "&order=id.desc&limit=10",
            svc=True)
        patterns = sb_get("pattern_frequency",
            "select=pattern_key,frequency,domain,description&stale=eq.false&order=frequency.desc&limit=10",
            svc=True)
        templates = sb_get("script_templates",
            "select=name,description,trigger_pattern&order=use_count.desc&limit=5",
            svc=True)
        tool_names = list(TOOLS.keys())

        evo_text = "\n".join([
            f"[#{e['id']} | {e['change_type']} | conf={e.get('confidence','?')} | src={e.get('source','?')}]\n"
            f"  Summary: {e.get('change_summary','')[:120]}\n"
            f"  Recommendation: {e.get('recommendation','') or 'none'}"
            for e in evolutions
        ]) or "No pending evolutions."

        mistake_text = "\n".join([
            f"[{m.get('domain','?')} | sev={m.get('severity','?')}] FAILED: {m.get('what_failed','')[:100]}\n"
            f"  ROOT: {m.get('root_cause','')[:80]} | FIX: {m.get('correct_approach','')[:100]}"
            for m in mistakes
        ]) or "No mistakes recorded."

        pattern_text = "\n".join([
            f"  [{p.get('domain','?')}] {p.get('pattern_key','')[:80]} (seen {p.get('frequency',0)}x)"
            for p in patterns
        ]) or "No patterns yet."

        template_text = "\n".join([
            f"  TEMPLATE: {t.get('name','')} - {t.get('description','')[:80]}"
            for t in templates
        ]) or "  No templates yet."

        system = """You are CORE's evolution engine. Generate a precise, actionable brief for Claude to act on NOW.

Output MUST be a JSON object:
{
  "session_title": "short title",
  "priority_actions": [
    {
      "rank": 1,
      "action_type": "code_patch | new_tool | new_template | kb_entry | reject",
      "title": "short title",
      "why": "1 sentence",
      "evolution_ids": [list of IDs],
      "ready_to_execute": true/false,
      "instruction": "exact instruction for Claude",
      "code_snippet": "Python code or null"
    }
  ],
  "new_tools_proposed": [{"name": "t_...", "purpose": "...", "trigger": "...", "code": "..."}],
  "templates_proposed": [{"name": "...", "description": "...", "trigger_pattern": "...", "code": "..."}],
  "reject_ids": [list of IDs to reject],
  "summary": "2-3 sentence summary"
}
Output ONLY valid JSON, no preamble."""

        user = (
            f"CORE MCP tools available: {', '.join(tool_names)}\n\n"
            f"PENDING EVOLUTIONS ({len(evolutions)}):\n{evo_text}\n\n"
            f"RECENT MISTAKES ({len(mistakes)}):\n{mistake_text}\n\n"
            f"TOP PATTERNS:\n{pattern_text}\n\n"
            f"EXISTING TEMPLATES:\n{template_text}\n\n"
            f"Generate the evolution brief for this session."
        )

        raw = gemini_chat(system, user, max_tokens=2000, json_mode=True)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        brief = json.loads(raw)

        # H.2: removed auto-save side effect -- check_evolutions is a READ tool.
        # Templates are returned in brief.templates_proposed for owner to review.
        # Owner calls run_template or manually approves before any template is saved.

        return {
            "ok": True,
            "brief": brief,
            "evolution_count": len(evolutions),
            "mistake_count": len(mistakes),
            "pattern_count": len(patterns),
        }

    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Gemini returned invalid JSON: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_approve_evolution(evolution_id):
    try: eid = int(evolution_id)
    except: return {"ok": False, "error": "evolution_id must be a number"}
    return apply_evolution(eid)

def t_reject_evolution(evolution_id="", reason=""):
    try: eid = int(evolution_id)
    except: return {"ok": False, "error": "evolution_id must be a number"}
    return reject_evolution(eid, reason)

def _patch_find(content: str, old_str: str):
    """Find old_str in content. Returns (found, count, matched_str, note).
    Fallback tiers in order:
      1. Exact match
      2. Line-ending normalization (\r\n -> \n)
      3. Trailing-whitespace strip per line (catches editor-added trailing spaces)
      4. Tab->spaces normalization (catches mixed indent files)
      5. Combined: trailing-whitespace + tab normalization
    Returns char-level ndiff hint on near-miss so caller knows exactly what differs.
    """
    def _rstrip_lines(s: str) -> str:
        return '\n'.join(line.rstrip() for line in s.splitlines())

    def _detab(s: str, tabsize: int = 4) -> str:
        return s.expandtabs(tabsize)

    def _find_in(c: str, o: str, orig_content: str, note: str):
        """Try to find o in c. Returns the ORIGINAL (unnormalized) text block that
        corresponds to the match, so the replacement operates on actual file bytes.
        Uses line-count anchoring: find match position in normalized string, count
        newlines before it to get start line, then extract that many lines from original.
        """
        cnt = c.count(o)
        if cnt <= 0:
            return None
        pos = c.find(o)
        # Count how many newlines precede the match in the normalized string
        start_line_idx = c[:pos].count('\n')
        match_line_count = o.count('\n') + 1
        # Extract the same line range from the original content
        orig_lines = orig_content.splitlines(keepends=True)
        end_line_idx = start_line_idx + match_line_count
        if end_line_idx > len(orig_lines):
            # Fallback: position-based slice (best effort)
            actual = orig_content[pos:pos + len(o)]
        else:
            actual = ''.join(orig_lines[start_line_idx:end_line_idx])
            # Strip trailing newline if original o didn't end with newline
            if not o.endswith('\n') and actual.endswith('\n'):
                actual = actual[:-1]
        return True, cnt, actual, note

    # Tier 1: exact
    count = content.count(old_str)
    if count > 0:
        return True, count, old_str, None

    # Tier 2: line-ending normalization
    nc = content.replace('\r\n', '\n').replace('\r', '\n')
    no = old_str.replace('\r\n', '\n').replace('\r', '\n')
    r = _find_in(nc, no, content, "line_ending_normalized")
    if r: return r

    # Tier 3: trailing whitespace stripped per line
    nc3 = _rstrip_lines(nc)
    no3 = _rstrip_lines(no)
    r = _find_in(nc3, no3, content, "trailing_whitespace_stripped")
    if r: return r

    # Tier 4: tab -> spaces (tabsize=4)
    nc4 = _detab(nc)
    no4 = _detab(no)
    r = _find_in(nc4, no4, content, "tabs_expanded")
    if r: return r

    # Tier 5: combined trailing-whitespace + tab normalization
    nc5 = _rstrip_lines(_detab(nc))
    no5 = _rstrip_lines(_detab(no))
    r = _find_in(nc5, no5, content, "tabs_and_trailing_normalized")
    if r: return r

    # Tier 6: leading-whitespace normalization (catches indentation drift --
    # most common near-miss cause: old_str copied from display has different indent)
    def _strip_leading(s: str) -> str:
        lines = s.splitlines()
        if not lines: return s
        # Find minimum non-empty indent
        indents = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
        min_indent = min(indents) if indents else 0
        return '\n'.join(l[min_indent:] if len(l) >= min_indent else l for l in lines)
    nc6 = _strip_leading(nc)
    no6 = _strip_leading(no)
    r = _find_in(nc6, no6, content, "leading_whitespace_normalized")
    if r: return r

    # Tier 7: full normalization (leading + trailing + tabs + line endings)
    nc7 = _strip_leading(_rstrip_lines(_detab(nc)))
    no7 = _strip_leading(_rstrip_lines(_detab(no)))
    r = _find_in(nc7, no7, content, "full_whitespace_normalized")
    if r: return r

    # Tier 8: fuzzy first-line anchor -- find closest matching line using char overlap
    # Returns the actual file lines around the best match so CORE doesn't need gh_read_lines
    first_line = no.strip().splitlines()[0].strip() if no.strip() else ""
    best_score = 0.0
    best_line_idx = -1
    all_lines = nc.splitlines()
    if first_line and len(first_line) > 8:
        fl_chars = set(first_line.lower())
        for idx, line in enumerate(all_lines):
            line_chars = set(line.strip().lower())
            if not line_chars: continue
            overlap = len(fl_chars & line_chars) / max(len(fl_chars | line_chars), 1)
            # Bonus for matching start of line
            if line.strip()[:20] == first_line[:20]:
                overlap += 0.3
            if overlap > best_score:
                best_score = overlap
                best_line_idx = idx

    hint = None
    auto_context = None
    if best_line_idx >= 0 and best_score > 0.5:
        # Extract surrounding context (5 lines before + 5 after)
        ctx_start = max(0, best_line_idx - 2)
        ctx_end   = min(len(all_lines), best_line_idx + 8)
        ctx_lines = all_lines[ctx_start:ctx_end]
        auto_context = {
            "file_line_number": ctx_start + 1,  # 1-indexed
            "actual_content": "\n".join(ctx_lines),
            "match_score": round(best_score, 2),
            "message": (
                f"Best match found near line {best_line_idx + 1} (score={best_score:.2f}). "
                f"Use this actual_content to build correct old_str -- no gh_read_lines needed."
            )
        }
        # Also build char-level diff hint between first lines
        diff = list(difflib.ndiff([first_line], [all_lines[best_line_idx].strip()]))
        near_diff = "".join(diff)[:200]
        hint = (
            f"near_miss (score={best_score:.2f}): {near_diff} "
            f"-- actual file content returned in auto_context, use it to fix old_str"
        )
    else:
        hint = (
            f"not_found -- no close match for '{first_line[:60]}'. "
            f"Use search_in_file to locate the correct string first."
        )
    # Pack auto_context into hint tuple as 5th element (backwards-compatible: callers only use 4)
    return False, 0, old_str, hint, auto_context


def t_gh_search_replace(path="", old_str="", new_str=None, message="", repo="", dry_run="false", allow_deletion="false"):
    """Surgical find-replace using Blobs API (atomic commit, no SHA conflict, no size limit).
    allow_deletion: must be 'true' to permit empty new_str. Default false -- blocks accidental deletion."""
    try:
        preflight = _require_external_service_preflight("github", "gh_search_replace")
        if preflight:
            return preflight
        repo = repo or GITHUB_REPO
        # DELETION GUARD: block empty/missing new_str unless allow_deletion=true
        _allow_del = str(allow_deletion).lower() == "true"
        if (new_str is None or new_str == "") and not _allow_del:
            return {"ok": False, "error": "DELETION BLOCKED: new_str is missing or empty. Pass allow_deletion=true if this deletion is intentional."}
        if new_str is None:
            new_str = ""
        file_content = _gh_blob_read(path, repo)
        pf_result = _patch_find(file_content, old_str)
        found, count, matched, hint = pf_result[0], pf_result[1], pf_result[2], pf_result[3]
        auto_context = pf_result[4] if len(pf_result) > 4 else None

        if not found:
            # Build all-occurrence search to help locate the string if it exists differently
            response = {
                "ok": False,
                "error": f"old_str not found in {path}",
                "hint": hint or "check whitespace/indentation",
            }
            if auto_context:
                response["auto_context"] = auto_context
            return response

        if count > 1:
            # Ambiguity: show ALL locations with surrounding context so CORE can disambiguate
            all_lines = file_content.splitlines()
            locations = []
            search_in = file_content
            pos = 0
            occurrence = 0
            while True:
                idx = search_in.find(old_str, pos)
                if idx == -1: break
                occurrence += 1
                line_num = file_content[:idx].count('\n') + 1
                ctx_s = max(0, line_num - 3)
                ctx_e = min(len(all_lines), line_num + old_str.count('\n') + 3)
                locations.append({
                    "occurrence": occurrence,
                    "line": line_num,
                    "context": "\n".join(
                        f"{ctx_s+i+1:4d}  {l}"
                        for i, l in enumerate(all_lines[ctx_s:ctx_e])
                    )
                })
                pos = idx + 1
                if occurrence >= 5: break  # cap at 5
            return {
                "ok": False,
                "error": f"AMBIGUOUS: old_str found {count}x in {path} -- add more surrounding lines to old_str to make it unique",
                "occurrence_count": count,
                "locations": locations,
                "fix_hint": "Extend old_str to include 2-3 unique surrounding lines that only appear near one of these locations."
            }

        new_content = file_content.replace(matched, new_str, 1)

        # Syntax check for .py files before committing
        if path.endswith(".py"):
            import py_compile, tempfile as _tmpf
            with _tmpf.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tf:
                tf.write(new_content); tmp = tf.name
            try:
                py_compile.compile(tmp, doraise=True)
            except py_compile.PyCompileError as ce:
                import os; os.unlink(tmp)
                return {"ok": False, "error": f"SYNTAX ERROR -- not pushed: {ce}",
                        "hint": "Fix the syntax error in new_str before retrying."}
            finally:
                import os
                if os.path.exists(tmp): os.unlink(tmp)

        # Build compact diff (always returned, even on live write)
        diff_lines = list(difflib.unified_diff(
            file_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"{path} (before)", tofile=f"{path} (after)", n=2
        ))
        compact_diff = "".join(diff_lines)[:2000]

        if str(dry_run).lower() == "true":
            return {"ok": True, "dry_run": True, "path": path,
                    "would_replace": old_str[:80], "diff": compact_diff,
                    "match_note": hint or "exact_match"}

        commit_sha = _gh_blob_write(path, new_content, message, repo)
        return {
            "ok": True, "dry_run": False, "path": path,
            "replaced": old_str[:80], "commit": commit_sha[:12] if commit_sha else None,
            "match_note": hint or "exact_match",
            "diff": compact_diff,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_gh_read_lines(path, start_line=1, end_line=50, repo=""):
    try:
        file_content = _gh_blob_read(path, repo or GITHUB_REPO)
        lines = file_content.splitlines()
        total = len(lines)
        s = max(1, int(start_line)) - 1
        e = min(total, int(end_line))
        selected = lines[s:e]
        numbered = "\n".join(f"{s+i+1:4d}  {line}" for i, line in enumerate(selected))
        # raw_lines: exact file bytes for each line, joined by \n -- use this to build old_str for patches.
        # Never construct old_str from the numbered display (content field) -- line number prefix will corrupt it.
        raw = "\n".join(selected)
        return {"ok": True, "path": path, "total_lines": total,
                "showing": f"{s+1}-{s+len(selected)}", "content": numbered, "raw": raw}
    except Exception as ex:
        return {"ok": False, "error": str(ex), "path": path}


# -- Agentic speed tools ------------------------------------------------------

def t_core_py_fn(fn_name: str, file: str = "core_tools.py") -> dict:
    """Read a single function from a CORE source file by name. Defaults to core_tools.py."""
    try:
        target = file if file else "core_tools.py"
        content = _gh_blob_read(target)
        lines = content.splitlines()
        start = None
        indent = None
        for i, line in enumerate(lines):
            if line.strip().startswith(f"def {fn_name}(") or line.strip() == f"def {fn_name}()":
                start = i
                indent = len(line) - len(line.lstrip())
                break
        if start is None:
            return {"ok": False, "error": f"Function '{fn_name}' not found in {target}"}
        end = start + 1
        while end < len(lines):
            line = lines[end]
            if line.strip() == "":
                end += 1
                continue
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent <= indent and line.strip().startswith("def "):
                break
            end += 1
        source = "\n".join(lines[start:end])
        return {"ok": True, "fn_name": fn_name, "start_line": start + 1,
                "end_line": end, "line_count": end - start, "source": source}
    except Exception as e:
        return {"ok": False, "error": str(e)}



# -- system_map scan ----------------------------------------------------------

def t_system_map_scan(trigger: str = "manual") -> dict:
    """Scan system_map table - snapshot at session_start, drift-fix at session_end.
    session_start: read-only, returns full wiring for Claude context.
    session_end: read-write, updates volatile key_facts (tool_count etc) if changed.
    manual: same as session_start (read-only snapshot).
    """
    try:
        rows = sb_get(
            "system_map",
            "select=id,layer,component,item_type,name,role,responsibility,key_facts,is_volatile,status,notes"
            "&order=layer,component,name&limit=2000",
            svc=True
        )
        if not isinstance(rows, list):
            return {"ok": False, "error": "system_map query failed", "rows": []}

        updates = []
        inserted_tools = []
        tombstoned_tools = []
        if trigger == "session_end":
            # B.2: delegate entirely to t_sync_system_map -- no duplicate reconciler logic here
            # t_sync_system_map runs all 6 layer reconcilers + volatile key_facts updates
            sync_result = t_sync_system_map(trigger="session_end", notify_on_changes="false")
            inserted_tools  = sync_result.get("drift", {}).get("inserted", [])
            tombstoned_tools = sync_result.get("drift", {}).get("tombstoned", [])
            updates = sync_result.get("drift", {}).get("kf_updated", [])

        wiring = {}
        for row in rows:
            if row.get("status") == "tombstone":
                continue  # exclude tombstoned components from live wiring snapshot
            layer = row["layer"]
            if layer not in wiring:
                wiring[layer] = []
            wiring[layer].append({
                "component": row["component"],
                "type": row["item_type"],
                "name": row["name"],
                "role": row["role"],
                "responsibility": row["responsibility"],
            })

        active_rows = [r for r in rows if r.get("status") != "tombstone"]
        return {
            "ok": True,
            "trigger": trigger,
            "total_components": len(active_rows),
            "updates_applied": len(updates),
            "updates": updates,
            "inserted_tools": inserted_tools,
            "tombstoned_tools": tombstoned_tools,
            "wiring": wiring,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _get_stale_pattern_count() -> int:
    """Count patterns with stale=true. Used in session_start to surface dead patterns."""
    try:
        rows = sb_get("pattern_frequency", "select=id&stale=eq.true", svc=True) or []
        return len(rows)
    except Exception:
        return 0


def _get_task_domain(task_json: str = "") -> str:
    """Detect mistake domain from resume_task JSON keywords.
    Maps task description + subtask content to the most relevant mistakes domain.
    Used by t_session_start to inject domain-scoped mistakes at boot."""
    try:
        t = task_json.lower()
        if any(k in t for k in ["patch_file", "core_tools", "core_train", "gh_search_replace", "multi_patch", "old_str"]):
            return "core_agi.patching"
        if any(k in t for k in ["cold_processor", "hot_reflection", "cold_reflection", "training", "evolution"]):
            return "core_train"
        if any(k in t for k in ["railway", "deploy", "build_status", "redeploy", "crash"]):
            return "core_agi.deploy"
        if any(k in t for k in ["session_end", "session_start", "skill_file", "session_close"]):
            return "core_agi.session"
        if any(k in t for k in ["zapier", "gmail", "calendar", "google"]):
            return "zapier"
        if any(k in t for k in ["project", "jk1-2", "lsei", "equinix", "rmu", "mltx"]):
            return "project"
        return "core_agi"
    except Exception:
        return "core_agi"


def t_session_start() -> dict:
    """One-call session bootstrap - includes system_map snapshot.
    Returns in_progress_tasks separately from pending_tasks so Claude immediately
    knows if a task was left partially done last session.
    domain_mistakes: top 5 scoped to resume_task domain (backfilled with global if <3 results).
    top_patterns: top 3 patterns by frequency from pattern_frequency.
    quality_alert: non-null if trend=declining or 7d_avg<0.75.
    Use get_mistakes(domain=X) for deeper domain-specific lookup."""
    try:
        state = t_state()
        health = t_health()
        # --- 25.B: Domain-scoped mistake injection ---
        pending_tasks_all = state.get("pending_tasks", [])
        resume_task_obj = next((t for t in pending_tasks_all if t.get("status") == "in_progress"), None)
        resume_task_json = resume_task_obj.get("task", "") if resume_task_obj else ""
        detected_domain = _get_task_domain(resume_task_json)
        try:
            domain_mistakes_raw = sb_get("mistakes",
                f"select={_sel_force('mistakes', ['domain','context','what_failed','correct_approach','severity','root_cause','how_to_avoid'])}&id=gt.1&domain=like.{detected_domain}%&order=severity.desc,created_at.desc&limit=5",
                svc=True) or []
        except Exception:
            domain_mistakes_raw = []
        # Backfill: if domain-scoped returns <3, supplement with global recent (deduplicated)
        if len(domain_mistakes_raw) < 3:
            try:
                global_mistakes = sb_get("mistakes",
                    f"select={_sel_force('mistakes', ['domain','context','what_failed','correct_approach','severity','root_cause','how_to_avoid'])}&id=gt.1&order=created_at.desc&limit=10",
                    svc=True) or []
                seen = {m.get("context", "")[:80] for m in domain_mistakes_raw}
                for m in global_mistakes:
                    if m.get("context", "")[:80] not in seen and len(domain_mistakes_raw) < 5:
                        domain_mistakes_raw.append(m)
                        seen.add(m.get("context", "")[:80])
            except Exception:
                pass
        # --- 25.C: Top pattern injection ---
        try:
            top_pattern_rows = sb_get("pattern_frequency",
                "select=pattern_key,domain,frequency&id=gt.1&order=frequency.desc&limit=3",
                svc=True) or []
            top_patterns = [{"pattern": r.get("pattern_key", "")[:120], "domain": r.get("domain", ""), "freq": r.get("frequency", 0)} for r in top_pattern_rows]
        except Exception:
            top_patterns = []
        try:
            evolutions = sb_get("evolution_queue",
                f"select={_sel_force('evolution_queue', ['id','change_summary','change_type','confidence'])}&status=eq.pending&order=confidence.desc&limit=5")
            if not isinstance(evolutions, list):
                evolutions = []
        except Exception:
            evolutions = []
        training = t_get_training_pipeline()
        # --- 25.D: Quality alert surface ---
        quality_alert = None
        try:
            q = training.get("quality", {})
            if q.get("trend") == "declining" or (q.get("7d_avg") or 1.0) < 0.75:
                quality_alert = {"trend": q.get("trend"), "7d_avg": q.get("7d_avg"), "note": "Review recent sessions -- session quality declining"}
        except Exception:
            pass
        try:
            smap = t_system_map_scan(trigger="session_start")
            # Auto-reconcile immediately if tool drift detected -- don't wait 6h
            try:
                wiring_check = smap.get("wiring", {}) if smap.get("ok") else {}
                registered_tool_count = sum(
                    1 for r in wiring_check.get("executor", [])
                    if r.get("type") == "tool"
                )
                if abs(len(TOOLS) - registered_tool_count) > 0:
                    print(f"[SMAP] Drift at session_start: live={len(TOOLS)} registered={registered_tool_count} -- auto-syncing")
                    t_sync_system_map(trigger="session_start", notify_on_changes="false")
                    smap = t_system_map_scan(trigger="session_start")  # re-read post-fix
            except Exception as _dr:
                print(f"[SMAP] session_start drift check error: {_dr}")
        except Exception as e:
            smap = {"ok": False, "error": f"system_map scan failed: {e}"}
        # --- 16.E: Drift summary per layer ---
        drift = {"tools": 0, "brain_tables": 0, "executor_files": 0, "skeleton_docs": 0}
        try:
            wiring = smap.get("wiring", {})
            # Tools: live TOOLS dict vs registered active tool entries
            registered_tools = sum(
                1 for r in wiring.get("executor", [])
                if r.get("type") == "tool" and r.get("name") in TOOLS
            )
            drift["tools"] = max(0, len(TOOLS) - registered_tools)
            # Brain tables: count active brain table entries
            registered_brain = sum(
                1 for r in wiring.get("brain", [])
                if r.get("type") == "table"
            )
            # We don't know live table count here (needs mgmt API) -- show registered count
            drift["brain_tables"] = registered_brain  # informational, not a gap count
            # Executor files: count registered .py files
            drift["executor_files"] = sum(
                1 for r in wiring.get("executor", [])
                if r.get("type") == "file"
            )
            # Skeleton docs: count registered skeleton file entries
            drift["skeleton_docs"] = sum(
                1 for r in wiring.get("skeleton", [])
                if r.get("type") == "file"
            )
        except Exception:
            pass
        # --- TASK-5.2.3: Task health check at boot ---
        task_health_warning = None
        try:
            th = t_task_health()
            if th.get("ok") and th.get("total_stale", 0) > 0:
                task_health_warning = th.get("warning")
        except Exception:
            pass
        # --- TASK-V8: Load behavioral_rules, infrastructure_map, credentials_index ---
        behavioral_rules_data = []
        infrastructure_map_data = []
        credentials_index_data = []
        migration_needed = False
        migration_missing = []
        try:
            # P1-07: Load top-40 rules. confidence>=0.9 always included first,
            # then remaining by priority. Low-signal rules (<0.5) excluded at DB level.
            br = t_get_behavioral_rules(domain=detected_domain, page="1", page_size="200")
            if br.get("migration_needed"):
                migration_needed = True
                migration_missing.append("behavioral_rules")
            else:
                all_rules = br.get("rules", []) or []
                # Split: high-confidence (>=0.9) always shown, rest sorted by priority
                critical_rules = [r for r in all_rules if float(r.get("confidence") or 0) >= 0.9]
                other_rules    = [r for r in all_rules if float(r.get("confidence") or 0) < 0.9]
                # Fill to 40: critical first, then others up to cap
                remaining_slots = max(0, 40 - len(critical_rules))
                behavioral_rules_data = critical_rules + other_rules[:remaining_slots]
        except Exception:
            migration_needed = True
            migration_missing.append("behavioral_rules")
        try:
            im = t_get_infrastructure()
            if im.get("migration_needed"):
                migration_needed = True
                migration_missing.append("infrastructure_map")
            else:
                infrastructure_map_data = im.get("components", [])
        except Exception:
            migration_needed = True
            migration_missing.append("infrastructure_map")
        try:
            ci = t_get_credentials_index()
            if ci.get("migration_needed"):
                migration_needed = True
                migration_missing.append("credentials_index")
            else:
                credentials_index_data = ci.get("credentials", [])
        except Exception:
            migration_needed = True
            migration_missing.append("credentials_index")
        # AGI-05/S3: Associative bridge -- surface 3 cross-domain KB entries related to current domain
        associated_context = []
        try:
            if detected_domain and detected_domain not in ("general", ""):
                # Get top keywords from domain KB entries
                domain_kb = sb_get("knowledge_base",
                    f"select=topic,instruction&id=gt.1&domain=eq.{detected_domain}&order=access_count.desc&limit=5",
                    svc=True) or []
                # Build keyword set from topics
                keywords = []
                for entry in domain_kb:
                    t = (entry.get("topic") or "").replace("_", " ").split()
                    keywords.extend([w for w in t if len(w) > 4])
                # Search OTHER domains for entries sharing those keywords
                seen_ids = set()
                for kw in keywords[:3]:  # top 3 keywords only -- keep it fast
                    from core_semantic import search as _sem
                    cross = _sem("knowledge_base", kw, limit=2,
                        filters=f"&domain=neq.{detected_domain}") or []
                    for r in cross:
                        rid = r.get("id")
                        if rid and rid not in seen_ids and len(associated_context) < 3:
                            associated_context.append({
                                "domain": r.get("domain", ""),
                                "topic": r.get("topic", ""),
                                "instruction": (r.get("instruction") or "")[:200],
                                "bridge_keyword": kw,
                            })
                            seen_ids.add(rid)
        except Exception:
            pass  # Non-fatal -- associative bridge is best-effort

        # P2-03: Load active goals for cross-session continuity
        active_goals = []
        try:
            active_goals = sb_get(
                "session_goals",
                "select=id,goal,domain,progress,status&status=eq.active&order=created_at.asc&limit=10",
                svc=True,
            ) or []
        except Exception as _ge:
            print(f"[SESSION] active_goals load failed (non-fatal): {_ge}")

        # P3-02: Load owner profile (top 10 high-confidence entries)
        owner_profile_data = []
        try:
            owner_profile_data = sb_get(
                "owner_profile",
                "select=dimension,value,confidence,domain"
                "&active=eq.true&order=confidence.desc,times_observed.desc&limit=10",
                svc=True,
            ) or []
        except Exception as _ope:
            print(f"[SESSION] owner_profile load failed (non-fatal): {_ope}")

        # P3-07: Load capability model (all domains, weakest first)
        capability_model_data = []
        weak_capability_domains = []
        try:
            cap_rows = sb_get(
                "capability_model",
                "select=domain,reliability,notes&order=reliability.asc&limit=20",
                svc=True,
            ) or []
            capability_model_data = cap_rows
            weak_capability_domains = [
                r["domain"] for r in cap_rows
                if float(r.get("reliability") or 1.0) < 0.60
            ]
        except Exception as _cme:
            print(f"[SESSION] capability_model load failed (non-fatal): {_cme}")

        previous_snapshot = _latest_session_snapshot_raw()
        session_snapshot = {
            "scope": "session_start",
            "generated_at": datetime.utcnow().isoformat(),
            "health": health.get("overall", "unknown"),
            "counts": state.get("counts", {}),
            "last_session_ts": state.get("last_session_ts", ""),
            "resume_task": {
                "id": resume_task_obj.get("id"),
                "status": resume_task_obj.get("status"),
                "priority": resume_task_obj.get("priority"),
            } if isinstance(resume_task_obj, dict) else None,
            "resume_checkpoint": _get_resume_checkpoint(resume_task_obj),
            "quality_alert": quality_alert,
            "training_pipeline_ok": training.get("pipeline_ok"),
            "training_health_flags": training.get("health_flags", []),
            "active_goals": [
                {
                    "goal": g.get("goal"),
                    "domain": g.get("domain"),
                    "status": g.get("status"),
                    "progress": g.get("progress"),
                }
                for g in (active_goals or [])[:10]
            ],
            "owner_profile": owner_profile_data[:5],
            "weak_capability_domains": weak_capability_domains[:10],
            "system_map_drift": drift,
        }
        try:
            _persist_session_snapshot(session_snapshot, scope="session_start")
        except Exception as _sp:
            print(f"[SESSION] snapshot persist failed (non-fatal): {_sp}")

        return {
            "ok": True,
            "health": health.get("overall", "unknown"),
            "components": health.get("components", {}),
            "counts": state.get("counts", {}),
            "last_session": state.get("last_session", ""),
            "last_session_ts": state.get("last_session_ts", ""),
            "in_progress_tasks": [t for t in pending_tasks_all if t.get("status") == "in_progress"],
            "pending_tasks": [t for t in pending_tasks_all if t.get("status") == "pending"],
            "resume_task": resume_task_obj,
            "session_md": state.get("session_md", ""),  # full SESSION.md content for claude.ai bootstrap
            "domain_mistakes": domain_mistakes_raw,
            "domain": detected_domain,
            "top_patterns": top_patterns,
            "quality_alert": quality_alert,
            "task_health_warning": task_health_warning,
            "behavioral_rules": behavioral_rules_data,
            "infrastructure_map": infrastructure_map_data,
            "credentials_index": credentials_index_data,
            "migration_needed": migration_needed,
            "migration_missing": migration_missing,
            "pending_evolutions": evolutions[:5] if isinstance(evolutions, list) else [],
            "training_pipeline": training,
            "live_tool_count": len(TOOLS),
            "system_map_drift": drift,
            "system_map": smap,
            "resume_checkpoint": _get_resume_checkpoint(resume_task_obj),
            "associated_context": associated_context,  # AGI-05/S3: cross-domain associative bridges
            "active_goals": active_goals,              # P2-03: cross-session goal continuity
            "owner_profile": owner_profile_data,          # P3-02: Vux behavioral model
            "weak_capability_domains": weak_capability_domains,  # P3-07: domains below 0.60 reliability
            "session_snapshot": session_snapshot,
            "previous_session_snapshot": previous_snapshot.get("snapshot"),
            "previous_session_snapshot_created_at": previous_snapshot.get("created_at"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _get_resume_checkpoint(resume_task_obj: dict) -> dict:
    """TASK-28.C: Fetch checkpoint_data from recent sessions for active resume_task.
    Returns checkpoint dict if found, else None."""
    try:
        if not resume_task_obj:
            return None
        rows = sb_get("sessions",
            "select=checkpoint_data,checkpoint_ts&order=created_at.desc&limit=5",
            svc=True) or []
        for row in rows:
            if row.get("checkpoint_data"):
                return row["checkpoint_data"]
        return None
    except Exception:
        return None


# -- TASK-28: Mid-Session State Checkpoint -----------------------------------
def t_checkpoint(active_task_id: str = "", last_action: str = "", last_result: str = "") -> dict:
    """Write a mid-session checkpoint to the current sessions row.
    Call after every subtask gate to prevent context collapse on long tasks.
    active_task_id: UUID of the task currently in progress.
    last_action: brief description of last completed action (e.g. 'patched core_train.py 29.B').
    last_result: outcome or next step (e.g. 'build SUCCESS, proceed to 29.C').
    On next session_start: resume_checkpoint field will contain this data."""
    try:
        from datetime import datetime
        checkpoint_data = {
            "active_task_id": active_task_id or "",
            "last_action": (last_action or "")[:500],
            "last_result": (last_result or "")[:500],
            "ts": datetime.utcnow().isoformat(),
        }
        # Write to most recent session row
        latest = sb_get("sessions", "select=id&order=created_at.desc&limit=1", svc=True)
        if not latest:
            return {"ok": False, "error_code": "no_session", "message": "No session row found", "retry_hint": False, "domain": "supabase"}
        session_id = latest[0]["id"]
        ok = sb_patch("sessions", f"id=eq.{session_id}", {
            "checkpoint_data": checkpoint_data,
            "checkpoint_ts": checkpoint_data["ts"],
        })
        if not ok:
            return {"ok": False, "error_code": "patch_failed", "message": "Failed to write checkpoint", "retry_hint": True, "domain": "supabase"}
        try:
            _persist_session_snapshot({
                "scope": "checkpoint",
                "generated_at": checkpoint_data["ts"],
                "session_id": session_id,
                "checkpoint": checkpoint_data,
                "last_action": checkpoint_data["last_action"],
                "last_result": checkpoint_data["last_result"],
            }, scope="checkpoint")
        except Exception:
            pass
        return {"ok": True, "session_id": session_id, "checkpoint": checkpoint_data}
    except Exception as e:
        return {"ok": False, "error_code": "exception", "message": str(e), "retry_hint": True, "domain": "supabase"}


def t_core_py_validate() -> dict:
    """Pre-deploy syntax checker for core_tools.py and core_main.py."""
    try:
        results = {}
        for target in ["core_tools.py", "core_main.py"]:
            content = _gh_blob_read(target)
            lines = content.splitlines()
            errors = []
            warnings = []
            size_kb = round(len(content.encode()) / 1024, 1)
            line_count = len(lines)
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("def def ") or stripped.startswith("import import "):
                    errors.append(f"L{i}: double keyword â€” {stripped[:60]}")
            if target == "core_tools.py":
                tool_fn_refs = _re.findall(r'"fn":\s*(t_\w+)', content)
                defined_fns  = set(_re.findall(r'^def (t_\w+)\(', content, _re.MULTILINE))
                for ref in tool_fn_refs:
                    if ref not in defined_fns:
                        errors.append(f"TOOLS refs '{ref}' but function not defined")
                if "\nTOOLS = {" not in content:
                    errors.append("TOOLS dict not found â€” critical corruption")
            for i, line in enumerate(lines, 1):
                # A.8: backboard.railway GQL endpoint is valid -- only flag old REST references
                if "backboard.railway.app/api" in line and "graphql" not in line.lower():
                    errors.append(f"L{i}: stale backboard.railway REST reference (use GQL endpoint)")
                if "core.py" in line and not line.strip().startswith("#"):
                    warnings.append(f"L{i}: stale core.py reference -- file deleted")
            if size_kb > 150:
                warnings.append(f"{target} is {size_kb}KB â€” consider splitting (>150KB)")
            triple_count = content.count('"""')
            if triple_count % 2 != 0:
                warnings.append(f"Odd number of triple-quotes ({triple_count}) â€” possible unclosed docstring")
            results[target] = {"ok": len(errors) == 0, "errors": errors,
                               "warnings": warnings, "line_count": line_count, "size_kb": size_kb}
        overall_ok = all(r["ok"] for r in results.values())
        return {"ok": overall_ok, "files": results}
    except Exception as e:
        return {"ok": False, "error": str(e), "errors": [str(e)], "warnings": []}


def t_search_in_file(path: str, pattern: str, repo: str = "",
                     regex: str = "false", case_sensitive: str = "false") -> dict:
    """Search for a pattern in a GitHub file."""
    try:
        content = _gh_blob_read(path, repo or GITHUB_REPO)
        lines = content.splitlines()
        matches = []
        use_regex = str(regex).lower() == "true"
        use_case  = str(case_sensitive).lower() == "true"
        flags = 0 if use_case else _re.IGNORECASE
        for i, line in enumerate(lines, 1):
            if use_regex:
                if _re.search(pattern, line, flags):
                    matches.append({"line": i, "content": line})
            else:
                hay = line if use_case else line.lower()
                ndl = pattern if use_case else pattern.lower()
                if ndl in hay:
                    matches.append({"line": i, "content": line})
        return {"ok": True, "path": path, "pattern": pattern,
                "regex": use_regex, "case_sensitive": use_case,
                "total_lines": len(lines), "matches": matches, "count": len(matches)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_multi_patch(path: str, patches: str, message: str, repo: str = "", dry_run: str = "false") -> dict:
    """Apply multiple find-replace patches via Blobs API (atomic commit, no SHA conflict, no size limit).
    Uses whitespace-normalized fallback matching + char-level diff hint on failure.
    dry_run=true: preview diff without pushing."""
    try:
        repo = repo or GITHUB_REPO
        if isinstance(patches, str):
            patches = json.loads(patches)
        content = _gh_blob_read(path, repo)
        applied = []
        skipped = []
        for i, patch in enumerate(patches):
            old = patch.get("old_str", "")
            new = patch.get("new_str", "")
            pf = _patch_find(content, old)
            found, count, matched, hint = pf[0], pf[1], pf[2], pf[3]
            auto_context = pf[4] if len(pf) > 4 else None
            if not found:
                skip_entry = {"index": i, "reason": "not found", "old_str": old[:80], "hint": hint}
                if auto_context: skip_entry["auto_context"] = auto_context
                skipped.append(skip_entry)
            elif count > 1:
                # Collect all occurrence locations for ambiguity resolution
                all_lines = content.splitlines()
                locs = []
                pos = 0
                while True:
                    idx = content.find(old, pos)
                    if idx == -1: break
                    ln = content[:idx].count('\n') + 1
                    cs, ce = max(0, ln-3), min(len(all_lines), ln+old.count('\n')+3)
                    locs.append({"line": ln, "context": "\n".join(f"{cs+j+1:4d}  {l}" for j,l in enumerate(all_lines[cs:ce]))})
                    pos = idx + 1
                    if len(locs) >= 5: break
                skipped.append({"index": i, "reason": f"ambiguous ({count}x)",
                                 "old_str": old[:80], "locations": locs,
                                 "fix_hint": "Extend old_str with unique surrounding lines from one location."})
            else:
                content = content.replace(matched, new, 1)
                applied.append({"index": i, "old_str": old[:80], "note": hint or "exact_match"})
        if not applied:
            return {
                "ok": False, "error_code": "no_patches_applied",
                "message": "No patches applied -- all old_str not found or ambiguous",
                "retry_hint": False, "domain": "github", "skipped": skipped,
                "fix_hint": "Check auto_context in skipped entries -- actual file content provided, use it to fix old_str."
            }
        # NEAR-MISS PROTECTION: fail hard if any patch was skipped
        if skipped:
            near_misses = [s for s in skipped if "near_miss" in str(s.get("hint", ""))]
            not_found   = [s for s in skipped if "near_miss" not in str(s.get("hint", ""))]
            return {
                "ok": False,
                "error_code": "partial_patch_blocked",
                "message": f"PARTIAL_PATCH_BLOCKED: {len(skipped)} of {len(patches)} patches skipped -- rolled back, nothing pushed.",
                "applied_count": len(applied),
                "skipped_count": len(skipped),
                "near_misses": near_misses,
                "not_found": not_found,
                "fix_hint": "Use auto_context in each skipped entry -- actual file content is embedded, no gh_read_lines needed.",
                "rolled_back": True,
            }
        if path.endswith(".py"):
            import py_compile, tempfile as _tmpf
            with _tmpf.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tf:
                tf.write(content); tmp = tf.name
            try:
                py_compile.compile(tmp, doraise=True)
            except py_compile.PyCompileError as e:
                import os; os.unlink(tmp)
                return {"ok": False, "error_code": "syntax_error", "message": f"Syntax error (patch not pushed): {e}", "retry_hint": False, "domain": "github"}
            finally:
                import os
                if os.path.exists(tmp): os.unlink(tmp)
        if str(dry_run).lower() == "true":
            import difflib as _dl_mp
            _orig = _gh_blob_read(path, repo)
            diff_preview = "".join(_dl_mp.unified_diff(
                _orig.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"{path} (before)", tofile=f"{path} (after)", n=2
            ))[:3000]
            return {"ok": True, "dry_run": True, "path": path,
                    "applied": len(applied), "diff": diff_preview, "details": applied}
        commit_sha = _gh_blob_write(path, content, message, repo)
        return {"ok": True, "path": path, "applied": len(applied), "skipped": 0,
                "details": applied, "commit": commit_sha[:12] if commit_sha else None}
    except Exception as e:
        return {"ok": False, "error_code": "exception", "message": str(e), "retry_hint": True, "domain": "github"}


def t_session_end(summary: str = "", actions: str = "", domain: str = "general",
                  patterns: str = "", quality: str = "0.8",
                  skill_file_updated: str = "false",
                  force_close: str = "false",
                  active_task_ids: str = "",
                  new_tool_sop: str = "",
                  tools_updated: str = "",
                  owner_corrections: str = "0") -> dict:  # E.1: explicit param
    """One-call session close.
    skill_file_updated: TASK-21.B gate. Pass 'true' after writing new rules to local skill file.
    force_close: pass 'true' to bypass all gates (owner explicit override).
    active_task_ids: pipe-separated UUIDs of tasks touched this session (e.g. 'uuid1|uuid2').
      session_end checks their status and warns if any are still pending/in_progress.
      Non-blocking -- warning only, Claude decides whether to patch or leave as-is.
    new_tool_sop: if non-empty, signals a new SOP was established this session affecting a specific tool.
      Triggers tools_updated gate -- session_end blocks until TOOLS dict is updated for that tool.
    tools_updated: pipe-separated tool names whose TOOLS dict description was updated this session.
      Required when new_tool_sop is set. Pass tool name(s) to confirm TOOLS dict was patched.
    Always: logs session to Supabase, runs Groq hot_reflection, scans system_map. SESSION.md is static."""
    from core_train import auto_hot_reflection
    try:
        session_start_at = datetime.utcnow()  # anchor: start of close call, used for duration
        if isinstance(actions, list):
            actions_list = [str(a).strip() for a in actions if str(a).strip()]
        else:
            # pipe-only split -- commas appear naturally inside action descriptions
            actions_list = [a.strip() for a in str(actions).split("|") if a.strip()]
        try:
            q = min(1.0, max(0.0, float(quality)))  # clamp to valid range 0.0-1.0
        except:
            q = 0.8

        # TASK-21.B: skill_file_updated gate
        # If patterns were noted this session but skill file was not confirmed written,
        # block session_end and return a warning unless force_close=true.
        # TASK-21.B gate RETIRED (owner directive 2026-03-19):
        # All new rules go to Supabase behavioral_rules only. Never write to local skill file.
        # skill_file_updated param retained for API compat but gate is permanently disabled.
        _skill_ok = True  # always passes
        _force = str(force_close).strip().lower() in ("true", "1", "yes")

        # TASK-23.B: tools_updated gate
        # If a new SOP was established this session affecting a specific tool,
        # block close until TOOLS dict description is confirmed updated for that tool.
        _new_sop = str(new_tool_sop).strip()
        _tools_updated = str(tools_updated).strip()
        if _new_sop and not _tools_updated and not _force:
            return {
                "ok": False,
                "blocked": True,
                "reason": "tools_dict_not_updated",
                "warning": (
                    f"New SOP established this session affecting tool: '{_new_sop}'. "
                    "TOOLS dict description must be updated for the affected tool(s) before closing. "
                    "Patch the TOOLS dict entry in core_tools.py via patch_file, then call session_end "
                    "with tools_updated='tool_name'. To skip: pass force_close=true."
                ),
                "new_tool_sop": _new_sop,
            }

        # 1. Log session to Supabase
        session_created_at = session_start_at.isoformat()
        session_ok = sb_post("sessions", {
            "summary": summary,
            "actions": actions_list,
            "interface": "claude-desktop"
        })

        # 2. Always run Groq-powered hot reflection
        # Pass session_created_at as anchor -- auto_hot_reflection uses last hot_reflection
        # timestamp as the enrichment window lower bound, not this value directly.
        # But we pass it as fallback in case no prior hot_reflection exists.
        caller_patterns = [p.strip() for p in patterns.split("|") if p.strip()]
        r_ok = auto_hot_reflection({
            "summary": summary,
            "actions": actions_list,
            "interface": "claude-desktop",
            "domain": domain,
            "quality": q,
            "seed_patterns": caller_patterns,
            "created_at": session_created_at,
        })
        reflection_id = "logged" if r_ok else "failed"

        # 3. SESSION.md is static -- tasks live in Supabase, session log in sessions table.
        # No writes to SESSION.md: eliminates spurious Railway redeploys on every session close.
        duration_seconds = int((datetime.utcnow() - session_start_at).total_seconds())

        # 4. Scan system_map - detect drift, update volatile rows
        smap_scan = {"ok": False, "error": "skipped"}
        try:
            smap_scan = t_system_map_scan(trigger="session_end")
        except Exception as e:
            smap_scan = {"ok": False, "error": str(e)}

        # 5. Task status check -- warn if active tasks still open at close
        task_warnings = []
        if active_task_ids and active_task_ids.strip():
            ids = [i.strip() for i in active_task_ids.split("|") if i.strip()]
            for tid in ids:
                try:
                    rows = sb_get("task_queue",
                        f"select=id,task,status&id=eq.{tid}&limit=1", svc=True)
                    if rows and isinstance(rows, list) and rows[0]:
                        row = rows[0]
                        if row.get("status") in ("pending", "in_progress"):
                            task_str = str(row.get("task", ""))[:80]
                            task_warnings.append({
                                "id": tid,
                                "status": row.get("status"),
                                "task": task_str,
                                "hint": "Call sb_patch to update status before closing, or pass intentionally."
                            })
                except Exception:
                    pass

        # 5.5 AGI-03/S2: Counterfactual analyzer
        # For each mistake logged this session, write a hot_reflection with domain=causal
        # capturing what the correct action would have been.
        # Also close out any open causal_predictions from this session.
        counterfactuals_written = 0
        try:
            # E.2: 4h window -- session_start_at is the time session_end was CALLED, not when session started
            session_ts_anchor = (session_start_at - timedelta(hours=4)).isoformat()
            recent_mistakes = sb_get("mistakes",
                f"select={_sel_force('mistakes', ['id','domain','context','what_failed','correct_approach','root_cause','how_to_avoid','severity'])}&created_at=gte.{session_ts_anchor}&order=created_at.desc&limit=10",
                svc=True) or []
            for m in recent_mistakes:
                try:
                    cf_content = (
                        f"COUNTERFACTUAL ANALYSIS\n"
                        f"Domain: {m.get('domain','')}\n"
                        f"What failed: {(m.get('what_failed') or '')[:300]}\n"
                        f"Root cause: {(m.get('root_cause') or '')[:300]}\n"
                        f"Correct approach: {(m.get('correct_approach') or '')[:300]}\n"
                        f"Prevention: {(m.get('how_to_avoid') or '')[:300]}\n"
                        f"Counterfactual: If correct_approach had been applied, "
                        f"the failure mode would not have occurred. "
                        f"Pattern to reinforce: {(m.get('how_to_avoid') or '')[:200]}"
                    )
                    sb_post("hot_reflections", {
                        "domain": "causal",
                        "quality_score": 0.5 if m.get("severity") == "critical" else 0.7,
                        "summary": f"Counterfactual: {(m.get('what_failed') or '')[:120]}",
                        "content": cf_content,
                        "interface": "session_end_counterfactual",
                        "processed_by_cold": False,
                    })
                    counterfactuals_written += 1
                except Exception:
                    pass
            # Mark open causal_predictions for this session as outcome=session_ended
            try:
                sb_patch("causal_predictions",
                    f"session_id=eq.unknown&actual_outcome=is.null",
                    {"actual_outcome": "session_ended_no_outcome_recorded"})
            except Exception:
                pass
        except Exception:
            pass

        # AGI-06: Capability metrics -- multi-dimensional session scoring
        cap_metrics = {}
        try:
            # E.2: widen to 4h (session_start_at is call time, not actual session start)
            session_ts_cap = (datetime.utcnow() - timedelta(hours=4)).isoformat()
            session_mistakes = sb_get("mistakes",
                f"select=id,severity&created_at=gte.{session_ts_cap}&limit=20",
                svc=True) or []
            n_mistakes = len(session_mistakes)
            n_critical = sum(1 for m in session_mistakes if m.get("severity") in ("critical", "high"))
            n_actions = max(len(actions_list), 1)

            # Derive dimensions from available session data
            # ACCURACY: correctness without rework -- penalizes mistakes per action
            accuracy = max(0.0, round(1.0 - min(n_mistakes / n_actions, 1.0), 3))

            # EFFICIENCY: quality score directly (proxy for actions-to-outcome ratio)
            efficiency = round(q, 3)

            # AUTONOMY: E.1 -- use explicit owner_corrections param instead of fragile string match
            try:
                n_corrections = int(owner_corrections) if str(owner_corrections).isdigit() else 0
            except Exception:
                n_corrections = 0
            autonomy = max(0.0, round(1.0 - min(n_corrections * 0.2, 1.0), 3))

            # ROBUSTNESS: 1.0 if no critical/high mistakes, degrades with severity
            robustness = max(0.0, round(1.0 - (n_critical * 0.3), 3))

            # LEARNING RATE: improving if quality >= 0.8, stable 0.6-0.79, declining <0.6
            learning_rate = 1.0 if q >= 0.8 else (0.7 if q >= 0.6 else 0.4)

            # TRANSFER: did session produce cross-domain insights (patterns non-empty = yes)
            transfer = 0.8 if (caller_patterns and q >= 0.7) else 0.5

            composite = round((accuracy + efficiency + autonomy + robustness + learning_rate + transfer) / 6, 3)

            cap_metrics = {
                "accuracy": accuracy,
                "efficiency": efficiency,
                "autonomy": autonomy,
                "robustness": robustness,
                "learning_rate": learning_rate,
                "transfer": transfer,
                "composite_score": composite,
            }
            sb_post("capability_metrics", {
                "domain": domain,
                "accuracy": accuracy,
                "efficiency": efficiency,
                "autonomy": autonomy,
                "robustness": robustness,
                "learning_rate": learning_rate,
                "transfer": transfer,
                "composite_score": composite,
                "raw_data": {"n_mistakes": n_mistakes, "n_critical": n_critical,
                             "n_actions": n_actions, "quality": q},
                "notes": summary[:200] if summary else "",
            })
        except Exception:
            pass

        # P2-06: Close out pending pattern_outcome rows — fill quality_after for this session
        pattern_outcomes_closed = 0
        try:
            pending_outcomes = sb_get(
                "pattern_outcome",
                "select=id,pattern_key,quality_before&outcome=eq.pending&limit=20",
                svc=True,
            ) or []
            for row in pending_outcomes:
                oid = row.get("id")
                q_before = float(row.get("quality_before") or 0)
                if not oid:
                    continue
                outcome = "neutral"
                if q - q_before > 0.05:
                    outcome = "improved"
                elif q_before - q > 0.05:
                    outcome = "degraded"
                try:
                    sb_patch("pattern_outcome", f"id=eq.{oid}", {
                        "quality_after": q,
                        "outcome":       outcome,
                    })
                    pattern_outcomes_closed += 1
                    # Mark pattern stale if it degraded quality
                    if outcome == "degraded":
                        try:
                            sb_patch("pattern_frequency",
                                f"pattern_key=eq.{row.get('pattern_key','')[:200]}",
                                {"stale": True})
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass  # pattern_outcome table may not exist yet -- non-fatal

        # Build return -- surface reflection failure and task warnings
        result = {
            "ok": session_ok,
            "session_logged": session_ok,
            "reflection_logged": reflection_id,
            "training_ok": r_ok,
            "system_map_scan": smap_scan,
            "actions_count": len(actions_list),
            "duration_seconds": duration_seconds,
            "skill_file_updated": _skill_ok,
            "tools_updated": _tools_updated if _tools_updated else "none",
            "new_tool_sop": _new_sop if _new_sop else "none",
            "counterfactuals_written": counterfactuals_written,
            "capability_metrics": cap_metrics,
            "pattern_outcomes_closed": pattern_outcomes_closed,  # P2-06
        }
        if not r_ok:
            result["reflection_warning"] = (
                "Hot reflection failed to log. Training pipeline will miss this session. "
                "Check Railway logs for [HOT] error. Common cause: Groq timeout or Supabase 400."
            )
        if task_warnings:
            result["task_status_warnings"] = task_warnings
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_core_py_rollback(commit_sha: str, file: str = "core_main.py") -> dict:
    """Emergency restore: fetch any CORE source file at a commit SHA, write back, redeploy.
    Defaults to core_main.py. core.py is deleted â€” do not use."""
    try:
        if not commit_sha or len(commit_sha) < 6:
            return {"ok": False, "error": "commit_sha required (min 6 chars)"}
        target = file if file else "core_main.py"
        if target == "core.py":
            return {"ok": False, "error": "core.py has been deleted. Use core_main.py or core_tools.py."}
        h = _ghh()
        ref_r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{commit_sha}",
                          headers=h, timeout=10)
        ref_r.raise_for_status()
        full_sha = ref_r.json()["sha"]
        short_sha = full_sha[:12]
        file_r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{target}?ref={full_sha}",
                           headers=h, timeout=30)
        file_r.raise_for_status()
        old_content = base64.b64decode(file_r.json()["content"]).decode()
        new_commit = _gh_blob_write(
            target, old_content,
            f"rollback: restore {target} from {short_sha}"
        )
        deploy = t_redeploy(f"rollback {target} to {short_sha}")
        notify_owner(f"ROLLBACK triggered â€” {target} restored from {short_sha}. Deploying...")
        return {
            "ok": True,
            "file": target,
            "restored_from": short_sha,
            "new_commit": new_commit[:12],
            "redeploying": deploy.get("ok", False),
            "note": "Use build_status to confirm deploy succeeds"
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_diff(path: str = "", sha_a: str = "", sha_b: str = "main") -> dict:
    """Compare a file between two commits and return a unified diff."""
    try:
        h = _ghh()
        def fetch_at(ref):
            r = httpx.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref={ref}",
                headers=h, timeout=20
            )
            r.raise_for_status()
            return base64.b64decode(r.json()["content"]).decode().splitlines(keepends=True)
        if sha_b == "main" or len(sha_b) < 20:
            ref_r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/git/ref/heads/{sha_b if sha_b != 'main' else 'main'}",
                              headers=h, timeout=10)
            sha_b_full = ref_r.json()["object"]["sha"] if ref_r.is_success else sha_b
        else:
            sha_b_full = sha_b
        if sha_a == "prev":
            commit_r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/git/commits/{sha_b_full}",
                                 headers=h, timeout=10)
            commit_r.raise_for_status()
            parents = commit_r.json().get("parents", [])
            if not parents:
                return {"ok": False, "error": "No parent commit found"}
            sha_a = parents[0]["sha"]
        lines_a = fetch_at(sha_a)
        lines_b = fetch_at(sha_b_full)
        diff = list(difflib.unified_diff(
            lines_a, lines_b,
            fromfile=f"{path}@{sha_a[:8]}",
            tofile=f"{path}@{sha_b_full[:8]}",
            n=3
        ))
        diff_text = "".join(diff)
        added   = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        return {
            "ok": True, "path": path,
            "sha_a": sha_a[:12], "sha_b": sha_b_full[:12],
            "added": added, "removed": removed,
            "diff": diff_text[:8000] if diff_text else "(no changes)"
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_deploy_and_wait(reason: str = "", timeout: str = "120") -> dict:
    """Trigger VM redeploy and wait for health check to pass.
    Replaces old Railway deploy-and-wait — CORE now runs on Oracle VM."""
    try:
        deploy_result = t_redeploy(reason=reason)
        if not deploy_result.get("ok"):
            return deploy_result
        import time as _time
        max_wait = min(int(timeout), 120)
        for _ in range(max_wait // 5):
            _time.sleep(5)
            try:
                h = t_health()
                if h.get("overall") == "ok":
                    return {"ok": True, "reason": reason, "health": "ok", "host": "oracle_vm"}
            except Exception:
                pass
        return {"ok": False, "reason": reason, "error": f"Health check did not pass within {max_wait}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_railway_logs_live(lines: str = "50", keyword: str = "") -> dict:
    """Fetch live CORE service logs from Oracle VM via journalctl.
    Replaces old Railway GraphQL log fetcher — CORE now runs on Oracle VM.
    lines: number of log lines to return (default 50).
    keyword: optional filter string (grep on output)."""
    try:
        n = max(1, min(int(lines), 500))
        cmd = ["journalctl", "-u", "core-agi", "-n", str(n), "--no-pager", "--output=short"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        output = result.stdout or result.stderr or "(no output)"
        if keyword:
            filtered = [l for l in output.splitlines() if keyword.lower() in l.lower()]
            output = "\n".join(filtered) or f"(no lines matching '{keyword}')"
        return {"ok": True, "source": "oracle_vm_journalctl", "lines": output.splitlines(), "raw": output[:8000]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_railway_env_get(key: str = "") -> dict:
    """Read CORE env vars from Oracle VM .env file.
    Replaces old Railway GraphQL env getter — CORE now runs on Oracle VM.
    key: specific var name. Empty = return all var names (values redacted)."""
    try:
        env_path = "/home/ubuntu/core-agi/.env"
        with open(env_path) as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        if key:
            for line in lines:
                if line.startswith(f"{key}="):
                    return {"ok": True, "key": key, "value": line.split("=", 1)[1].strip('"').strip("'")}
            return {"ok": False, "error": f"Key '{key}' not found in .env"}
        return {"ok": True, "keys": [l.split("=")[0] for l in lines if "=" in l],
                "note": "values redacted — use key= to read a specific value"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_railway_env_set(key: str, value: str) -> dict:
    """Write a CORE env var to Oracle VM .env file + restart service.
    Replaces old Railway GraphQL env setter — CORE now runs on Oracle VM.
    key: var name. value: new value. Restarts core-agi after write."""
    try:
        env_path = "/home/ubuntu/core-agi/.env"
        with open(env_path) as f:
            lines = f.readlines()
        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith(f"{key}="):
                new_lines.append(f'{key}="{value}"\n')
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f'{key}="{value}"\n')
        with open(env_path, "w") as f:
            f.writelines(new_lines)
        subprocess.run(["systemctl", "restart", "core-agi"], timeout=10)
        notify(f"\u2699\ufe0f CORE env updated: {key} set. Service restarting.")
        return {"ok": True, "key": key, "action": "written_and_restarted"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_railway_service_info() -> dict:
    """VM service snapshot: systemd status, uptime, memory, latest git commit.
    Replaces old Railway GraphQL service info — CORE now runs on Oracle VM."""
    try:
        status = subprocess.run(["systemctl", "show", "core-agi",
            "--property=ActiveState,SubState,MainPID,MemoryCurrent,ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        git = subprocess.run(["git", "-C", "/home/ubuntu/core-agi", "log", "--oneline", "-3"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        parsed = dict(line.split("=", 1) for line in status.splitlines() if "=" in line)
        return {"ok": True, "host": "oracle_vm", "service": "core-agi",
                "state": parsed.get("ActiveState"), "sub": parsed.get("SubState"),
                "pid": parsed.get("MainPID"), "since": parsed.get("ActiveEnterTimestamp"),
                "recent_commits": git.splitlines()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_ping_health() -> dict:
    """Direct health check - calls t_health() internally without HTTP."""
    try:
        return t_health()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_verify_live(expected_text: str, timeout: str = "90") -> dict:
    """Poll /state until expected_text appears."""
    try:
        t_secs = int(timeout) if timeout else 90
        railway_url = os.environ.get("PUBLIC_URL",
                     f"https://{os.environ.get('PUBLIC_DOMAIN', 'core-agi.duckdns.org')}")
        deadline = time.time() + t_secs
        poll_count = 0
        while time.time() < deadline:
            try:
                r = httpx.get(f"{railway_url}/state", timeout=8)
                if r.is_success and expected_text in r.text:
                    return {"ok": True, "found": True, "polls": poll_count,
                            "elapsed_s": round(time.time() - (deadline - t_secs))}
            except Exception:
                pass
            time.sleep(8)
            poll_count += 1
        return {"ok": False, "found": False, "polls": poll_count, "timeout_s": t_secs,
                "note": "Text not found in /state within timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- Signal extraction --------------------------------------------------------
def extract_signals(task: str) -> dict:
    t = task.lower()
    intent = "generate"
    for kw, v in [("fix","fix"),("debug","fix"),("error","fix"),("broken","fix"),
                  ("explain","explain"),("what is","explain"),("how does","explain"),("teach","explain"),
                  ("find","lookup"),("search","lookup"),("who is","lookup"),("when did","lookup"),
                  ("analyze","analyze"),("review","analyze"),("check","validate"),("is this","validate"),
                  ("write","generate"),("create","generate"),("build","build"),("make","build"),
                  ("should i","decide"),("which","decide"),("recommend","decide"),
                  ("help","support"),("overwhelmed","support"),("worried","support"),("scared","support"),
                  ("plan","orchestrate"),("steps to","orchestrate"),("how to","orchestrate")]:
        if kw in t: intent = v; break
    domain = "general"
    for kw, d in [("def ","code"),("function","code"),("import ","code"),("class ","code"),("sql","code"),
                  ("contract","legal"),("liability","legal"),("clause","legal"),("lawsuit","legal"),
                  ("invoice","finance"),("revenue","finance"),("cash flow","finance"),("tax","finance"),
                  ("patient","medical"),("symptoms","medical"),("diagnosis","medical"),("medication","medical"),
                  ("marketing","business"),("customers","business"),("startup","business"),("sales","business"),
                  ("essay","academic"),("research","academic"),("thesis","academic"),("cite","academic"),
                  ("content","creative"),("story","creative"),("blog","creative"),("design","creative")]:
        if kw in t: domain = d; break
    expertise = 3
    beginner_markers = ["what is","how do i","i don't know","explain","simple","basic","beginner","noob"]
    expert_markers   = ["implement","optimize","architecture","idiomatic","edge case","tradeoff","latency","throughput","refactor"]
    if any(m in t for m in beginner_markers): expertise = 2
    if any(m in t for m in expert_markers):   expertise = 4
    if len(task.split()) <= 5 and "?" not in task: expertise = max(expertise, 4)
    emotion = "neutral"
    if any(m in t for m in ["asap","urgent","deadline","help!","tolong","buru","cepat"]): emotion = "urgent"
    elif any(m in t for m in ["still","again","doesn't work","still not"]): emotion = "frustrated"
    elif any(m in t for m in ["worried","scared","overwhelmed","anxious"]): emotion = "vulnerable"
    elif any(m in t for m in ["lol","btw","just wondering","haha"]): emotion = "casual"
    stakes = "medium"
    if any(m in t for m in ["quick","short","brief","simple","just"]): stakes = "low"
    if any(m in t for m in ["production","deploy","contract","legal","medical","critical"]): stakes = "high"
    if any(m in t for m in ["life","death","emergency"]): stakes = "critical"
    archetype_map = {
        "lookup": "A1", "explain": "A4", "generate": "A3", "fix": "A4",
        "analyze": "A4", "validate": "A8", "build": "A5", "decide": "A6",
        "orchestrate": "A7", "support": "A9",
    }
    return {"intent": intent, "domain": domain, "expertise": expertise,
            "emotion": emotion, "stakes": stakes, "archetype": archetype_map.get(intent, "A3")}



def t_ask(question: str, domain: str = ""):
    if not question: return {"ok": False, "error": "question required"}
    from core_reasoning_packet import build_reasoning_packet
    from core_config import GROQ_MODEL, GROQ_FAST
    pkt = build_reasoning_packet(question, domain=domain, limit=10, per_table=2)
    packet = pkt.get("packet") or {}
    kb_context = packet.get("context", "")
    runtime_facts = {
        "orchestrator_model_policy": {
            "intent_and_structure": "Gemini 2.5 Flash",
            "planning_and_final_synthesis": "Groq " + str(GROQ_MODEL),
            "fallback": "Gemini 2.5 Flash",
            "fast_lane": str(GROQ_FAST),
        },
        "source_of_truth": "Current code/runtime facts override stale KB memory when they conflict.",
        "live_model_split": "Structured extraction/classification uses Gemini; prose synthesis and tool-answering use Groq unless Groq fails.",
    }
    system = (
        "You are CORE, a personal AGI assistant with accumulated knowledge from many sessions. "
        "You must answer from the most current evidence available. If runtime facts conflict with memory, runtime facts win. "
        "Be specific and actionable."
    )
    user = f"Question: {question}\n\n"
    user += "CURRENT_RUNTIME_FACTS:\n" + json.dumps(runtime_facts, indent=2, ensure_ascii=False) + "\n\n"
    if kb_context: user += f"Relevant knowledge:\n{kb_context}\n\n"
    if packet.get("focus"): user += f"Focus:\n{packet.get('focus')}\n\n"
    user += "Answer using runtime facts first, then relevant knowledge, and explicitly say if something is only inferred."
    kb_hit_count = int((packet.get("memory_by_table") or {}).get("knowledge_base") or 0)
    memory_hits = int(packet.get("hit_count") or 0)
    try:
        answer = groq_chat(system, user, model=GROQ_MODEL, max_tokens=512)
        return {"ok": True, "answer": answer, "kb_hits": kb_hit_count, "memory_hits": memory_hits, "memory_by_table": packet.get("memory_by_table", {}), "packet_focus": packet.get("focus", ""), "model": GROQ_MODEL, "question": question}
    except Exception:
        # I.1: Gemini fallback -- if Groq is down, ask() must not fail entirely
        try:
            answer = gemini_chat(system, user, max_tokens=512)
            return {"ok": True, "answer": answer, "kb_hits": kb_hit_count, "memory_hits": memory_hits, "memory_by_table": packet.get("memory_by_table", {}), "packet_focus": packet.get("focus", ""), "model": "gemini_fallback", "question": question}
        except Exception as e2:
            return {"ok": False, "error": str(e2), "note": "both Groq and Gemini fallback failed"}



def t_stats():
    try:
        hots = sb_get("hot_reflections", "select=domain,quality_score&limit=200", svc=True)
        domain_counts: Counter = Counter(h.get("domain","general") for h in hots)
        patterns = sb_get("pattern_frequency", "select=pattern_key,frequency,domain&stale=eq.false&order=frequency.desc&limit=10", svc=True)
        mistakes = sb_get("mistakes", "select=domain&limit=200", svc=True)
        mistake_counts: Counter = Counter(m.get("domain","general") for m in mistakes)
        scores = [min(1.0, max(0.0, float(h["quality_score"]))) for h in hots if h.get("quality_score") is not None]
        avg_quality = round(sum(scores) / len(scores), 2) if scores else None
        counts = get_system_counts()
        
        # A.10: fix evolution counts -- select=count without Prefer header returns wrong shape.
        # Use slim select + len() which works correctly with existing sb_get implementation.
        def _evo_count(status):
            try:
                return len(sb_get("evolution_queue",
                    f"select=id&status=eq.{status}", svc=True) or [])
            except Exception:
                return 0
        evo_counts = {
            "pending":  _evo_count("pending"),
            "applied":  _evo_count("applied"),
            "rejected": _evo_count("rejected"),
        }
        
        cold_rows = sb_get("cold_reflections", "select=created_at&order=created_at.desc&limit=1", svc=True) or []
        last_cold = cold_rows[0].get("created_at", "never") if cold_rows else "never"
        return {
            "ok": True,
            "total_sessions": counts.get("sessions", 0),
            "knowledge_entries": counts.get("knowledge_base", 0),
            "total_mistakes": counts.get("mistakes", 0),
            "hot_reflections": len(hots),
            "avg_quality_score": avg_quality,
            "domain_distribution": dict(domain_counts.most_common(8)),
            "mistake_distribution": dict(mistake_counts.most_common(6)),
            "top_patterns": [{"pattern": p.get("pattern_key","")[:80], "freq": p.get("frequency",0), "domain": p.get("domain","")} for p in patterns],
            "evolution_queue": evo_counts,
            "last_cold_processor_run": last_cold,
            "quality_trend_7d": t_get_quality_trend("7"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_search_mistakes(query: str = "", domain: str = "", limit: int = 10):
    try:
        lim = int(limit) if limit else 10
        # C.4: add id=gt.1, expand search to root_cause+how_to_avoid fields
        qs = f"select={_sel_force('mistakes', ['domain','context','what_failed','correct_approach','root_cause','how_to_avoid','severity'])}&order=created_at.desc&limit={lim}&id=gt.1"
        if domain and domain not in ("all", ""): qs += f"&domain=eq.{domain}"
        if query:
            from core_semantic import search as sem_search
            domain_filter = f"&domain=eq.{domain}" if domain and domain not in ("all", "") else ""
            results = sem_search("mistakes", query, limit=lim, filters=domain_filter)
        else:
            results = sb_get("mistakes", qs, svc=True) or []
        return {"ok": True, "count": len(results), "mistakes": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- Background Researcher (globals + helpers only â€” loop lives in core_train) ----
_RESEARCH_DOMAINS = [
    ("code",     ["debug this python function", "optimize SQL query", "refactor async code"]),
    ("business", ["improve cash flow", "write investor pitch", "reduce churn"]),
    ("legal",    ["draft NDA", "understand terms of service", "IP protection for startup"]),
    ("creative", ["write product description", "social media strategy", "brand voice guide"]),
    ("academic", ["summarize research paper", "explain statistical method"]),
    ("medical",  ["explain diagnosis", "medication interaction check"]),
    ("finance",  ["build financial model", "tax optimization", "runway calculation"]),
    ("data",     ["clean messy dataset", "visualize trends", "build dashboard"]),
]


def _backlog_add(items: list) -> list:
    """Write new backlog items to Supabase backlog table."""
    try:
        existing_rows = sb_get("backlog", "select=title&order=id.asc&limit=500", svc=True)
        existing_titles = {r.get("title", "").lower() for r in existing_rows}
    except Exception as e:
        print(f"[BACKLOG] fetch existing error: {e}")
        existing_titles = set()
    new_items = []
    for item in items:
        title = item.get("title", "").strip()
        if not title or title.lower() in existing_titles:
            continue
        existing_titles.add(title.lower())
        priority = int(item.get("priority", 1))
        itype    = item.get("type", "other")
        effort   = item.get("effort", "medium")
        domain   = item.get("domain", "general")
        ok = sb_post("backlog", {
            "title":        title,
            "type":         itype,
            "priority":     priority,
            "description":  item.get("description", "")[:500],
            "domain":       domain,
            "effort":       effort,
            "impact":       item.get("impact", "medium"),
            "status":       "pending",
            "discovered_at": item.get("discovered_at", datetime.utcnow().isoformat()),
        })
        if ok:
            new_items.append(item)
    return new_items


def _sync_backlog_status():
    """No-op: backlog status is managed directly in the backlog table."""
    return 0


def _repopulate_evolution_queue():
    """DISABLED: Backlog items are never pushed to evolution_queue."""
    print("[RESEARCH] _repopulate_evolution_queue: disabled â€” backlog items never go to evolution_queue")
    return 0


def _backlog_to_markdown() -> str:
    """Generate BACKLOG.md from Supabase backlog table."""
    _sync_backlog_status()
    try:
        rows = sb_get("backlog", "select=*&order=priority.desc&limit=500", svc=True)
    except Exception as e:
        return f"# CORE Improvement Backlog\n\n_Error reading backlog: {e}_\n"
    if not rows:
        return "# CORE Improvement Backlog\n\n_No items yet._\n"
    total     = len(rows)
    n_done    = sum(1 for b in rows if b.get("status") == "done")
    n_prog    = sum(1 for b in rows if b.get("status") == "in_progress")
    n_pending = total - n_done - n_prog
    lines = [
        "# CORE Improvement Backlog",
        f"\n_Auto-generated. Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}_",
        f"_Total: {total} | Pending: {n_pending} | In Progress: {n_prog} | Done: {n_done}_\n",
        "---\n",
    ]
    by_type: dict = {}
    for item in rows:
        by_type.setdefault(item.get("type", "other"), []).append(item)
    type_labels = {
        "new_tool": "New Tools", "logic_improvement": "Logic Improvements",
        "new_kb": "Knowledge Gaps", "telegram_command": "Telegram Commands",
        "performance": "Performance", "missing_data": "Missing Data", "other": "Other",
    }
    status_icon = {"done": "[x]", "in_progress": "[~]", "pending": "[ ]"}
    for t, items in by_type.items():
        n_t_done = sum(1 for i in items if i.get("status") == "done")
        lines.append(f"## {type_labels.get(t, t)} ({n_t_done}/{len(items)} done)\n")
        for item in items:
            p      = item.get("priority", 1)
            status = item.get("status", "pending")
            s_icon = status_icon.get(status, "[ ]")
            lines.append(f"### {s_icon} P{p}: {item.get('title','')}")
            lines.append(f"- **Status:** {status} | **Type:** {t} | **Effort:** {item.get('effort','?')} | **Impact:** {item.get('impact','?')} | **Domain:** {item.get('domain','?')}")
            lines.append(f"- **What:** {item.get('description','')}")
            lines.append(f"- **Discovered:** {item.get('discovered_at','')[:16]}")
            lines.append("")
    lines.append("---\n_CORE runs background_researcher every 60 min._")
    lines.append("_Use `/backlog` in Telegram or `get_backlog` MCP tool to review._")
    return "\n".join(lines)


# -- KB Mining ----------------------------------------------------------------
def run_kb_mining(max_batches: int = 50, force: bool = False) -> dict:
    """Mine KB in batches to populate backlog."""
    try:
        counts = get_system_counts()
        kb_count = counts.get("knowledge_base", 0)
        backlog_count = int(httpx.get(
            f"{SUPABASE_URL}/rest/v1/backlog?select=id&limit=1",
            headers=_sbh_count_svc(), timeout=10
        ).headers.get("content-range", "*/0").split("/")[-1])
        if not force and backlog_count >= kb_count / KB_MINE_RATIO_THRESHOLD:
            msg = f"[KB MINE] Skipped - backlog ({backlog_count}) sufficient vs KB ({kb_count})."
            print(msg)
            return {"ok": True, "skipped": True, "reason": msg}
        notify(f"KB Mining started\nScanning {kb_count} KB entries in batches of {KB_MINE_BATCH_SIZE}")
        total_new = 0
        offset = 0
        batches_done = 0
        system = """You are CORE's KB mining engine. Identify gaps from KB entries.
Output MUST be a JSON array of 3-5 items:
[{"priority": 1-5, "type": "new_tool|logic_improvement|new_kb|telegram_command|performance|missing_data",
 "title": "short title", "description": "actionable description", "effort": "low|medium|high", "impact": "low|medium|high"}]
Output ONLY valid JSON array, no preamble."""
        while batches_done < max_batches:
            kb_batch = sb_get("knowledge_base",
                              f"select=domain,topic,content&order=id.asc&limit={KB_MINE_BATCH_SIZE}&offset={offset}",
                              svc=True)
            if not kb_batch:
                break
            batch_text = "\n".join([
                f"[{r.get('domain','?')}] {r.get('topic','?')}: {str(r.get('content',''))[:150]}"
                for r in kb_batch
            ])
            domains_in_batch = list({r.get("domain","general") for r in kb_batch})
            user = (f"KB batch ({len(kb_batch)} entries, domains: {', '.join(domains_in_batch)}):\n\n"
                    f"{batch_text}\n\nWhat gaps does CORE need to address?")
            try:
                raw = groq_chat(system, user, model=GROQ_MODEL, max_tokens=700)
                raw = raw.strip()
                if raw.startswith("```"): raw = raw.split("```")[1]
                if raw.startswith("json"): raw = raw[4:]
                items = json.loads(raw.strip())
                if isinstance(items, list):
                    for item in items:
                        if not item.get("domain"):
                            item["domain"] = domains_in_batch[0] if domains_in_batch else "general"
                        item["discovered_at"] = datetime.utcnow().isoformat()
                        item["status"] = "pending"
                    new = _backlog_add(items)
                    total_new += len(new)
            except Exception as e:
                print(f"[KB MINE] Batch {batches_done+1} error: {e}")
            offset += KB_MINE_BATCH_SIZE
            batches_done += 1
            if len(kb_batch) < KB_MINE_BATCH_SIZE:
                break
            time.sleep(3)
        final_count = int(httpx.get(
            f"{SUPABASE_URL}/rest/v1/backlog?select=id&limit=1",
            headers=_sbh_count_svc(), timeout=10
        ).headers.get("content-range", "*/0").split("/")[-1])
        notify(f"KB Mining complete\nBatches: {batches_done}\nNew items: {total_new}\nTotal backlog: {final_count}")
        return {"ok": True, "batches_scanned": batches_done, "new_items": total_new,
                "total_backlog": final_count, "kb_count": kb_count}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_list_templates(limit: int = 20) -> dict:
    try:
        rows = sb_get("script_templates",
                      f"select=name,description,trigger_pattern,use_count,created_at"
                      f"&order=use_count.desc&limit={limit}",
                      svc=True)
        return {"ok": True, "templates": rows, "count": len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_run_template(name: str, params: str = "") -> dict:
    try:
        rows = sb_get("script_templates",
                      f"select=*&name=eq.{name}&limit=1", svc=True)
        if not rows:
            return {"ok": False, "error": f"Template '{name}' not found"}
        tpl = rows[0]
        code = tpl.get("code", "")
        if params:
            try:
                p = json.loads(params)
                for k, v in p.items():
                    code = code.replace(f"{{{k}}}", str(v))
            except Exception:
                pass
        sb_patch("script_templates", f"name=eq.{name}",
                 {"use_count": (tpl.get("use_count") or 0) + 1})
        return {
            "ok": True, "name": name,
            "description": tpl.get("description", ""),
            "trigger_pattern": tpl.get("trigger_pattern", ""),
            "code": code,
            "instruction": "Execute this code via gh_search_replace or direct MCP tool calls.",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_redeploy(reason: str = "") -> dict:
    """Trigger CORE redeploy on Oracle VM: git pull latest + restart service.
    Calls /deploy-webhook on the VM. Replaces old Railway empty-commit approach.
    reason: optional description logged to Telegram and Supabase."""
    try:
        preflight = t_external_service_preflight("github,telegram")
        if not preflight.get("ok"):
            return {"ok": False, "error": "deploy preflight failed", "preflight": preflight}
        r = httpx.post(
            f"{BASE_URL}/deploy-webhook",
            headers={"X-MCP-Secret": MCP_SECRET, "Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        notify(f"\U0001f680 CORE redeploy triggered\nReason: {reason or 'manual trigger'}\nHost: oracle_vm")
        try:
            t_changelog_add(
                version=datetime.utcnow().strftime("v%Y%m%d"),
                component="deploy",
                summary=f"Oracle VM redeploy triggered. Reason: {reason or 'manual trigger'}",
                before="deploy state pending",
                after="deploy webhook accepted and restart scheduled",
                change_type="config",
            )
        except Exception as _changelog_e:
            print(f"[CHANGELOG] deploy log error: {_changelog_e}")
        import threading as _t
        def _post_deploy_sync():
            import time as _time
            _time.sleep(15)
            try:
                t_sync_system_map(trigger="post_deploy", notify_on_changes="true")
            except Exception as _se:
                print(f"[SMAP] post-deploy sync error: {_se}")
        _t.Thread(target=_post_deploy_sync, daemon=True).start()
        return {"ok": True, "reason": reason, "status": "deploy_started", "host": "oracle_vm"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_logs(limit: str = "10", keyword: str = "") -> dict:
    """Fetch recent deploy history from GitHub commit log. NOTE: this is commit history, not Railway stdout.
    For live process logs, check Railway dashboard. Default limit lowered to 10 to reduce N+1 API calls."""
    try:
        lim = min(int(limit) if limit else 10, 50)
        h = _ghh()
        r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits?per_page={lim}", headers=h, timeout=10)
        r.raise_for_status()
        commits = r.json()
        logs = []
        kw = keyword.strip().lower() if keyword else ""
        for commit in commits[:lim]:
            sha = commit["sha"]
            msg = commit.get("commit", {}).get("message", "")[:80]
            ts  = commit.get("commit", {}).get("committer", {}).get("date", "")[:19]
            if kw and kw not in msg.lower():
                continue
            sr = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{sha}/statuses", headers=h, timeout=8)
            statuses = sr.json() if sr.status_code == 200 else []
            railway = [s for s in statuses if "railway" in s.get("context","").lower() or "railway" in s.get("description","").lower()]
            st = railway[0] if railway else {}
            logs.append({"ts": ts, "sha": sha[:10], "message": msg,
                         "deploy": st.get("state", "no_status"), "detail": st.get("description", "")})
        latest = logs[0] if logs else {}
        return {"ok": True, "count": len(logs), "keyword": kw or "(none)",
                "latest": latest, "logs": logs,
                "note": "For live stdout logs, check Railway dashboard"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_changelog_add(version: str = "", component: str = "", summary: str = "",
                    before: str = "", after: str = "", change_type: str = "upgrade") -> dict:
    """Log a completed change to the changelog table + Telegram notify."""
    try:
        preflight = t_external_service_preflight("supabase,telegram")
        if "supabase" in (preflight.get("blocked") or []):
            return {"ok": False, "error": "changelog preflight failed", "preflight": preflight}
        ts = datetime.utcnow().isoformat()
        ver = version.strip() or datetime.utcnow().strftime("v%Y%m%d")
        comp = component.strip() or "general"
        ctype = change_type.strip() or "upgrade"
        title = summary.strip()[:120]
        desc = summary.strip()[:500]
        before_state = before.strip()[:300]
        after_state = after.strip()[:300]
        # A.3: dedup guard -- skip if identical title+component already logged today
        today = datetime.utcnow().strftime("%Y-%m-%d")
        _title = summary.strip()[:40].replace("'", "")
        try:
            existing = sb_get("changelog",
                f"component=eq.{comp}&title=ilike.*{_title}*&created_at=gte.{today}&select=id,version,change_type,component,title,description,before_state,after_state,triggered_by,created_at&limit=1",
                svc=True) or []
            if existing:
                verification = _changelog_verification_packet(
                    version=ver,
                    component=comp,
                    summary=title,
                    before=before_state,
                    after=after_state,
                    change_type=ctype,
                )
                return {
                    "ok": True,
                    "action": "skipped_duplicate",
                    "version": ver,
                    "component": comp,
                    "hint": "identical changelog entry already logged today",
                    "verified": bool(verification.get("ok") and not verification.get("blocked")),
                    "verification_packet": verification,
                }
        except Exception:
            pass  # dedup failure is non-fatal
        ok = sb_post("changelog", {
            "version":      ver,
            "change_type":  ctype,
            "component":    comp,
            "title":        title,
            "description":  desc,
            "before_state": before_state,
            "after_state":  after_state,
            "triggered_by": "claude_desktop",
            "created_at":   ts,
        })
        verification = _changelog_verification_packet(
            version=ver,
            component=comp,
            summary=title,
            before=before_state,
            after=after_state,
            change_type=ctype,
        )
        source_packet = _changelog_source_packet(limit=5)
        if ok:
            notify(f"CHANGELOG [{ver}] {comp}\n{title[:200]}")
        return {
            "ok": ok,
            "action": "logged" if ok else "insert_failed",
            "version": ver,
            "component": comp,
            "logged_at": ts,
            "verified": bool(verification.get("ok") and not verification.get("blocked")),
            "verification_packet": verification,
            "source_packet": source_packet,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _changelog_verification_packet(
    version: str = "",
    component: str = "",
    summary: str = "",
    before: str = "",
    after: str = "",
    change_type: str = "upgrade",
) -> dict:
    """Verify a changelog row exists and matches the canonical write contract."""
    try:
        ver = version.strip()
        comp = component.strip() or "general"
        title = summary.strip()[:120]
        desc = summary.strip()[:500]
        before_state = before.strip()[:300]
        after_state = after.strip()[:300]
        ctype = change_type.strip() or "upgrade"
        rows = sb_get(
            "changelog",
            f"select=id,version,change_type,component,title,description,before_state,after_state,triggered_by,created_at&version=eq.{ver}&change_type=eq.{ctype}&order=id.desc&limit=20",
            svc=True,
        ) or []
        if not rows:
            return {
                "ok": True,
                "blocked": True,
                "verified": False,
                "verification_score": 0.0,
                "passed_checks": [],
                "failed_checks": ["changelog_row_missing"],
                "warnings": [],
                "summary": f"{ver}/{comp}: changelog row missing",
            }

        row = None
        failed = []
        warnings = []
        for candidate in rows:
            cand_title = (candidate.get("title") or "").strip()
            cand_desc = (candidate.get("description") or "").strip()
            cand_before = (candidate.get("before_state") or "").strip()
            cand_after = (candidate.get("after_state") or "").strip()
            cand_triggered_by = (candidate.get("triggered_by") or "").strip()
            if cand_title == title and cand_desc == desc:
                row = candidate
                if before_state and cand_before != before_state:
                    warnings.append("before_state_drift")
                if after_state and cand_after != after_state:
                    warnings.append("after_state_drift")
                if cand_triggered_by and cand_triggered_by != "claude_desktop":
                    warnings.append(f"triggered_by:{cand_triggered_by}")
                break

        if row is None:
            row = rows[0]
            warnings.append("exact_title_description_match_missing")

        passed = ["changelog_row_found"]
        if (row.get("version") or "") == ver:
            passed.append("version_match")
        else:
            failed.append("version_mismatch")
        if (row.get("component") or "") == comp:
            passed.append("component_match")
        else:
            failed.append("component_mismatch")
        if (row.get("change_type") or "") == ctype:
            passed.append("change_type_match")
        else:
            failed.append("change_type_mismatch")
        if (row.get("title") or "").strip() == title:
            passed.append("title_match")
        else:
            failed.append("title_mismatch")
        if (row.get("description") or "").strip() == desc:
            passed.append("description_match")
        else:
            failed.append("description_mismatch")
        if before_state and (row.get("before_state") or "").strip() == before_state:
            passed.append("before_state_match")
        elif before_state:
            warnings.append("before_state_mismatch")
        if after_state and (row.get("after_state") or "").strip() == after_state:
            passed.append("after_state_match")
        elif after_state:
            warnings.append("after_state_mismatch")
        if (row.get("triggered_by") or "").strip() == "claude_desktop":
            passed.append("triggered_by_match")
        else:
            failed.append("triggered_by_mismatch")

        verified = len(failed) == 0
        score = 1.0
        score -= 0.35 if failed else 0.0
        score -= 0.05 * len(warnings)
        score = max(0.0, round(score, 2))
        blocked = not verified or score < 0.8

        return {
            "ok": True,
            "blocked": blocked,
            "verified": verified,
            "verification_score": score,
            "passed_checks": passed,
            "failed_checks": failed,
            "warnings": warnings,
            "row": {
                "id": row.get("id"),
                "version": row.get("version"),
                "change_type": row.get("change_type"),
                "component": row.get("component"),
                "title": row.get("title"),
                "triggered_by": row.get("triggered_by"),
                "created_at": row.get("created_at"),
            },
            "summary": (
                f"{ver}/{comp}: {'verified' if verified else 'unverified'} "
                f"(warnings={len(warnings)}, failed={len(failed)})"
            ),
        }
    except Exception as exc:
        return {
            "ok": True,
            "blocked": True,
            "verified": False,
            "verification_score": 0.0,
            "passed_checks": [],
            "failed_checks": ["changelog_verification_error"],
            "warnings": [str(exc)],
            "summary": f"changelog verification error: {exc}",
        }


def _changelog_source_packet(limit: int = 5) -> dict:
    """Collect supporting source evidence for changelog context."""
    try:
        limit = max(1, int(limit))
        sessions = sb_get(
            "sessions",
            f"select=summary,actions,interface,created_at&order=created_at.desc&limit={limit}",
            svc=True,
        ) or []
        hot_reflections = sb_get(
            "hot_reflections",
            "select=domain,task_summary,reflection_text,new_patterns,new_mistakes,gaps_identified,source,created_at,processed_by_cold"
            f"&order=created_at.desc&limit={limit}",
            svc=True,
        ) or []
        mistakes = sb_get(
            "mistakes",
            "select=domain,what_failed,root_cause,how_to_avoid,severity,created_at"
            f"&order=created_at.desc&limit={limit}",
            svc=True,
        ) or []
        knowledge = sb_get(
            "knowledge_base",
            "select=domain,topic,source_type,source_ref,created_at"
            f"&order=created_at.desc&limit={limit}",
            svc=True,
        ) or []

        def _line(prefix: str, row: dict, keys: list[str]) -> str:
            parts = []
            for key in keys:
                val = row.get(key)
                if val:
                    parts.append(str(val))
            return f"- {prefix}: " + " | ".join(parts[:4]) if parts else f"- {prefix}: <empty>"

        lines = []
        for r in sessions[:limit]:
            lines.append(_line("session", r, ["interface", "summary", "created_at"]))
        for r in hot_reflections[:limit]:
            lines.append(_line("hot_reflection", r, ["domain", "task_summary", "created_at"]))
        for r in mistakes[:limit]:
            lines.append(_line("mistake", r, ["domain", "what_failed", "created_at"]))
        for r in knowledge[:limit]:
            lines.append(_line("knowledge", r, ["domain", "topic", "source_type", "created_at"]))
        counts = {
            "sessions": len(sessions),
            "hot_reflections": len(hot_reflections),
            "mistakes": len(mistakes),
            "knowledge_base": len(knowledge),
        }
        passed = ["source_packet_built"]
        failed = []
        warnings = []
        for key, value in counts.items():
            if value > 0:
                passed.append(f"{key}_covered")
            else:
                failed.append(f"{key}_missing")
        if counts["sessions"] < 2:
            warnings.append("sessions_low_sample")
        if counts["hot_reflections"] < 2:
            warnings.append("hot_reflections_low_sample")
        if counts["mistakes"] < 2:
            warnings.append("mistakes_low_sample")
        if counts["knowledge_base"] < 2:
            warnings.append("knowledge_base_low_sample")
        verified = len(failed) == 0
        score = 1.0
        score -= 0.20 if failed else 0.0
        score -= 0.05 * len(warnings)
        score = max(0.0, round(score, 2))
        blocked = not verified or score < 0.8
        return {
            "available": True,
            "counts": counts,
            "rows": {
                "sessions": sessions,
                "hot_reflections": hot_reflections,
                "mistakes": mistakes,
                "knowledge_base": knowledge,
            },
            "text": "\n".join(lines) if lines else "None yet.",
            "sources": ["sessions", "hot_reflections", "mistakes", "knowledge_base"],
            "error": "",
            "verified": verified,
            "blocked": blocked,
            "verification_score": score,
            "passed_checks": passed,
            "failed_checks": failed,
            "warnings": warnings,
            "summary": (
                f"source_packet: {'verified' if verified else 'unverified'} "
                f"(warnings={len(warnings)}, failed={len(failed)})"
            ),
        }
    except Exception as exc:
        return {
            "available": False,
            "counts": {"sessions": 0, "hot_reflections": 0, "mistakes": 0, "knowledge_base": 0},
            "rows": {"sessions": [], "hot_reflections": [], "mistakes": [], "knowledge_base": []},
            "text": "Unavailable.",
            "sources": ["sessions", "hot_reflections", "mistakes", "knowledge_base"],
            "error": str(exc),
            "verified": False,
            "blocked": True,
            "verification_score": 0.0,
            "passed_checks": [],
            "failed_checks": ["source_packet_error"],
            "warnings": [str(exc)],
            "summary": f"source packet error: {exc}",
        }


def t_changelog_verification_packet(
    version: str = "",
    component: str = "",
    summary: str = "",
    before: str = "",
    after: str = "",
    change_type: str = "upgrade",
) -> dict:
    """Public wrapper for canonical changelog verification."""
    return _changelog_verification_packet(
        version=version,
        component=component,
        summary=summary,
        before=before,
        after=after,
        change_type=change_type,
    )


def t_changelog_source_packet(limit: int = 5) -> dict:
    """Return supporting source evidence for changelog context."""
    return _changelog_source_packet(limit=limit)


def _normalize_changelog_row(row: dict) -> dict:
    """Normalize canonical and legacy changelog rows into one safe shape."""
    row = row or {}
    canonical = {
        "version": str(row.get("version") or "").strip(),
        "change_type": str(row.get("change_type") or "").strip(),
        "component": str(row.get("component") or "").strip(),
        "title": str(row.get("title") or "").strip(),
        "description": str(row.get("description") or "").strip(),
        "before_state": str(row.get("before_state") or "").strip(),
        "after_state": str(row.get("after_state") or "").strip(),
        "triggered_by": str(row.get("triggered_by") or "").strip(),
        "created_at": str(row.get("created_at") or "").strip(),
    }
    legacy = {
        "summary": str(row.get("summary") or "").strip(),
        "category": str(row.get("category") or "").strip(),
    }
    missing_fields = [name for name, value in canonical.items() if not value]
    # Fill display fields from legacy columns when canonical data is absent.
    if not canonical["title"]:
        canonical["title"] = legacy["summary"] or "Untitled changelog entry"
    if not canonical["description"]:
        canonical["description"] = legacy["summary"] or "No description provided."
    if not canonical["change_type"]:
        canonical["change_type"] = legacy["category"] or "unknown"
    if not canonical["component"]:
        canonical["component"] = "general"
    if not canonical["version"]:
        canonical["version"] = "?"
    display_bits = [
        canonical["version"],
        canonical["change_type"],
        canonical["component"],
        canonical["title"],
    ]
    display_line = " | ".join(bit for bit in display_bits if bit and bit != "?")
    if not display_line:
        display_line = canonical["description"] or legacy["summary"] or "Untitled changelog entry"
    completeness = round((len(canonical) - len(missing_fields)) / max(1, len(canonical)), 2)
    normalized = dict(row)
    normalized.update({
        "_missing_fields": missing_fields,
        "_row_completeness": completeness,
        "_display_line": display_line,
        "_legacy_summary": legacy["summary"],
        "_legacy_category": legacy["category"],
    })
    return normalized


@dataclass
class ChangelogPacket:
    latest_version: str = ""
    latest_component: str = ""
    latest_change_type: str = ""
    latest_title: str = ""
    latest_created_at: str = ""
    total_rows: int = 0
    today_rows: int = 0
    verified_rows: int = 0
    missing_triggered_by_rows: int = 0
    missing_fields_rows: int = 0
    missing_fields_total: int = 0
    row_completeness: float = 0.0
    tracking_state: str = "unknown"
    stalled: bool = False
    tracking_score: float = 0.0
    passed_checks: list[str] | None = None
    failed_checks: list[str] | None = None
    warnings: list[str] | None = None
    summary: str = ""
    rows: list[dict] | None = None
    normalized_rows: list[dict] | None = None
    source_packet: dict | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["passed_checks"] = list(self.passed_checks or [])
        data["failed_checks"] = list(self.failed_checks or [])
        data["warnings"] = list(self.warnings or [])
        data["rows"] = list(self.rows or [])
        data["normalized_rows"] = list(self.normalized_rows or [])
        return data


def build_changelog_packet(limit: int = 10) -> dict:
    try:
        lim = max(1, min(int(limit or 10), 25))
    except Exception:
        lim = 10

    try:
        rows = sb_get(
            "changelog",
            f"select=id,version,change_type,component,title,description,before_state,after_state,triggered_by,created_at&order=created_at.desc&limit={lim}",
            svc=True,
        ) or []
        today = datetime.utcnow().date().isoformat()
        today_rows = [r for r in rows if str(r.get("created_at") or "").startswith(today)]
        normalized_rows = [_normalize_changelog_row(r) for r in rows]
        verified_rows = 0
        missing_triggered_by_rows = 0
        missing_fields_rows = 0
        missing_fields_total = 0
        total_completeness = 0.0
        latest = normalized_rows[0] if normalized_rows else {}
        passed = ["changelog_rows_loaded"]
        failed = []
        warnings = []
        for row in normalized_rows:
            missing_fields = row.get("_missing_fields") or []
            if missing_fields:
                missing_fields_rows += 1
                missing_fields_total += len(missing_fields)
                warnings.append(f"missing_fields:{row.get('id')}")
            total_completeness += float(row.get("_row_completeness") or 0.0)
            if row.get("triggered_by"):
                verified_rows += 1
            else:
                missing_triggered_by_rows += 1
                warnings.append(f"missing_triggered_by:{row.get('id')}")
        if rows:
            passed.append("latest_row_present")
            if latest.get("triggered_by"):
                passed.append("latest_triggered_by_present")
            else:
                warnings.append("latest_missing_triggered_by")
        else:
            failed.append("changelog_rows_missing")

        if len(rows) < 2:
            warnings.append("sample_low")

        avg_completeness = round(total_completeness / max(1, len(normalized_rows)), 2) if normalized_rows else 0.0
        tracking_state = "healthy" if rows and missing_triggered_by_rows == 0 and missing_fields_total == 0 else ("degraded" if rows else "empty")
        stalled = not rows
        score = 0.0
        if rows:
            score += 0.6
        if missing_triggered_by_rows == 0:
            score += 0.2
        if missing_fields_total == 0:
            score += 0.1
        if today_rows:
            score += 0.1
        if len(rows) >= 2:
            score += 0.1
        score = max(0.0, min(1.0, round(score, 2)))

        packet = ChangelogPacket(
            latest_version=str(latest.get("version") or ""),
            latest_component=str(latest.get("component") or ""),
            latest_change_type=str(latest.get("change_type") or ""),
            latest_title=str(latest.get("title") or ""),
            latest_created_at=str(latest.get("created_at") or ""),
            total_rows=len(rows),
            today_rows=len(today_rows),
            verified_rows=verified_rows,
            missing_triggered_by_rows=missing_triggered_by_rows,
            missing_fields_rows=missing_fields_rows,
            missing_fields_total=missing_fields_total,
            row_completeness=avg_completeness,
            tracking_state=tracking_state,
            stalled=stalled,
            tracking_score=score,
            passed_checks=passed,
            failed_checks=failed,
            warnings=warnings,
            summary=(
                f"CHANGELOG: {tracking_state}"
                f" | rows {len(rows)}"
                f" | today {len(today_rows)}"
                f" | missing_triggered_by {missing_triggered_by_rows}"
                f" | missing_fields {missing_fields_total}"
                f" | completeness {avg_completeness:.2f}"
            ),
            rows=rows,
            normalized_rows=normalized_rows,
            source_packet=_changelog_source_packet(limit=lim),
        )
        return {
            "ok": True,
            "tracking_state": tracking_state,
            "stalled": stalled,
            "tracking_score": score,
            "packet": packet.to_dict(),
            "rows": rows,
            "normalized_rows": normalized_rows,
            "source_packet": packet.source_packet,
            "message": packet.summary,
        }
    except Exception as exc:
        return {
            "ok": False,
            "tracking_state": "error",
            "stalled": True,
            "tracking_score": 0.0,
            "error": str(exc),
            "failed_checks": ["changelog_packet_error"],
            "warnings": [str(exc)],
            "message": f"changelog packet error: {exc}",
        }


def t_changelog_tracking_packet(limit: int = 10) -> dict:
    return build_changelog_packet(limit=limit)


def t_changelog_state_packet(limit: int = 10, strict: str = "false") -> dict:
    """Return changelog tracking plus verification as a single state packet."""
    try:
        tracking = t_changelog_tracking_packet(limit=limit)
        if not tracking.get("ok"):
            return tracking
        packet = tracking.get("packet") or {}
        rows = packet.get("rows") or tracking.get("rows") or []
        latest = rows[0] if rows else {}
        verification = {}
        if latest:
            verification = _changelog_verification_packet(
                version=str(latest.get("version") or ""),
                component=str(latest.get("component") or ""),
                summary=str(latest.get("title") or ""),
                before=str(latest.get("before_state") or ""),
                after=str(latest.get("after_state") or ""),
                change_type=str(latest.get("change_type") or "upgrade"),
            )
        fallback_context = {}
        availability = "available" if rows and tracking.get("tracking_state") == "healthy" else "degraded"
        if availability != "available":
            try:
                fallback_context["state_packet"] = t_state_packet(strict="false")
            except Exception as exc:
                fallback_context["state_packet_error"] = str(exc)
            try:
                fallback_context["recent_mistakes"] = t_get_mistakes(limit=3)
            except Exception as exc:
                fallback_context["recent_mistakes_error"] = str(exc)
            fallback_context["source_packet"] = t_changelog_source_packet(limit=max(3, int(limit or 10)))
            fallback_context["fallback_note"] = "changelog unavailable; using state, mistakes, and source packet as fallback evidence"
        return {
            "ok": True,
            "strict": str(strict).strip().lower() in ("true", "1", "yes"),
            "availability": availability,
            "tracking": tracking,
            "verification": verification,
            "rows": rows,
            "source_packet": tracking.get("source_packet") or packet.get("source_packet") or {},
            "fallback_context": fallback_context,
            "summary": (
                f"changelog_state={tracking.get('tracking_state') or 'unknown'} | "
                f"availability={availability} | rows={len(rows)} | "
                f"verified={bool((verification or {}).get('verified', False))}"
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "summary": f"changelog_state error: {e}"}


def t_knowledge_state_packet(
    domain: str = "",
    topic: str = "",
    instruction: str = "",
    content: str = "",
    confidence: str = "medium",
    source_type: str = "",
    source_ref: str = "",
    query: str = "",
    limit: str = "5",
) -> dict:
    """Return a canonical KB state packet with search + entry verification."""
    try:
        lim = max(1, min(int(limit or 5), 20))
    except Exception:
        lim = 5
    try:
        if not domain and not topic and not query:
            return {"ok": False, "error": "domain, topic, or query required"}
        search_query = query or topic or instruction or content
        search_results = t_search_kb(query=search_query, domain=domain, limit=lim) if search_query else {"ok": True, "count": 0, "results": []}
        verification = None
        if domain and topic:
            verification = t_kb_entry_packet(
                domain=domain,
                topic=topic,
                instruction=instruction or "",
                content=content or "",
                confidence=confidence,
                source_type=source_type,
                source_ref=source_ref,
            )
        state_rows = search_results if isinstance(search_results, list) else []
        search_count = len(state_rows)
        freshness_keys = ["last_research_ts", "last_real_signal_ts", "last_public_source_ts"]
        freshness_values = {}
        for key in freshness_keys:
            try:
                value = _latest_state_update_value(key).get("value")
            except Exception:
                value = None
            if value not in (None, "", {}, []):
                freshness_values[key] = value
        return {
            "ok": True,
            "domain": domain or "",
            "topic": topic or "",
            "query": search_query or "",
            "search": search_results,
            "verification": verification or {},
            "freshness": freshness_values,
            "rows": state_rows,
            "summary": (
                f"knowledge_state: rows={search_count} | "
                f"verified={bool((verification or {}).get('verified', False))} | "
                f"freshness={len(freshness_values)}"
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "summary": f"knowledge_state error: {e}"}


def t_architecture_review_packet(
    module_name: str = "",
    architecture_name: str = "",
    task_context: str = "",
    goal: str = "",
    evidence: str = "",
    domain: str = "general",
    state_hint: str = "",
    knowledge_domain: str = "",
    knowledge_topic: str = "",
    source_type: str = "",
    source_ref: str = "",
    learning_rate: str = "0.1",
) -> dict:
    """Bundle module readiness with KB freshness for architecture decisions."""
    try:
        module_name = (module_name or "").strip()
        architecture_name = (architecture_name or "").strip()
        task_context = (task_context or "").strip()
        goal = (goal or "").strip()
        evidence = (evidence or "").strip()
        arch_blob = " | ".join(part for part in (architecture_name, module_name, task_context, goal, evidence) if part)

        module_assessment = t_module_assessment_packet(
            module_name=module_name or architecture_name,
            module_description=architecture_name or module_name,
            task_context=task_context or goal,
            goal=goal or architecture_name or module_name,
            evidence=evidence,
            domain=domain,
            state_hint=state_hint,
            learning_rate=learning_rate,
        )
        kb_domain = knowledge_domain or domain or "general"
        kb_topic = knowledge_topic or architecture_name or module_name or "architecture_review"
        kb_state = t_knowledge_state_packet(
            domain=kb_domain,
            topic=kb_topic,
            instruction=architecture_name or module_name or goal,
            content=evidence or task_context,
            confidence="medium",
            source_type=source_type,
            source_ref=source_ref,
            query=architecture_name or module_name or goal or evidence or "",
            limit="5",
        )

        module_ready = bool(module_assessment.get("ok") and not module_assessment.get("blocked"))
        kb_verified = bool((kb_state.get("verification") or {}).get("verified", False))
        kb_rows = len((kb_state.get("rows") or []))
        freshness = kb_state.get("freshness") or {}
        freshness_score = min(1.0, round(0.35 + 0.15 * len(freshness), 2))
        readiness_score = round(
            min(1.0, max(0.0, (
                float(module_assessment.get("readiness_score") or 0.0) * 0.7
                + freshness_score * 0.3
            ))),
            2,
        )
        recommendation = "proceed" if readiness_score >= 0.75 and kb_verified else ("verify_more" if readiness_score >= 0.6 else "split_or_delay")
        warnings = list(module_assessment.get("warnings") or [])
        if not kb_verified:
            warnings.append("kb_unverified")
        if not freshness:
            warnings.append("kb_freshness_missing")
        summary = (
            f"architecture_review={'ok' if module_ready and kb_verified else 'review'} | "
            f"readiness={readiness_score:.2f} | kb_rows={kb_rows} | freshness={len(freshness)} | "
            f"recommendation={recommendation}"
        )
        return {
            "ok": True,
            "architecture_name": architecture_name or module_name,
            "module_name": module_name,
            "task_context": task_context,
            "goal": goal,
            "module_assessment": module_assessment,
            "knowledge_state": kb_state,
            "freshness": freshness,
            "readiness_score": readiness_score,
            "module_ready": module_ready,
            "kb_verified": kb_verified,
            "kb_rows": kb_rows,
            "recommendation": recommendation,
            "warnings": warnings,
            "summary": summary,
            "arch_blob": arch_blob,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "blocked": True}


@dataclass
class MistakePacket:
    total_rows: int = 0
    today_rows: int = 0
    recent_rows: list[dict] | None = None
    domain_counts: dict | None = None
    severity_counts: dict | None = None
    missing_context_rows: int = 0
    missing_root_cause_rows: int = 0
    missing_how_to_avoid_rows: int = 0
    tracking_state: str = "unknown"
    stalled: bool = False
    tracking_score: float = 0.0
    passed_checks: list[str] | None = None
    failed_checks: list[str] | None = None
    warnings: list[str] | None = None
    summary: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["recent_rows"] = list(self.recent_rows or [])
        data["domain_counts"] = dict(self.domain_counts or {})
        data["severity_counts"] = dict(self.severity_counts or {})
        data["passed_checks"] = list(self.passed_checks or [])
        data["failed_checks"] = list(self.failed_checks or [])
        data["warnings"] = list(self.warnings or [])
        return data


def build_mistake_tracking_packet(limit: int = 10) -> dict:
    try:
        lim = max(1, min(int(limit or 10), 25))
    except Exception:
        lim = 10

    try:
        rows = sb_get(
            "mistakes",
            f"select=id,domain,context,what_failed,correct_approach,root_cause,how_to_avoid,severity,created_at&order=created_at.desc&limit={lim}",
            svc=True,
        ) or []
        today = datetime.utcnow().date().isoformat()
        today_rows = [r for r in rows if str(r.get("created_at") or "").startswith(today)]
        domain_counts = Counter((r.get("domain") or "general") for r in rows)
        severity_counts = Counter((r.get("severity") or "unknown") for r in rows)
        missing_context_rows = sum(1 for r in rows if not (r.get("context") or "").strip())
        missing_root_cause_rows = sum(1 for r in rows if not (r.get("root_cause") or "").strip())
        missing_how_to_avoid_rows = sum(1 for r in rows if not (r.get("how_to_avoid") or "").strip())
        passed = ["mistake_rows_loaded"]
        failed = []
        warnings = []
        if rows:
            passed.append("latest_row_present")
        else:
            failed.append("mistake_rows_missing")
        if missing_context_rows:
            warnings.append(f"missing_context:{missing_context_rows}")
        if missing_root_cause_rows:
            warnings.append(f"missing_root_cause:{missing_root_cause_rows}")
        if missing_how_to_avoid_rows:
            warnings.append(f"missing_how_to_avoid:{missing_how_to_avoid_rows}")
        if len(rows) < 2:
            warnings.append("sample_low")
        tracking_state = "healthy" if rows and not missing_context_rows else ("degraded" if rows else "empty")
        stalled = not rows
        score = 0.0
        if rows:
            score += 0.55
        if not missing_context_rows:
            score += 0.15
        if not missing_root_cause_rows:
            score += 0.15
        if not missing_how_to_avoid_rows:
            score += 0.1
        if len(rows) >= 2:
            score += 0.05
        if today_rows:
            score += 0.05
        score = max(0.0, min(1.0, round(score, 2)))

        packet = MistakePacket(
            total_rows=len(rows),
            today_rows=len(today_rows),
            recent_rows=rows,
            domain_counts=dict(domain_counts.most_common()),
            severity_counts=dict(severity_counts.most_common()),
            missing_context_rows=missing_context_rows,
            missing_root_cause_rows=missing_root_cause_rows,
            missing_how_to_avoid_rows=missing_how_to_avoid_rows,
            tracking_state=tracking_state,
            stalled=stalled,
            tracking_score=score,
            passed_checks=passed,
            failed_checks=failed,
            warnings=warnings,
            summary=(
                f"MISTAKES: {tracking_state}"
                f" | rows {len(rows)}"
                f" | today {len(today_rows)}"
                f" | missing_context {missing_context_rows}"
            ),
        )
        return {
            "ok": True,
            "tracking_state": tracking_state,
            "stalled": stalled,
            "tracking_score": score,
            "packet": packet.to_dict(),
            "rows": rows,
            "message": packet.summary,
        }
    except Exception as exc:
        return {
            "ok": False,
            "tracking_state": "error",
            "stalled": True,
            "tracking_score": 0.0,
            "error": str(exc),
            "failed_checks": ["mistake_packet_error"],
            "warnings": [str(exc)],
            "message": f"mistake packet error: {exc}",
        }


def t_mistake_tracking_packet(limit: int = 10) -> dict:
    return build_mistake_tracking_packet(limit=limit)


def t_bulk_apply(executor_override: str = "claude_desktop", dry_run: bool = False):
    """Apply all pending evolution_queue items."""
    if isinstance(dry_run, str):
        dry_run = dry_run.strip().lower() not in ("false", "0", "no", "")
    try:
        rows = sb_get("evolution_queue",
                      # H.1: slim select -- only fetch needed columns, not fat diff_content
                      f"select={_sel_force('evolution_queue', ['id','change_type','change_summary','diff_content','status','source'])}&status=in.(pending,pending_desktop)&order=id.asc",
                      svc=True)
        if not rows:
            return {"ok": True, "message": "No pending evolutions", "applied": [], "total": 0}
        results = []
        for evo in rows:
            eid   = evo["id"]
            ctype = evo.get("change_type", "knowledge")
            summary = evo.get("change_summary", "")
            try:
                meta = json.loads(evo.get("diff_content") or "{}")
            except Exception:
                meta = {}
            btype    = meta.get("backlog_type", "")
            title    = meta.get("title", summary[:80])
            desc     = meta.get("description", summary)
            domain   = meta.get("domain", "general")
            original_exec = meta.get("executor", "auto")
            effective = executor_override if executor_override != "auto" else original_exec
            if dry_run:
                results.append({"id": eid, "title": title, "btype": btype,
                                 "original_executor": original_exec, "would_use": effective,
                                 "action": "dry_run - not applied"})
                continue
            if effective == "claude_desktop" or executor_override == "claude_desktop":
                if ctype == "knowledge" or btype == "new_kb":
                    ok = bool(sb_post("knowledge_base", {
                        "domain": domain, "topic": title, "content": desc,
                        "confidence": "medium", "tags": ["bulk_apply", "claude_desktop"],
                        "source": "bulk_apply",
                    }))
                    note = f"[desktop] KB entry added: {title}"
                elif btype in ("logic_improvement", "performance", "missing_data"):
                    ok = bool(sb_post("task_queue", {
                        "type": "improvement",
                        "payload": json.dumps({"title": title, "desc": desc, "domain": domain}),
                        "status": "pending", "priority": 5, "source": "bulk_apply",
                    }))
                    note = f"[desktop] Queued for execution: {title}"
                elif btype in ("new_tool", "telegram_command"):
                    ok = bool(sb_post("knowledge_base", {
                        "domain": "pending_impl", "topic": f"[TODO] {title}",
                        "content": f"Type: {btype}\n{desc}",
                        "confidence": "low", "tags": ["todo", "new_tool", "claude_desktop"],
                        "source": "bulk_apply",
                    }))
                    note = f"[desktop] Logged as TODO: {title}"
                else:
                    ok = bool(sb_post("knowledge_base", {
                        "domain": domain, "topic": title, "content": desc,
                        "confidence": "medium", "tags": ["bulk_apply"], "source": "bulk_apply",
                    }))
                    note = f"[desktop] KB fallback: {title}"
                if ok:
                    sb_patch("evolution_queue", f"id=eq.{eid}",
                             {"status": "applied", "applied_at": datetime.utcnow().isoformat()})
                results.append({"id": eid, "title": title, "ok": ok, "note": note})
            else:
                r = apply_evolution(eid)
                results.append({"id": eid, "title": title, "ok": r.get("ok"), "note": r.get("note", "")})
        applied = [r for r in results if r.get("ok")]
        failed  = [r for r in results if not r.get("ok") and not r.get("action")]
        notify(f"Bulk apply done\nApplied: {len(applied)} | Failed: {len(failed)} | Total: {len(results)}\nExecutor: {executor_override}")
        # BACKLOG.md deleted in Task 1.8 â€” backlog lives in Supabase only
        # Slim results to prevent h11 Content-Length overflow on large batches
        slim_results = [{"id": r.get("id"), "ok": r.get("ok"), "note": str(r.get("note", ""))[:120]} for r in results]
        return {"ok": True, "applied": len(applied), "failed": len(failed), "results": slim_results}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- Railway GraphQL constants
_RAILWAY_TOKEN   = os.environ.get("RAILWAY_TOKEN", "")
_RAILWAY_GQL     = "https://backboard.railway.app/graphql/v2"
_RAILWAY_PROJECT = "b6ead639-5fa2-4637-b8a5-d403ce6dac82"
_RAILWAY_SERVICE = "5c43b876-47ca-4125-834d-92c268d89b33"
_RAILWAY_ENV     = "002425ee-b1a3-4ec2-b668-f24ae5e12411"

def _railway_gql(query: str, variables: dict = None) -> dict:
    """Execute a Railway GraphQL query. Returns response data dict."""
    tok = _RAILWAY_TOKEN or os.environ.get("RAILWAY_TOKEN", "")
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    body = {"query": query}
    if variables:
        body["variables"] = variables
    r = httpx.post(_RAILWAY_GQL, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise Exception(data["errors"][0]["message"])
    return data.get("data", {})

def _railway_latest_deployment() -> dict:
    """Get the latest deployment for the CORE service.
    NOTE: meta is a SCALAR (JSON blob) on Railway -- never select subfields.
    Access as node['meta']['commitHash'] etc after retrieval.
    projectId is REQUIRED in input -- serviceId alone returns empty.
    """
    q = """
    query($projectId: String!, $serviceId: String!) {
        deployments(first: 1, input: { projectId: $projectId, serviceId: $serviceId }) {
            edges { node { id status createdAt environmentId staticUrl meta } }
        }
    }"""
    data = _railway_gql(q, {"projectId": _RAILWAY_PROJECT, "serviceId": _RAILWAY_SERVICE})
    edges = data.get("deployments", {}).get("edges", [])
    if not edges:
        return {}
    node = edges[0]["node"]
    # meta is a scalar JSON blob -- unpack it into the node for easy access
    meta = node.get("meta") or {}
    if isinstance(meta, dict):
        node["commitHash"] = (meta.get("commitHash") or "")[:12]
        node["commitMessage"] = meta.get("commitMessage") or ""
        node["commitAuthor"] = meta.get("commitAuthor") or ""
    return node

def _gh_commit_status(sha: str = "") -> dict:
    try:
        h = _ghh()
        if not sha:
            ref = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/git/ref/heads/main", headers=h, timeout=10)
            ref.raise_for_status()
            sha = ref.json()["object"]["sha"]
        r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{sha}/statuses", headers=h, timeout=10)
        r.raise_for_status()
        statuses = r.json()
        c = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{sha}", headers=h, timeout=10)
        c.raise_for_status()
        commit_msg = c.json().get("commit", {}).get("message", "")[:80]
        railway = [s for s in statuses if "railway" in s.get("context", "").lower() or "railway" in s.get("description", "").lower()]
        latest = railway[0] if railway else (statuses[0] if statuses else {})
        return {
            "sha": sha[:12], "commit_msg": commit_msg,
            "state": latest.get("state", "unknown"),
            "description": latest.get("description", "no status yet"),
            "updated_at": latest.get("updated_at", ""),
            "all_statuses": [{"context": s.get("context"), "state": s.get("state"), "description": s.get("description")} for s in statuses],
        }
    except Exception as e:
        return {"state": "error", "description": str(e)}


def t_deploy_status() -> dict:
    """VM deploy status: latest git commit on VM vs GitHub.
    Replaces old Railway deploy status — CORE now runs on Oracle VM."""
    try:
        vm_sha = subprocess.run(["git", "-C", "/home/ubuntu/core-agi", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        vm_msg = subprocess.run(["git", "-C", "/home/ubuntu/core-agi", "log", "-1", "--pretty=%s"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        h = _ghh()
        r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/main", headers=h, timeout=10)
        r.raise_for_status()
        gh = r.json()
        gh_sha = gh["sha"]
        synced = vm_sha == gh_sha
        return {"ok": True, "vm_sha": vm_sha[:12], "vm_msg": vm_msg,
                "github_sha": gh_sha[:12], "synced": synced,
                "status": "up_to_date" if synced else "behind"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_build_status() -> dict:
    """VM build/deploy status: checks if VM is running latest GitHub commit.
    Replaces old Railway build status — CORE now runs on Oracle VM."""
    return t_deploy_status()


def t_crash_report() -> dict:
    """Check if core-agi service has crashed or restarted recently on Oracle VM.
    Replaces old Railway crash loop detector."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", "core-agi", "--no-pager", "-n", "50",
             "--output=short", "--since", "1 hour ago"],
            capture_output=True, text=True, timeout=15
        )
        lines = result.stdout.splitlines()
        crashes = [l for l in lines if any(w in l.lower() for w in ["failed", "error", "crashed", "killed", "start request"])]
        status = subprocess.run(["systemctl", "is-active", "core-agi"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        return {"ok": True, "service_state": status, "crash_indicators": crashes[-10:],
                "crash_count": len(crashes), "source": "oracle_vm_journalctl"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_review_evolutions() -> dict:
    try:
        domain = os.environ.get("PUBLIC_DOMAIN", "core-agi.duckdns.org")
        url = f"https://{domain}/review"
        return {"ok": True, "url": url, "note": "Open URL in browser to review pending evolutions."}


    except Exception as e:
        return {"ok": False, "error": str(e)}
# -- Project Mode tools -------------------------------------------------------

def t_project_list() -> dict:
    """List all registered projects from Supabase."""
    try:
        rows = sb_get("projects", "select=project_id,name,status,last_indexed,folder_path&order=created_at.asc", svc=True) or []
        return {"ok": True, "projects": rows, "count": len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_get(project_ids: str = "") -> dict:
    """Load full context for one or more projects. project_ids = comma-separated slugs or list."""
    try:
        def _extract_id(p):
            if isinstance(p, dict):
                return str(p.get("id") or p.get("project_id") or next(iter(p.values()), "")).strip()
            return str(p).strip()
        if isinstance(project_ids, list):
            ids = [_extract_id(p) for p in project_ids if _extract_id(p)]
        else:
            ids = [p.strip() for p in str(project_ids).split(",") if p.strip()]
        if not ids:
            return {"ok": False, "error": "project_ids required"}
        results = []
        for pid in ids:
            # Try unconsumed prepared context first
            ctx_rows = sb_get("project_context",
                f"select=context_md,id&project_id=eq.{pid}&consumed=eq.false&order=prepared_at.desc&limit=1",
                svc=True) or []
            if ctx_rows:
                ctx = ctx_rows[0].get("context_md", "")
            else:
                # Fall back to top 30 KB entries
                kb = sb_get("knowledge_base",
                    f"select=topic,instruction,content&domain=eq.project%3A{pid}&order=updated_at.desc&limit=30",
                    svc=True) or []
                ctx = "\n\n".join([
                    f"### {r['topic']}\n" +
                    (f"**Directive:** {r['instruction']}\n" if r.get('instruction') else "") +
                    r.get('content', '')
                    for r in kb
                ])
            results.append({"project_id": pid, "context": ctx})
        return {"ok": True, "results": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_search(project_id: str = "", query: str = "") -> dict:
    """Search KB entries for a specific project. Server-side ilike on topic+content."""
    try:
        if not project_id or not query:
            return {"ok": False, "error": "project_id and query required"}
        domain = f"project:{project_id}"
        enc_q = query.replace("%", "%25")
        rows = sb_get("knowledge_base",
            f"select=topic,content,confidence&domain=eq.{domain}&or=(topic.ilike.*{enc_q}*,content.ilike.*{enc_q}*)&limit=30",
            svc=True) or []
        return {"ok": True, "project_id": project_id, "query": query, "hits": rows, "count": len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_context_check() -> dict:
    """Check for unconsumed prepared project contexts (Telegram-prepared, waiting for Desktop)."""
    try:
        rows = sb_get("project_context",
            "select=project_id,prepared_by,prepared_at&consumed=eq.false&order=prepared_at.desc",
            svc=True) or []
        return {"ok": True, "pending": rows, "count": len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_register(project_id: str = "", name: str = "", folder_path: str = "", index_path: str = "") -> dict:
    """Register a new project in Supabase."""
    try:
        if not project_id or not name:
            return {"ok": False, "error": "project_id and name required"}
        ok = sb_post_critical("projects", {
            "project_id": project_id,
            "name": name,
            "folder_path": folder_path,
            "index_path": index_path,
            "status": "active",
        })
        if ok:
            return {"ok": True, "project_id": project_id, "name": name}
        return {"ok": False, "error": "insert failed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_update_kb(project_id: str = "", topic: str = "", content: str = "", confidence: str = "high") -> dict:
    """Add or update a KB entry for a project. domain=project:{project_id}."""
    try:
        if not project_id or not topic or not content:
            return {"ok": False, "error": "project_id, topic, content required"}
        domain = f"project:{project_id}"
        canon_source_type, canon_source_ref = _kb_normalize_provenance("session", f"project:{project_id}")
        row = {
            "domain": domain,
            "topic": topic,
            "content": content,
            "confidence": confidence,
            "source": "mcp_session",
            "source_type": canon_source_type,
            "source_ref": canon_source_ref,
        }
        ok = sb_upsert("knowledge_base", row, on_conflict="domain,topic")
        verification = _kb_entry_verification_packet(
            domain=domain,
            topic=topic,
            content=content,
            confidence=confidence,
            source_type=canon_source_type,
            source_ref=canon_source_ref,
            require_exact_match=False,
        )
        return {
            "ok": bool(ok and verification.get("verified")),
            "domain": domain,
            "topic": topic,
            "verified": bool(verification.get("verified")),
            "verification_packet": verification,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_update_index(project_id: str = "", last_indexed: str = "") -> dict:
    """Update last_indexed timestamp for a project in Supabase."""
    try:
        if not project_id:
            return {"ok": False, "error": "project_id required"}
        ts = last_indexed or datetime.utcnow().isoformat()
        ok = sb_patch("projects", f"project_id=eq.{project_id}", {"last_indexed": ts})
        return {"ok": bool(ok), "project_id": project_id, "last_indexed": ts}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_read_image_content(content_b64: str = "", mime_type: str = "image/jpeg",
                         project_id: str = "", topic: str = "",
                         prompt: str = "", file_name: str = "") -> dict:
    """Extract text/data from an image using Claude vision (claude-haiku-4-5).
    content_b64: base64-encoded image bytes.
    mime_type: image/jpeg or image/png.
    project_id + topic: if provided, auto-saves extracted text to project KB.
    prompt: extraction instruction (default: extract all visible text, tables, lists, and data).
    Returns: {ok, extracted_text, tokens_used, kb_saved}
    """
    try:
        import base64 as _b64
        if not content_b64:
            return {"ok": False, "error": "content_b64 required"}
        _prompt = prompt or (
            "Extract ALL text visible in this image. Include: any text, numbers, dates, names, "
            "status fields, table contents, lists, annotations, and any other data. "
            "Preserve structure. Output plain text only."
        )
        ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
        if not ANTHROPIC_KEY:
            return {"ok": False, "error": "ANTHROPIC_API_KEY not set"}
        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 2048,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": content_b64
                    }},
                    {"type": "text", "text": _prompt}
                ]
            }]
        }
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=payload, timeout=60
        )
        r.raise_for_status()
        data = r.json()
        extracted = data["content"][0]["text"].strip()
        usage = data.get("usage", {})
        kb_saved = False
        if project_id and topic and extracted:
            label = file_name or "image"
            kb_content = f"[Extracted from image: {label}]\n{extracted}"
            ok = sb_upsert("knowledge_base",
                {"domain": f"project:{project_id}", "topic": topic,
                 "content": kb_content[:4000], "confidence": "high"},
                on_conflict="domain,topic")
            kb_saved = bool(ok)
        return {
            "ok": True,
            "extracted_text": extracted,
            "chars": len(extracted),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "kb_saved": kb_saved,
            "project_id": project_id,
            "topic": topic
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_read_pdf_content(content_b64: str = "", project_id: str = "",
                       topic: str = "", file_name: str = "",
                       max_chars: str = "8000") -> dict:
    """Extract text from a PDF using pdfminer.six.
    content_b64: base64-encoded PDF bytes.
    project_id + topic: if provided, auto-saves to project KB.
    max_chars: truncate extracted text to this length (default 8000).
    Returns: {ok, extracted_text, pages, chars, kb_saved}
    """
    try:
        import base64 as _b64, io
        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams
        if not content_b64:
            return {"ok": False, "error": "content_b64 required"}
        pdf_bytes = _b64.b64decode(content_b64)
        pdf_io = io.BytesIO(pdf_bytes)
        text_io = io.StringIO()
        extract_text_to_fp(pdf_io, text_io, laparams=LAParams(), output_type="text", codec=None)
        extracted = text_io.getvalue().strip()
        limit = int(max_chars) if max_chars else 8000
        truncated = len(extracted) > limit
        extracted_out = extracted[:limit]
        kb_saved = False
        if project_id and topic and extracted_out:
            label = file_name or "pdf"
            kb_content = f"[Extracted from PDF: {label}]\n{extracted_out}"
            ok = sb_upsert("knowledge_base",
                {"domain": f"project:{project_id}", "topic": topic,
                 "content": kb_content[:4000], "confidence": "high"},
                on_conflict="domain,topic")
            kb_saved = bool(ok)
        # Count pages roughly (\x0c = form feed = page break in pdfminer output)
        pages = extracted.count('\x0c') + 1
        return {
            "ok": True,
            "extracted_text": extracted_out,
            "chars": len(extracted_out),
            "pages": pages,
            "truncated": truncated,
            "kb_saved": kb_saved,
            "project_id": project_id,
            "topic": topic
        }
    except ImportError:
        return {"ok": False, "error": "pdfminer.six not installed -- add to requirements.txt and redeploy"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_prepare(project_ids: str = "") -> dict:
    """Railway-side: assemble context for project(s) and store in project_context for Desktop to consume."""
    try:
        def _extract_id(p):
            if isinstance(p, dict):
                return str(p.get("id") or p.get("project_id") or next(iter(p.values()), "")).strip()
            return str(p).strip()
        if isinstance(project_ids, list):
            ids = [_extract_id(p) for p in project_ids if _extract_id(p)]
        else:
            ids = [p.strip() for p in str(project_ids).split(",") if p.strip()]
        if not ids:
            return {"ok": False, "error": "project_ids required"}
        prepared = []
        for pid in ids:
            proj_rows = sb_get("projects", f"select=name&project_id=eq.{pid}&limit=1", svc=True) or []
            if not proj_rows:
                continue
            name = proj_rows[0].get("name", pid)
            kb = sb_get("knowledge_base",
                f"select=topic,instruction,content&domain=eq.project%3A{pid}&order=updated_at.desc&limit=30",
                svc=True) or []
            context_md = f"# Project Context: {name}\n\n"
            context_md += "\n\n".join([
                f"### {r['topic']}\n" +
                (f"**Directive:** {r['instruction']}\n" if r.get('instruction') else "") +
                r.get('content', '')
                for r in kb
            ])
            sb_post_critical("project_context", {
                "project_id": pid,
                "prepared_by": "railway",
                "context_md": context_md,
                "consumed": False,
            })
            notify(f"Project ready: {name}. Open Claude Desktop to activate.")
            prepared.append(pid)
        return {"ok": True, "prepared": prepared, "count": len(prepared)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_consume(project_id: str = "") -> dict:
    """Mark project_context rows as consumed after Claude Desktop has loaded them."""
    try:
        if not project_id:
            return {"ok": False, "error": "project_id required"}
        ts = datetime.utcnow().isoformat()
        ok = sb_patch("project_context",
            f"project_id=eq.{project_id}&consumed=eq.false",
            {"consumed": True, "consumed_at": ts})
        return {"ok": bool(ok), "project_id": project_id, "consumed_at": ts}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- synthesize_evolutions ----------------------------------------------------
def t_synthesize_evolutions() -> dict:
    """Pure signal fetcher for Claude Desktop architect synthesis.

    Groq's job ends at cold_processor (pattern clustering + queuing evolutions).
    Claude (in Desktop chat) is the architect -- NOT Groq.

    This tool collects all accumulated signals and returns them to Claude.
    Claude then:
      - Reads all signals directly in the chat
      - Thinks 6 months ahead as unconstrained architect
      - Calls task_add for each new task
      - Registers tasks into task_queue (source=core_v6_registry)

    No Groq call. No auto task insertion. No Telegram notify.
    The blueprint and task_add calls are 100% Claude's responsibility.
    """
    try:
        # 1. All pending evolutions
        evolutions = sb_get("evolution_queue",
            f"select={_sel_force('evolution_queue', ['id','change_type','change_summary','pattern_key','confidence','impact'])}&status=eq.pending&order=confidence.desc",
            svc=True) or []

        # 2. Top patterns by frequency (top 40) -- exclude stale dead patterns
        patterns = sb_get("pattern_frequency",
            "select=pattern_key,frequency,domain&stale=eq.false&order=frequency.desc&limit=40",
            svc=True) or []

        # 3. Recent cold_reflections (last 10)
        cold = sb_get("cold_reflections",
            f"select={_sel_force('cold_reflections', ['summary_text','patterns_found','evolutions_queued','created_at'])}&order=id.desc&limit=10",
            svc=True) or []

        # 4. Hot reflection gaps (last 20)
        gaps = sb_get("hot_reflections",
            f"select={_sel_force('hot_reflections', ['gaps_identified','domain','quality_score'])}&gaps_identified=not.is.null&order=id.desc&limit=20",
            svc=True) or []

        # 5. Open task_queue items -- pending AND in_progress (full context for Q1+Q3 pre-flight checks)
        # H.5: safe_select -- task column is a fat JSONB blob, don't load it
        open_tasks_pending = sb_get("task_queue",
            "select=id,status,priority&status=eq.pending&order=priority.desc&limit=20",
            svc=True) or []
        open_tasks_inprog = sb_get("task_queue",
            "select=id,status,priority&status=eq.in_progress&order=priority.desc&limit=10",
            svc=True) or []
        open_tasks = open_tasks_pending + open_tasks_inprog

        # Return all raw signals to Claude Desktop.
        # Claude reads, reasons as architect, applies pre-flight, calls task_add directly.
        return {
            "ok": True,
            "instruction": (
                "YOU are the architect. Read all signals below. Think 6 months ahead. "
                "Before calling task_add for ANY task, run ALL 5 pre-flight checks:\n"
                "Q1 DUPLICATE CHECK: Does this task already exist in open_tasks (pending or in_progress)? "
                "If yes -> SKIP, do not add.\n"
                "Q2 SIGNAL FRESHNESS: Is the driving signal (evolution created_at or pattern) recent? "
                "If the signal is >30 days old with no recent activity -> flag as stale, do not add standalone task.\n"
                "Q3 REDUNDANCY CHECK: Does this task overlap >50% with an in_progress task? "
                "If yes -> merge as a subtask of that task, do not create new standalone task.\n"
                "Q4 REAL IMPACT CHECK: Is this task a real architectural/behavioral change, or cosmetic? "
                "Cosmetic tasks (renaming, minor doc updates, formatting) -> batch into a housekeeping task, not standalone.\n"
                "Q5 ACTIONABILITY CHECK: Is this task specific enough to execute now? "
                "Vague tasks ('improve reasoning', 'make CORE smarter') -> log as KB gap entry instead, not a task.\n"
                "OUTCOME MAP: pass all 5 -> call task_add | exists -> skip | stale -> skip | "
                "redundant -> merge as subtask | cosmetic -> batch | vague -> add_knowledge gap instead.\n"
                "Subtasks must be concrete, verifiable, and executable. No hallucinated tool names."
            ),
            "signals": {
                "pending_evolutions": [
                    {
                        "id": e.get("id"),
                        "confidence": e.get("confidence"),
                        "change_type": e.get("change_type"),
                        "domain": e.get("impact"),
                        "pattern_key": e.get("pattern_key", "")[:200],
                        "summary": e.get("change_summary", "")[:300],
                    } for e in evolutions[:50]
                ],
                "top_patterns": [
                    {
                        "pattern": p.get("pattern_key", "")[:200],
                        "frequency": p.get("frequency"),
                        "domain": p.get("domain"),
                    } for p in patterns
                ],
                "cold_reflection_themes": [
                    {
                        "date": c.get("created_at", "")[:10],
                        "summary": c.get("summary_text", "")[:300],
                        "patterns_found": c.get("patterns_found"),
                        "evolutions_queued": c.get("evolutions_queued"),
                    } for c in cold
                ],
                "hot_gaps": [
                    {
                        "domain": g.get("domain"),
                        "quality": g.get("quality_score"),
                        "gaps": g.get("gaps_identified"),
                    } for g in gaps
                ],
                "open_tasks": [
                    {"status": t.get("status"), "task": str(t.get("task", ""))[:200]}
                    for t in open_tasks
                ],
            },
            "counts": {
                "evolutions": len(evolutions),
                "patterns": len(patterns),
                "cold_reflections": len(cold),
                "gaps": len(gaps),
                "open_tasks_pending": len(open_tasks_pending),
                "open_tasks_inprog": len(open_tasks_inprog),
            },
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}



# -- Task 8: Server-side patching tools ---------------------------------------

def t_patch_file(path: str, patches: str, message: str, repo: str = "", dry_run: str = "false", allow_deletion: str = "false") -> dict:
    """Server-side patch: fetch file from GitHub, apply find-replace patches,
    run py_compile if .py, then push. Prevents syntax errors from crashing Railway.
    patches: JSON array of {old_str, new_str} objects (same format as multi_patch).
    dry_run: true = show diff but do not push.
    allow_deletion: must be 'true' to permit a patch with empty/missing new_str.
                    Default false -- empty new_str is BLOCKED to prevent accidental deletion."""
    try:
        import subprocess, tempfile as _tmpfile
        repo = repo or GITHUB_REPO
        if isinstance(patches, str):
            patches = json.loads(patches)
        content = _gh_blob_read(path, repo)
        _orig_content = content  # save for diff — avoids second network call
        applied = []
        skipped = []
        _allow_del = str(allow_deletion).lower() == "true"
        for i, patch in enumerate(patches):
            old = patch.get("old_str", "")
            new = patch.get("new_str")  # None if missing, "" if explicitly empty
            # DELETION GUARD: block patches with missing or empty new_str unless allow_deletion=true
            if (new is None or new == "") and not _allow_del:
                skipped.append({"index": i, "reason": "DELETION BLOCKED: new_str is missing or empty. Pass allow_deletion=true to permit intentional deletions.", "old_str": old[:80]})
                continue
            if new is None:
                new = ""
            pf = _patch_find(content, old)
            found, count, matched, hint = pf[0], pf[1], pf[2], pf[3]
            auto_context = pf[4] if len(pf) > 4 else None
            if not found:
                skip_entry = {"index": i, "reason": "not found", "old_str": old[:80], "hint": hint}
                if auto_context: skip_entry["auto_context"] = auto_context
                skipped.append(skip_entry)
            elif count > 1:
                all_lines = content.splitlines()
                locs, pos = [], 0
                while True:
                    idx = content.find(old, pos)
                    if idx == -1: break
                    ln = content[:idx].count('\n') + 1
                    cs, ce = max(0, ln-3), min(len(all_lines), ln+old.count('\n')+3)
                    locs.append({"line": ln, "context": "\n".join(f"{cs+j+1:4d}  {l}" for j,l in enumerate(all_lines[cs:ce]))})
                    pos = idx + 1
                    if len(locs) >= 5: break
                skipped.append({"index": i, "reason": f"ambiguous ({count}x)", "old_str": old[:80],
                                 "locations": locs,
                                 "fix_hint": "Extend old_str with unique surrounding lines from one location."})
            else:
                content = content.replace(matched, new, 1)
                applied.append({"index": i, "old_str": old[:80], "note": hint or "exact_match"})
        if not applied:
            return {
                "ok": False, "error": "No patches applied", "skipped": skipped,
                "fix_hint": "Check auto_context in skipped entries -- actual file content provided, no gh_read_lines needed."
            }
        # NEAR-MISS PROTECTION: fail hard if any patch was skipped
        if skipped:
            near_misses = [s for s in skipped if "near_miss" in str(s.get("hint", ""))]
            not_found   = [s for s in skipped if "near_miss" not in str(s.get("hint", ""))]
            return {
                "ok": False,
                "error": f"PARTIAL_PATCH_BLOCKED: {len(skipped)} patch(es) skipped -- ALL changes rolled back.",
                "applied_count": len(applied),
                "skipped_count": len(skipped),
                "near_misses": near_misses,
                "not_found": not_found,
                "fix_hint": "Use auto_context in each skipped entry -- actual file content embedded, no gh_read_lines needed.",
                "rolled_back": True,
            }
        # Syntax check for .py files
        syntax_ok = True
        syntax_error = ""
        if path.endswith(".py"):
            with _tmpfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tf:
                tf.write(content)
                tf_path = tf.name
            try:
                r = subprocess.run(
                    ["python3", "-m", "py_compile", tf_path],
                    capture_output=True, text=True, timeout=15
                )
                if r.returncode != 0:
                    syntax_ok = False
                    syntax_error = r.stderr.strip().replace(tf_path, path)
            finally:
                try:
                    os.unlink(tf_path)
                except Exception:
                    pass
            if not syntax_ok:
                return {"ok": False, "error": f"Syntax error - NOT pushed: {syntax_error}",
                        "applied": len(applied), "skipped": len(skipped)}
        # Build compact preview diff using original content (already loaded, no double read)
        preview_diff = "".join(difflib.unified_diff(
            _orig_content.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=f"{path} (before)", tofile=f"{path} (after)", n=2
        ))[:2000]
        if str(dry_run).lower() == "true":
            return {"ok": True, "dry_run": True, "path": path,
                    "applied": len(applied), "skipped": 0,
                    "syntax_ok": syntax_ok, "details": applied, "preview": preview_diff}
        commit_sha = _gh_blob_write(path, content, message, repo)
        return {"ok": True, "dry_run": False, "path": path,
                "applied": len(applied), "skipped": 0,
                "syntax_ok": syntax_ok, "details": applied,
                "commit": commit_sha[:12] if commit_sha else None,
                "preview": preview_diff}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_validate_syntax(path: str, repo: str = "") -> dict:
    """Fetch a .py file from GitHub and run py_compile on it server-side.
    Returns ok=True/False, error line number and message if syntax error found.
    Use before any deploy to catch issues without pushing."""
    try:
        import os
        import py_compile
        import tempfile as _tmpfile
        if not path.endswith(".py"):
            return {"ok": True, "skipped": True, "reason": "Not a .py file"}
        content = _gh_blob_read(path, repo or GITHUB_REPO)
        with _tmpfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tf:
            tf.write(content)
            tf_path = tf.name
        with _tmpfile.NamedTemporaryFile(prefix="core_py_compile_", suffix=".pyc", delete=False) as cf:
            cfile_path = cf.name
        try:
            py_compile.compile(tf_path, doraise=True, cfile=cfile_path)
            return {"ok": True, "path": path, "lines": len(content.splitlines()),
                    "size_kb": round(len(content.encode()) / 1024, 1), "message": "Syntax OK"}
        except py_compile.PyCompileError as e:
            err_msg = str(e).replace(tf_path, path)
            return {"ok": False, "path": path, "syntax_error": err_msg}
        finally:
            try:
                os.unlink(tf_path)
            except Exception:
                pass
            try:
                os.unlink(cfile_path)
            except Exception:
                pass
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_append_to_file(path: str, content_to_append: str, message: str, repo: str = "") -> dict:
    """Fetch a file from GitHub, append content, run py_compile if .py, push.
    Designed for adding new functions without fetching the whole file into Claude context.
    content_to_append: the text to append (must include leading newlines as needed)."""
    try:
        import subprocess, tempfile as _tmpfile
        repo = repo or GITHUB_REPO
        existing = _gh_blob_read(path, repo)
        # D.1: duplicate content guard -- block if first 80 chars already exist in file
        _first80 = content_to_append.strip()[:80]
        if _first80 and _first80 in existing:
            # Find approximate line number of existing occurrence
            _lines = existing.splitlines()
            _dup_line = next((i+1 for i, l in enumerate(_lines) if _first80[:40] in l), None)
            return {
                "ok": False,
                "error": "DUPLICATE_APPEND_BLOCKED",
                "hint": f"Content already exists in {path}"
                        + (f" near line {_dup_line}" if _dup_line else "") +
                        ". Use gh_read_lines to verify before appending."
            }
        new_content = existing + content_to_append
        # Syntax check for .py files
        if path.endswith(".py"):
            with _tmpfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tf:
                tf.write(new_content)
                tf_path = tf.name
            try:
                r = subprocess.run(
                    ["python3", "-m", "py_compile", tf_path],
                    capture_output=True, text=True, timeout=15
                )
                if r.returncode != 0:
                    err_msg = r.stderr.strip().replace(tf_path, path)
                    return {"ok": False, "error": f"Syntax error - NOT pushed: {err_msg}"}
            finally:
                try:
                    os.unlink(tf_path)
                except Exception:
                    pass
        commit_sha = _gh_blob_write(path, new_content, message, repo)
        return {"ok": True, "path": path,
                "original_lines": len(existing.splitlines()),
                "appended_lines": len(content_to_append.splitlines()),
                "total_lines": len(new_content.splitlines()),
                "commit": commit_sha[:12] if commit_sha else None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- Supabase write layer (TASK-14) ------------------------------------------
def t_sb_patch(table: str = "", filters: str = "", data="") -> dict:
    """Schema-validated Supabase update. Blocks tombstone+filterless+unknown-table calls.
    filters: PostgREST filter string e.g. 'id=eq.abc123'. REQUIRED -- no filterless updates.
    data: JSON string or dict of fields to update."""
    if not table:
        return {"ok": False, "error": "table required"}
    if table in _SB_SCHEMA.get("_tombstone", set()):
        return {"ok": False, "error": f"TOMBSTONE: '{table}' is retired"}
    if table not in _SB_SCHEMA.get("tables", {}):
        known = sorted(_SB_SCHEMA.get("tables", {}).keys())
        return {"ok": False, "error": f"UNKNOWN_TABLE: '{table}' not in schema registry.",
                "hint": f"Known tables: {known}",
                "tip": "Common mistake: 'tasks' should be 'task_queue'"}
    if not filters or not filters.strip():
        return {"ok": False, "error": "BLOCKED: filters required -- full-table updates not allowed",
                "hint": "e.g. filters='id=eq.<uuid>' or filters='status=eq.pending'"}
    if isinstance(data, str):
        try: data = json.loads(data)
        except Exception as e:
            return {"ok": False, "error": f"data must be valid JSON: {e}"}
    if not isinstance(data, dict) or not data:
        return {"ok": False, "error": "data must be a non-empty JSON object"}
    # Validate only the columns being updated (partial update -- don't require all required fields)
    schema = _sb_schema(table)
    if schema:
        known = set(schema.get("columns", {}).keys())
        bad = [c for c in data if c not in known]
        if bad:
            return {"ok": False, "error": f"UNKNOWN_COLUMN(S): {bad} not in '{table}'",
                    "hint": f"Valid columns: {sorted(known)}"}
        enums = schema.get("enums", {})
        for col, allowed in enums.items():
            if col in data and data[col] is not None and str(data[col]) not in [str(v) for v in allowed]:
                return {"ok": False, "error": f"INVALID_ENUM: '{col}'='{data[col]}' -- allowed: {allowed}"}
    try:
        ok = sb_patch(table, filters.strip(), data)
        if not ok:
            return {"ok": False, "error_code": "patch_failed",
                    "message": f"Supabase patch failed for '{table}'", "retry_hint": True}
        return {"ok": True, "table": table, "filters": filters, "updated_fields": sorted(data.keys())}
    except Exception as e:
        return {"ok": False, "error_code": "exception", "message": str(e), "retry_hint": True}


def t_sb_upsert(table: str = "", data="", on_conflict: str = "") -> dict:
    """Schema-validated Supabase upsert. Blocks tombstone+unknown-table, validates columns+enums+required.
    data: JSON string or dict of the full row.
    on_conflict: column(s) defining uniqueness. If omitted, schema default is used.
    Schema defaults: knowledge_base=domain,topic | script_templates=name | pattern_frequency=pattern_key"""
    if not table:
        return {"ok": False, "error": "table required"}
    if table in _SB_SCHEMA.get("_tombstone", set()):
        return {"ok": False, "error": f"TOMBSTONE: '{table}' is retired"}
    if table not in _SB_SCHEMA.get("tables", {}):
        known = sorted(_SB_SCHEMA.get("tables", {}).keys())
        return {"ok": False, "error": f"UNKNOWN_TABLE: '{table}' not in schema registry.",
                "hint": f"Known tables: {known}",
                "tip": "Common mistake: 'tasks' should be 'task_queue'"}
    if isinstance(data, str):
        try: data = json.loads(data)
        except Exception as e:
            return {"ok": False, "error": f"data must be valid JSON: {e}"}
    if not isinstance(data, dict) or not data:
        return {"ok": False, "error": "data must be a non-empty JSON object"}
    # Auto-fill on_conflict from schema if not provided
    schema = _sb_schema(table)
    conflict_col = on_conflict.strip() if on_conflict and on_conflict.strip() else ""
    if not conflict_col and schema:
        conflict_col = schema.get("on_conflict", "")
    if not conflict_col:
        return {"ok": False, "error": "on_conflict required",
                "hint": f"Schema default for '{table}': {schema.get('on_conflict', 'none defined -- specify manually')}"}
    errs = _validate_write(table, data)
    if errs:
        return {"ok": False, "error": "schema_violation", "violations": errs,
                "hint": f"Valid columns for {table}: {sorted(schema.get('columns', {}).keys()) if schema else 'unknown'}"}
    try:
        ok = sb_upsert(table, data, conflict_col)
        if not ok:
            return {"ok": False, "error_code": "upsert_failed",
                    "message": f"Supabase upsert failed for '{table}'", "retry_hint": True}
        return {"ok": True, "table": table, "on_conflict": conflict_col, "fields": sorted(data.keys())}
    except Exception as e:
        return {"ok": False, "error_code": "exception", "message": str(e), "retry_hint": True}


def t_sb_delete(table: str, filters: str, confirm: str = "") -> dict:
    """Delete rows from a Supabase table matching filters.
    filters: PostgREST filter string e.g. 'id=eq.abc123'. REQUIRED -- rejected if empty.
    confirm: pass the literal string 'DELETE' to execute. Any other value runs a dry-run
             showing rows that WOULD be deleted without touching anything.
    PROTECTED tables (cannot delete from): sessions, mistakes, hot_reflections,
    cold_reflections, pattern_frequency, changelog, evolution_queue.
    ALLOWED tables: knowledge_base, task_queue, project_context, script_templates,
    system_map, projects.
    Always dry-run first to see what will be affected."""
    _PROTECTED = {
        "sessions", "mistakes", "hot_reflections", "cold_reflections",
        "pattern_frequency", "changelog", "evolution_queue"
    }
    _ALLOWED = {
        "knowledge_base", "task_queue", "project_context",
        "script_templates", "system_map", "projects"
    }
    if not filters or not str(filters).strip():
        return {"ok": False, "error": "BLOCKED: filters required -- cannot delete from entire table"}
    if table in _PROTECTED:
        return {"ok": False, "error": f"BLOCKED: {table} is a protected audit table -- deletes not allowed"}
    if table not in _ALLOWED:
        return {"ok": False, "error": f"BLOCKED: {table} not in allowed list. Allowed: {sorted(_ALLOWED)}"}
    # Dry-run: preview rows that would be deleted — ok=False so caller knows action still needed
    if str(confirm).strip() != "DELETE":
        try:
            preview = sb_get(table, f"{filters}&limit=10", svc=True)
            return {
                "ok": False,  # False = no action taken yet, confirmation required
                "dry_run": True,
                "table": table,
                "filters": filters,
                "would_delete_preview": preview if isinstance(preview, list) else [],
                "row_count_estimate": len(preview) if isinstance(preview, list) else 0,
                "message": "DRY RUN — no rows deleted. Pass confirm='DELETE' to execute.",
                "action_required": "Call again with confirm='DELETE' to actually delete"
            }
        except Exception as e:
            return {"ok": False, "error": f"dry-run preview failed: {e}"}
    # Execute delete
    ok = sb_delete(table, filters.strip())
    return {"ok": ok, "table": table, "filters": filters, "deleted": ok}


# -- 13.F New tools -----------------------------------------------------------

def t_get_state_key(key: str) -> dict:
    """Read back a specific state key written by update_state. Fills the read-back gap."""
    if not key:
        return {"ok": False, "error": "key required"}
    try:
        # A.9: PostgREST LIKE uses * not % as wildcard
        rows = sb_get("sessions",
            f"select=summary&summary=like.*%5Bstate_update%5D+{key}:*&order=id.desc&limit=1",
            svc=True) or []
        if not rows:
            return {"ok": False, "key": key, "found": False, "value": None}
        raw = rows[0].get("summary", "")
        prefix = f"[state_update] {key}: "
        value = raw[len(prefix):].strip() if raw.startswith(prefix) else raw
        return {"ok": True, "key": key, "value": value, "found": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _latest_session_snapshot_raw() -> dict:
    """Return the most recent session snapshot payload stored in sessions.summary."""
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


def _persist_session_snapshot(snapshot: dict, scope: str = "boot") -> dict:
    """Persist a compact snapshot into sessions.summary for cross-session continuity."""
    try:
        blob = json.dumps(snapshot or {}, default=str)
        payload = {
            "summary": f"[state_update] session_snapshot: {blob[:1200]}",
            "actions": [f"session_snapshot persisted ({scope})"],
            "interface": "mcp",
        }
        ok = sb_post("sessions", payload)
        return {"ok": bool(ok), "scope": scope, "bytes": len(blob), "stored": bool(ok)}
    except Exception as e:
        return {"ok": False, "scope": scope, "error": str(e), "stored": False}


def t_session_snapshot(scope: str = "boot", persist: str = "true") -> dict:
    """Build a canonical session continuity snapshot, and optionally persist it."""
    try:
        state = t_state()
        health = t_health()
        training = t_get_training_pipeline()
        quality_alert = t_get_quality_alert()
        resume_task = state.get("resume_task") or {}
        resume_checkpoint = state.get("resume_checkpoint")
        snapshot = {
            "scope": scope or "boot",
            "generated_at": datetime.utcnow().isoformat(),
            "health": health.get("overall", "unknown"),
            "counts": state.get("counts", {}),
            "last_session_ts": state.get("last_session_ts", ""),
            "resume_task": {
                "id": resume_task.get("id"),
                "status": resume_task.get("status"),
                "priority": resume_task.get("priority"),
            } if isinstance(resume_task, dict) else None,
            "resume_checkpoint": resume_checkpoint,
            "quality_alert": quality_alert,
            "training": {
                "pipeline_ok": training.get("pipeline_ok"),
                "health_flags": training.get("health_flags", []),
                "quality": training.get("quality", {}),
            },
            "active_goals": [
                {
                    "goal": g.get("goal"),
                    "domain": g.get("domain"),
                    "status": g.get("status"),
                    "progress": g.get("progress"),
                }
                for g in (state.get("active_goals") or [])[:10]
            ],
            "owner_profile": (state.get("owner_profile") or [])[:5],
            "weak_capability_domains": state.get("weak_capability_domains", [])[:10],
            "system_map_drift": state.get("system_map_drift", {}),
        }
        persisted = None
        if str(persist).strip().lower() not in ("false", "0", "no"):
            persisted = _persist_session_snapshot(snapshot, scope=scope or "boot")
        previous = _latest_session_snapshot_raw()
        return {
            "ok": True,
            "snapshot": snapshot,
            "persisted": persisted,
            "previous_snapshot": previous.get("snapshot"),
            "previous_snapshot_created_at": previous.get("created_at"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "scope": scope}



def t_task_update(task_id: str = "", status: str = "", result: str = "") -> dict:
    """Update a task_queue row status. task_id = UUID or TASK-N string. status = pending/in_progress/done/failed."""
    valid = {"pending", "in_progress", "done", "failed"}
    if not task_id or not status:
        return {"ok": False, "error": "task_id and status required"}
    if status not in valid:
        return {"ok": False, "error": f"status must be one of: {valid}"}
    try:
        rows = _task_resolve_rows(task_id)
        if not rows:
            return {"ok": False, "error": f"task not found: {task_id}"}
        row_id = rows[0]["id"]
        data = {"status": status}
        if result:
            data["result"] = result
        ok = sb_patch("task_queue", f"id=eq.{row_id}", data)
        verification = t_task_verification_packet(
            task_id=task_id,
            expected_status=status,
            require_result="true" if status in {"done", "failed"} else "false",
            require_checkpoint="true" if status == "in_progress" else "false",
        )
        return {
            "ok": bool(ok),
            "task_id": task_id,
            "row_id": row_id,
            "status": status,
            "verified": bool(verification.get("ok") and not verification.get("blocked")),
            "verification_packet": verification,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _task_resolve_rows(task_id: str) -> list:
    """Resolve a task row by UUID or TASK-N/title label."""
    import re as _re
    _is_uuid = bool(_re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', task_id.lower()))
    rows = []
    if _is_uuid:
        rows = sb_get("task_queue", f"select=id,task,status,result,checkpoint,created_at,updated_at,priority,source&id=eq.{task_id}&limit=1", svc=True) or []
    if not rows:
        # Fall back: search task JSON for task_id field (handles TASK-N strings)
        all_rows = sb_get(
            "task_queue",
            f"select=id,task,status,result,checkpoint,created_at,updated_at,priority,source&source=in.(core_v6_registry,mcp_session)&limit=200",
            svc=True
        ) or []
        rows = [r for r in all_rows if f'"task_id": "{task_id}"' in str(r.get("task", ""))
                or f'"title": "{task_id}"' in str(r.get("task", ""))]
    return rows


def t_task_verification_packet(
    task_id: str = "",
    expected_status: str = "",
    require_result: str = "false",
    require_checkpoint: str = "false",
) -> dict:
    """Canonical task verification packet.

    Verifies a task row exists, optionally matches an expected status, and
    optionally requires result/checkpoint fields depending on the task state.
    """
    if not task_id:
        return {"ok": False, "error": "task_id required"}
    try:
        rows = _task_resolve_rows(task_id)
        if not rows:
            return {
                "ok": True,
                "task_id": task_id,
                "blocked": True,
                "verification_score": 0.0,
                "failed_checks": ["task_not_found"],
                "warnings": [],
                "message": f"Task not found: {task_id}",
            }

        row = rows[0]
        current_status = str(row.get("status") or "")
        expected_status = str(expected_status or "").strip()
        require_result_bool = str(require_result).lower() in ("true", "1", "yes", "on")
        require_checkpoint_bool = str(require_checkpoint).lower() in ("true", "1", "yes", "on")

        passed = ["task_found", f"current_status={current_status or 'unknown'}"]
        failed = []
        warnings = []

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

        if require_checkpoint_bool or current_status == "in_progress":
            if row.get("checkpoint") or row.get("checkpoint_draft"):
                passed.append("checkpoint_present")
            else:
                failed.append("missing_checkpoint")

        # Soft stale warning for task rows lingering too long without updates.
        try:
            from datetime import datetime, timezone, timedelta
            ts_candidates = [
                ("updated_at", row.get("updated_at")),
                ("created_at", row.get("created_at")),
            ]
            now = datetime.now(timezone.utc)
            for label, ts_str in ts_candidates:
                if not ts_str:
                    continue
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                else:
                    ts = ts.astimezone(timezone.utc)
                if ts > now:
                    warnings.append(f"{label}_future_timestamp_clamped")
                if label == "updated_at":
                    if current_status == "in_progress" and ts < now - timedelta(hours=24):
                        warnings.append("in_progress_stale>24h")
                    if current_status == "pending" and ts < now - timedelta(days=7):
                        warnings.append("pending_stale>7d")
        except Exception:
            pass

        score = len(passed) / max(1, len(passed) + len(failed))
        score = max(0.0, round(score - min(0.15, 0.02 * len(warnings)), 2))
        blocked = bool(failed) or score < 0.8
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
            "row": {
                "id": row.get("id"),
                "priority": row.get("priority"),
                "source": row.get("source"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            },
            "message": (
                f"BLOCKED: task verification failed for {task_id}"
                if blocked else
                f"CLEAR: task {task_id} verified"
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "blocked": True}


def t_task_add(title: str = "", description: str = "", priority: str = "5",
               subtasks: str = "", blocked_by: str = "") -> dict:
    """Add a new task to task_queue. source=mcp_session set automatically.
    Dedup guard: blocks insertion if a pending/in_progress task with same title already exists.
    Only source=mcp_session allowed -- self_assigned tasks must not be created via this tool."""
    if not title:
        return {"ok": False, "error": "title required"}
    preflight = _require_external_service_preflight("supabase", "task_add")
    if preflight:
        return preflight
    try: pri = int(priority) if priority else 5
    except Exception: pri = 5
    task_json = json.dumps({
        "title": title,
        "description": description,
        **({"subtasks": subtasks} if subtasks else {}),
        **({"blocked_by": blocked_by} if blocked_by else {}),
    })
    # Dedup guard: exact title match on open tasks.
    try:
        from urllib.parse import quote as _urlquote
        title_q = _urlquote(title.strip(), safe="")
        existing = sb_get(
            "task_queue",
            f"select=id,status,task&status=in.(pending,in_progress)&task=ilike.*{title_q}*&limit=5",
            svc=True
        ) or []
        for row in existing:
            try:
                t = json.loads(row.get("task") or "{}")
                if t.get("title", "").strip().lower() == title.strip().lower():
                    duplicate_result = {
                        "duplicate_of": row["id"],
                        "existing_status": row["status"],
                        "message": f"Task with title '{title}' already exists (id={row['id']}, status={row['status']})",
                    }
                    ok = sb_post("task_queue", {
                        "task": task_json,
                        "status": "failed",
                        "priority": pri,
                        "source": "mcp_session",
                        "result": json.dumps(duplicate_result, default=str),
                    })
                    if ok:
                        verification = t_task_verification_packet(
                            task_id=row["id"],
                            expected_status="failed",
                            require_result="true",
                        )
                        return {
                            "ok": True,
                            "action": "duplicate_recorded",
                            "title": title,
                            "priority": pri,
                            "duplicate_of": row["id"],
                            "duplicate_status": row["status"],
                            "verified": bool(verification.get("ok") and not verification.get("blocked")),
                            "verification_packet": verification,
                        }
                    return {
                        "ok": False,
                        "error": "DUPLICATE_TASK",
                        "message": duplicate_result["message"],
                        "existing_id": row["id"],
                        "hint": "Use task_update to progress the existing task instead",
                    }
            except Exception:
                pass
    except Exception:
        # Fallback: scan a bounded set so older rows or schema quirks don't block inserts.
        try:
            existing = sb_get(
                "task_queue",
                "select=id,status,task&status=in.(pending,in_progress)&limit=200",
                svc=True
            ) or []
            for row in existing:
                try:
                    t = json.loads(row.get("task") or "{}")
                    if t.get("title", "").strip().lower() == title.strip().lower():
                        duplicate_result = {
                            "duplicate_of": row["id"],
                            "existing_status": row["status"],
                            "message": f"Task with title '{title}' already exists (id={row['id']}, status={row['status']})",
                        }
                        ok = sb_post("task_queue", {
                            "task": task_json,
                            "status": "failed",
                            "priority": pri,
                            "source": "mcp_session",
                            "result": json.dumps(duplicate_result, default=str),
                        })
                        if ok:
                            verification = t_task_verification_packet(
                                task_id=row["id"],
                                expected_status="failed",
                                require_result="true",
                            )
                            return {
                                "ok": True,
                                "action": "duplicate_recorded",
                                "title": title,
                                "priority": pri,
                                "duplicate_of": row["id"],
                                "duplicate_status": row["status"],
                                "verified": bool(verification.get("ok") and not verification.get("blocked")),
                                "verification_packet": verification,
                            }
                        return {
                            "ok": False,
                            "error": "DUPLICATE_TASK",
                            "message": duplicate_result["message"],
                            "existing_id": row["id"],
                            "hint": "Use task_update to progress the existing task instead",
                        }
                except Exception:
                    pass
        except Exception:
            pass  # Dedup check non-fatal -- proceed if it fails
    try:
        ok = sb_post("task_queue", {
            "task": task_json, "status": "pending",
            "priority": pri, "source": "mcp_session",
        })
        verification = t_task_verification_packet(
            task_id=title,
            expected_status="pending",
        )
        return {
            "ok": bool(ok),
            "title": title,
            "priority": pri,
            "verified": bool(verification.get("ok") and not verification.get("blocked")),
            "verification_packet": verification,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_task_state_packet(task_id: str = "", expected_status: str = "", require_result: str = "false", require_checkpoint: str = "false", include_history: str = "true", history_limit: str = "8") -> dict:
    """Canonical task state packet combining tracking and verification."""
    try:
        tracking = t_task_tracking_packet(
            task_id=task_id or "",
            include_history=include_history,
            history_limit=history_limit,
        )
        verification = t_task_verification_packet(
            task_id=task_id or "",
            expected_status=expected_status or "",
            require_result=require_result,
            require_checkpoint=require_checkpoint,
        )
        if not tracking.get("ok") and not verification.get("ok"):
            return {
                "ok": False,
                "error": tracking.get("error") or verification.get("error") or "task_state unavailable",
                "tracking": tracking,
                "verification": verification,
            }
        return {
            "ok": True,
            "task_id": task_id or "",
            "tracking": tracking,
            "verification": verification,
            "summary": (
                f"task_state: tracking={'ok' if tracking.get('ok') else 'missing'} | "
                f"verified={bool(verification.get('ok') and not verification.get('blocked'))}"
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "task_id": task_id or ""}


def t_kb_update(domain: str, topic: str, instruction: str = "",
                content: str = "", confidence: str = "medium",
                source_type: str = "", source_ref: str = "") -> dict:
    """Upsert a KB entry on domain+topic. Updates if exists, inserts if new. Prevents duplicates.
    Use instead of add_knowledge when the rule may already exist.
    TASK-27.C: Logs overwrite diff to Telegram when instruction changes.
    TASK-24.B: source_type=manual|ingested|evolved|session, source_ref=URL or session_id."""
    if not domain or not topic:
        return {"ok": False, "error": "domain and topic required"}
    if not instruction and not content:
        return {"ok": False, "error": "at least one of instruction or content required"}
    preflight = _require_external_service_preflight("supabase", "kb_update")
    if preflight:
        return preflight
    try:
        # TASK-27.C: Check for existing entry -- notify owner if instruction is being changed
        existing = sb_get("knowledge_base",
            f"select=instruction&domain=eq.{domain}&topic=eq.{topic}&limit=1",
            svc=True)
        if existing:
            ex_instr = (existing[0].get("instruction") or "").strip()
            new_instr = (instruction or "").strip()
            if ex_instr and new_instr and ex_instr.lower() != new_instr.lower():
                notify(f"[KB UPDATE] {domain}/{topic}\nOld: {ex_instr[:120]}\nNew: {new_instr[:120]}")
        # Normalize confidence enum (coerce float strings if needed)
        VALID_CONFIDENCE = {"low", "medium", "high", "proven"}
        if str(confidence) not in VALID_CONFIDENCE:
            try:
                v = float(confidence)
                confidence = "proven" if v >= 0.9 else "high" if v >= 0.7 else "medium" if v >= 0.4 else "low"
                print(f"[SCHEMA] confidence coerced {v} -> '{confidence}'")
            except (TypeError, ValueError):
                confidence = "medium"
        canon_source_type, canon_source_ref = _kb_normalize_provenance(source_type, source_ref)
        row = {
            "domain": domain, "topic": topic,
            "instruction": instruction or None,
            "content": content or "",
            "confidence": confidence,
            "source": "mcp_session",
            "source_type": canon_source_type,
            "source_ref": canon_source_ref,
        }
        errs = _validate_write("knowledge_base", row)
        if errs:
            return {"ok": False, "domain": domain, "topic": topic, "error": f"Schema violation: {errs}"}
        try:
            h = {**_sbh(True), "Prefer": "resolution=merge-duplicates,return=minimal"}
            r = httpx.post(f"{SUPABASE_URL}/rest/v1/knowledge_base?on_conflict=domain,topic", headers=h, json=row, timeout=15)
            if not r.is_success:
                return {"ok": False, "domain": domain, "topic": topic, "action": "upserted", "error": f"Supabase {r.status_code}: {r.text[:300]}"}
            verification = _kb_entry_verification_packet(
                domain=domain,
                topic=topic,
                instruction=instruction or "",
                content=content or "",
                confidence=confidence,
                source_type=canon_source_type,
                source_ref=canon_source_ref,
                require_exact_match=False,
            )
            freshness = _kb_refresh_freshness_markers(
                source_type=canon_source_type,
                source_ref=canon_source_ref,
                domain=domain,
                topic=topic,
            )
            return {
                "ok": bool(verification.get("verified")),
                "domain": domain,
                "topic": topic,
                "action": "upserted",
                "verified": bool(verification.get("verified")),
                "verification_packet": verification,
                "freshness_packet": freshness,
            }
        except Exception as e:
            return {"ok": False, "domain": domain, "topic": topic, "action": "upserted", "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_mistakes_since(hours: str = "24") -> dict:
    """Return mistakes logged in the last N hours. Use at session_end to see only this session's errors."""
    try:
        h = int(hours) if hours else 24
        # A.5: compute cutoff in Python -- PostgREST interval syntax (now()-interval.Xhours) is invalid
        cutoff = (datetime.utcnow() - timedelta(hours=h)).isoformat()
        rows = sb_get("mistakes",
            f"select={_sel_force('mistakes', ['domain','context','what_failed','correct_approach','severity','root_cause','how_to_avoid'])}"
            f"&created_at=gte.{cutoff}&order=created_at.desc&limit=50",
            svc=True) or []
        return {"ok": True, "hours": h, "cutoff": cutoff, "count": len(rows), "mistakes": rows}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- TASK-21: Persistent Evolution Engine ------------------------------------
def t_add_evolution_rule(
    rule: str,
    domain: str,
    category: str = "hard_rule",
    source: str = "session_correction",
) -> dict:
    """Write a new behavioral rule to ALL server-side persistence layers atomically.

    RULE #1 FOR AGI: evolution = data in storage, not chat promises.
    This tool writes to:
      1. knowledge_base (instruction field, confidence=proven, tags=[evolution_rule])
    SESSION.md is static -- never auto-written. KB is the sole server-side persistence layer.

    NOTE: Cannot write to local skill file (C:\\Users\\rnvgg\\.claude-skills\\CORE_AGI_SKILL_V4.md)
    because this runs on Railway. That write is Claude Desktop's job via Windows-MCP:FileSystem.
    This tool returns a reminder. session_end gate (TASK-21.B) enforces it.

    category: hard_rule | sop | architectural_decision | correction
    source: session_correction | owner_directive | cold_processor
    """
    try:
        if not rule or not domain:
            return {"ok": False, "error": "rule and domain are required"}
        preflight = _require_external_service_preflight("supabase", "add_evolution_rule")
        if preflight:
            return preflight

        persisted_to = []

        # 1 -- Write to knowledge_base
        kb_ok = sb_upsert("knowledge_base", {
            "domain": domain,
            "topic": f"evolution_rule: {rule[:80]}",
            "instruction": rule,
            "content": f"category={category} source={source}. Established via add_evolution_rule. Persists across all sessions.",
            "confidence": "proven",
            "source": "evolution_rule",
            "tags": ["evolution_rule", "persistent", category],
        }, "domain,topic")
        if kb_ok:
            persisted_to.append("knowledge_base")

        # I.2: also write to behavioral_rules table for hard_rule and sop categories
        # behavioral_rules is the authoritative source for session_start rule loading
        if category in ("hard_rule", "sop", "architectural_decision"):
            try:
                br_ok = sb_post("behavioral_rules", {
                    "domain": domain,
                    "trigger": "session_open",
                    "pointer": f"add_evolution_rule: {rule[:80]}",
                    "full_rule": rule,
                    "confidence": 0.9,
                    "active": True,
                    "source": source,
                    "priority": 2,
                })
                if br_ok:
                    persisted_to.append("behavioral_rules")
            except Exception:
                pass  # non-fatal -- KB is primary, behavioral_rules is secondary

        return {
            "ok": True,
            "persisted_to": persisted_to,
            "rule": rule[:120],
            "domain": domain,
            "category": category,
            "note": "Rule persisted to: " + ", ".join(persisted_to),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_get_quality_trend(days: str = "7") -> dict:
    """Return session quality trend for the last N days.
    Shows daily average quality score, overall trend direction, and best/worst day."""
    try:
        d = int(days) if days else 7
        cutoff = (datetime.utcnow() - timedelta(days=d)).isoformat()
        rows = sb_get(
            "hot_reflections",
            f"select=quality_score,created_at,domain,source&quality_score=not.is.null"
            f"&quality_score=lte.1.0&source=eq.real&created_at=gte.{cutoff}&order=created_at.asc",
            svc=True
        ) or []
        if not rows:
            return {"ok": True, "days": d, "entries": 0, "trend": "no_data",
                    "daily": [], "avg": None, "best_day": None, "worst_day": None}

        # Group by day
        daily: dict = {}
        for r in rows:
            day = r.get("created_at", "")[:10]
            score = float(r.get("quality_score", 0))
            if day not in daily:
                daily[day] = []
            daily[day].append(score)

        daily_avgs = [
            {"date": day, "avg": round(sum(scores) / len(scores), 3), "count": len(scores)}
            for day, scores in sorted(daily.items())
        ]

        all_scores = [r["avg"] for r in daily_avgs]
        overall_avg = round(sum(all_scores) / len(all_scores), 3)

        # Trend: compare first half vs second half
        mid = len(all_scores) // 2
        if mid > 0 and len(all_scores) >= 2:
            first_half = sum(all_scores[:mid]) / mid
            second_half = sum(all_scores[mid:]) / len(all_scores[mid:])
            if second_half - first_half > 0.03:
                trend = "improving"
            elif first_half - second_half > 0.03:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "stable"

        best = max(daily_avgs, key=lambda x: x["avg"])
        worst = min(daily_avgs, key=lambda x: x["avg"])

        return {
            "ok": True,
            "days": d,
            "entries": len(rows),
            "overall_avg": overall_avg,
            "trend": trend,
            "daily": daily_avgs,
            "best_day": best,
            "worst_day": worst,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_project_index(project_id: str = "", topic: str = "", content: str = "", notify: str = "true") -> dict:
    """Index content into a project's KB. Writes one KB chunk (topic+content) to domain=project:{id},
    then updates last_indexed timestamp on the projects table.
    Use to push document extracts, notes, or field data into a project's knowledge base from Claude.ai.
    If content is empty, only refreshes the last_indexed timestamp (ping-mode).
    topic: short label for this chunk (e.g. 'RMU commissioning checklist' or 'daily report 2026-03-16').
    notify=true sends Telegram confirmation."""
    try:
        if not project_id:
            return {"ok": False, "error": "project_id required"}
        results = {}
        # Write KB chunk if content provided
        if content and topic:
            ok = sb_upsert("knowledge_base",
                {"domain": f"project:{project_id}", "topic": topic, "content": content, "confidence": "high"},
                on_conflict="domain,topic")
            results["kb_written"] = bool(ok)
            results["domain"] = f"project:{project_id}"
            results["topic"] = topic
        else:
            results["kb_written"] = False
            results["note"] = "no content/topic provided -- timestamp-only update"
        # Always update last_indexed
        ts = datetime.utcnow().isoformat()
        sb_patch("projects", f"project_id=eq.{project_id}", {"last_indexed": ts})
        results["last_indexed"] = ts
        results["project_id"] = project_id
        # Telegram notify
        if str(notify).lower() in ("true", "1", "yes"):
            msg = f"[PROJECT] {project_id} indexed\ntopic: {topic or '(timestamp only)'}\nts: {ts}"
            notify_owner(msg)
        return {"ok": True, **results}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# -- TASK-10.D: CORE Mistake Predictor ----------------------------------------

def _predict_failure(operation: str = "", context: str = "", domain: str = "", session_id: str = "") -> dict:
    """Predicts likely failure modes before executing an operation -- AGI-03 causal layer.
    Searches mistakes table via two passes: (1) what_failed keyword match, (2) root_cause + context match.
    Returns structured causal chain: predicted_failure_modes=[{mode, probability, root_cause, prevention}].
    Writes prediction to causal_predictions table for post-session counterfactual analysis.
    operation: short label of what's about to happen (e.g. 'gh_search_replace', 'patch_file', 'sb_insert').
    context: extra info (e.g. file being edited, table being written to).
    domain: optional domain filter to narrow mistake search.
    session_id: optional -- links prediction to session for accuracy tracking.
    Returns: {predicted, warnings, predicted_failure_modes, top_mistake, confidence, match_count}
    """
    try:
        if not operation:
            return {"predicted": False, "warnings": [], "predicted_failure_modes": [],
                    "top_mistake": None, "confidence": 0.0}

        from core_semantic import search as _sem
        _dom_f = f"&domain=eq.{domain}" if domain else ""
        rows = _sem("mistakes", operation, limit=8, filters=_dom_f) or []
        if not rows:
            return {"predicted": False, "warnings": [], "predicted_failure_modes": [],
                    "top_mistake": None, "confidence": 0.0,
                    "note": f"no known mistakes for operation={operation}"}

        # Build flat warnings (backward compat)
        warnings = []
        for row in rows:
            if row.get("how_to_avoid"):
                warnings.append({
                    "domain": row.get("domain", ""),
                    "warning": row.get("what_failed", "")[:120],
                    "avoid": row.get("how_to_avoid", "")[:200],
                    "severity": row.get("severity", "medium"),
                })

        # F.4: Calibrated probability -- blends static severity with learned accuracy from causal_predictions
        _sev_base = {"high": 0.70, "medium": 0.45, "low": 0.20, "critical": 0.85}
        try:
            past = sb_get("causal_predictions",
                f"select=was_correct&operation=eq.{operation[:60]}&was_correct=not.is.null&limit=20",
                svc=True) or []
            if len(past) >= 3:
                acc = sum(1 for p in past if p.get("was_correct")) / len(past)
                _calib = 0.7 + 0.3 * acc  # 70% static + 30% learned
            else:
                _calib = 1.0
        except Exception:
            _calib = 1.0
        _recency_cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        predicted_failure_modes = []
        for row in rows[:5]:
            sev = row.get("severity", "medium")
            base = _sev_base.get(sev, 0.45)
            recent = (row.get("created_at") or "") > _recency_cutoff
            prob = min(0.95, round((base + (0.1 if recent else 0.0)) * _calib, 2))
            predicted_failure_modes.append({
                "mode": (row.get("what_failed") or "")[:120],
                "probability": prob,
                "root_cause": (row.get("root_cause") or "")[:200],
                "prevention": (row.get("how_to_avoid") or "")[:200],
                "domain": row.get("domain", ""),
                "severity": sev,
                "recent": recent,
            })

        top = rows[0] if rows else None
        confidence = min(0.9, 0.3 + (len(rows) * 0.15))

        result = {
            "predicted": len(predicted_failure_modes) > 0,
            "warnings": warnings,
            "predicted_failure_modes": predicted_failure_modes,
            "top_mistake": {
                "domain": top.get("domain") if top else None,
                "what_failed": (top.get("what_failed") or "")[:150] if top else None,
                "correct_approach": (top.get("correct_approach") or "")[:200] if top else None,
            } if top else None,
            "confidence": round(confidence, 2),
            "match_count": len(rows),
            "instruction": "Review predicted_failure_modes before proceeding. Highest probability modes indicate likely failure patterns based on mistake history.",
        }

        # Write prediction to causal_predictions for post-session counterfactual analysis
        try:
            sb_post("causal_predictions", {
                "session_id": session_id or "unknown",
                "operation": operation[:200],
                "domain": domain or (top.get("domain") if top else ""),
                "planned_action": context[:500] if context else "",
                "mistake_context": {"match_count": len(rows), "domains": list({r.get("domain", "") for r in rows})},
                "predicted_failure_modes": predicted_failure_modes,
                "actual_outcome": None,  # filled in at session_end counterfactual pass
                "was_correct": None,
            })
        except Exception:
            pass  # Non-fatal -- prediction still returned

        return result
    except Exception as e:
        return {"predicted": False, "warnings": [], "predicted_failure_modes": [],
                "top_mistake": None, "confidence": 0.0, "error": str(e)}






def t_predict_failure(operation: str = "", context: str = "", domain: str = "", session_id: str = "") -> dict:
    """MCP wrapper: predict likely failure modes before an operation.
    Searches mistake history for matching patterns and returns causal chain warnings.
    operation: what you're about to do (e.g. 'gh_search_replace', 'patch_file', 'sb_insert').
    context: optional extra info (file name, table name, content snippet).
    domain: optional domain to narrow search (e.g. 'core_agi.patching').
    session_id: optional -- links prediction to session for counterfactual tracking.
    Use before any risky write operation to get a pre-flight causal warning.
    """
    try:
        return _predict_failure(operation=operation, context=context, domain=domain, session_id=session_id)
    except Exception as e:
        return {"ok": False, "error": str(e), "predicted": False, "warnings": []}
# -- TASK-26: Tool Reliability Tracking ---------------------------------------
def _track_tool_stat(tool_name: str, success: bool, error: str = None):
    """Fire-and-forget: increment tool_stats counters for today. Non-fatal -- never raises."""
    try:
        from datetime import date
        today = date.today().isoformat()
        # Fetch existing row
        existing = sb_get("tool_stats", 
            f"select=call_count,success_count,fail_count&tool_name=eq.{tool_name}&date=eq.{today}",
            svc=True)
        if existing and len(existing) > 0:
            # Row exists -- increment counters
            row = existing[0]
            new_data = {
                "call_count": row["call_count"] + 1,
                "success_count": row["success_count"] + (1 if success else 0),
                "fail_count": row["fail_count"] + (0 if success else 1),
            }
            if not success and error:
                new_data["last_error"] = error
            sb_patch("tool_stats", f"tool_name=eq.{tool_name}&date=eq.{today}", new_data)
        else:
            # Row doesn't exist -- insert initial values
            sb_post("tool_stats", {
                "tool_name": tool_name,
                "date": today,
                "call_count": 1,
                "success_count": 1 if success else 0,
                "fail_count": 0 if success else 1,
                "last_error": error if not success else None
            })
    except Exception:
        pass  # Always non-fatal


def t_tool_stats(days: str = "7") -> dict:
    """TASK-26.C: Per-tool success/fail rate for last N days (default 7).
    Returns tools sorted by fail_rate descending. Any tool with fail_rate > 0.2 is flagged.
    Use to identify flaky tools that need investigation."""
    try:
        n = int(days) if days else 7
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=n)).isoformat()
        rows = sb_get("tool_stats",
            f"select=tool_name,date,call_count,success_count,fail_count,last_error&date=gte.{cutoff}&order=date.desc&limit=500",
            svc=True) or []
        summary = _summarize_tool_stats_rows(rows)
        results = summary["tools"]
        flagged = [r["tool_name"] for r in results if r["fail_rate"] > 0.2]
        return {"ok": True, "days": n, "tools_tracked": len(results),
                "flagged": flagged, "results": results, "summary": summary["summary"]}
    except Exception as e:
        return {"ok": False, "error_code": "exception", "message": str(e), "retry_hint": True, "domain": "supabase"}


def _summarize_tool_stats_rows(rows: list) -> dict:
    """Aggregate raw tool_stats rows into fleet and per-tool metrics."""
    agg = {}
    for r in rows or []:
        name = str(r.get("tool_name") or "").strip() or "unknown"
        if name not in agg:
            agg[name] = {
                "tool_name": name,
                "calls": 0,
                "successes": 0,
                "failures": 0,
                "last_error": None,
            }
        agg[name]["calls"] += int(r.get("call_count") or 0)
        agg[name]["successes"] += int(r.get("success_count") or 0)
        agg[name]["failures"] += int(r.get("fail_count") or 0)
        if r.get("last_error") and not agg[name]["last_error"]:
            agg[name]["last_error"] = r["last_error"]

    tools = []
    fleet_calls = 0
    fleet_successes = 0
    fleet_failures = 0
    for d in agg.values():
        rate = round(d["failures"] / d["calls"], 3) if d["calls"] > 0 else 0.0
        tools.append({**d, "fail_rate": rate, "health": "ok" if rate <= 0.2 else "flagged"})
        fleet_calls += d["calls"]
        fleet_successes += d["successes"]
        fleet_failures += d["failures"]
    tools.sort(key=lambda x: (x["fail_rate"], x["failures"], x["calls"]), reverse=True)
    fleet_rate = round(fleet_failures / fleet_calls, 3) if fleet_calls > 0 else 0.0
    return {
        "summary": {
            "tool_count": len(tools),
            "fleet_calls": fleet_calls,
            "fleet_successes": fleet_successes,
            "fleet_failures": fleet_failures,
            "fleet_fail_rate": fleet_rate,
            "healthy_tools": sum(1 for t in tools if t["health"] == "ok"),
            "flagged_tools": sum(1 for t in tools if t["health"] != "ok"),
        },
        "tools": tools,
    }


def t_tool_metrics_summary(days: str = "7", limit: str = "10") -> dict:
    """Centralized tool metrics summary over tool_stats.

    Returns fleet totals, health bands, and the top failing tools so CORE can
    reason about the whole tool surface from one canonical report.
    """
    try:
        n = max(1, int(days) if days else 7)
        lim = max(1, int(limit) if limit else 10)
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=n)).isoformat()
        rows = sb_get(
            "tool_stats",
            f"select=tool_name,date,call_count,success_count,fail_count,last_error&date=gte.{cutoff}&order=date.desc&limit=1000",
            svc=True,
        ) or []
        summary = _summarize_tool_stats_rows(rows)
        tools = summary["tools"]
        return {
            "ok": True,
            "days": n,
            "summary": summary["summary"],
            "top_failing": tools[:lim],
            "bottom_failing": tools[-lim:] if tools else [],
            "flagged": [t["tool_name"] for t in tools if t["health"] != "ok"],
            "fleet_health": "degraded" if summary["summary"]["fleet_fail_rate"] > 0.2 else "healthy",
        }
    except Exception as e:
        return {"ok": False, "error_code": "exception", "message": str(e), "retry_hint": True, "domain": "supabase"}
  
# -- Tool registry ------------------------------------------------------------

def t_get_time(timezone: str = 'UTC') -> dict:
    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc)
    return {
        'utc': now.strftime('%Y-%m-%d %H:%M:%S UTC'),
        'date': now.strftime('%Y-%m-%d'),
        'time': now.strftime('%H:%M:%S'),
        'day_of_week': now.strftime('%A'),
        'timezone_note': 'Always UTC.',
    }


def t_get_time(timezone: str = 'UTC') -> dict:
    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc)
    return {
        'utc': now.strftime('%Y-%m-%d %H:%M:%S UTC'),
        'date': now.strftime('%Y-%m-%d'),
        'time': now.strftime('%H:%M:%S'),
        'day_of_week': now.strftime('%A'),
        'timezone_note': 'Always UTC.',
    }

# ── Agentic session state management ─────────────────────────────────────────
# Solves the "step 6 can't remember step 4" infinite loop problem.
# The agent writes named variables to agentic_sessions.state (jsonb scratchpad)
# and reads them back on every iteration — state survives across LLM calls.

def t_agent_state_set(session_id: str = "default", key: str = "", value: str = "") -> dict:
    """Write a named variable to the agentic session state scratchpad.
    Solves cross-iteration memory: agent writes 'kb_entry_id=9789' after step 4,
    reads it back in step 6 instead of re-searching.
    session_id: Telegram chat_id or 'default'.
    key: variable name (e.g. 'kb_entry_id', 'last_inserted_topic').
    value: value to store (stored as string in jsonb).
    """
    try:
        from core_config import SUPABASE_URL, _sbh
        import httpx as _hx, json as _j
        # Load existing state
        r = _hx.get(
            f"{SUPABASE_URL}/rest/v1/agentic_sessions?session_id=eq.{session_id}&order=created_at.desc&limit=1&select=id,state",
            headers=_sbh(True), timeout=10
        )
        rows = r.json() if r.is_success else []
        if not rows:
            return {"ok": False, "error": f"No agentic session found for session_id={session_id}"}
        row_id = rows[0]["id"]
        current_state = rows[0].get("state") or {}
        if isinstance(current_state, str):
            try: current_state = _j.loads(current_state)
            except Exception: current_state = {}
        current_state[key] = value
        # Patch state + last_updated
        patch = _hx.patch(
            f"{SUPABASE_URL}/rest/v1/agentic_sessions?id=eq.{row_id}",
            headers={**_sbh(True), "Prefer": "return=minimal"},
            json={"state": current_state, "last_updated": __import__("datetime").datetime.utcnow().isoformat()},
            timeout=10
        )
        if not patch.is_success:
            return {"ok": False, "error": f"Patch failed: {patch.status_code} {patch.text[:100]}"}
        return {"ok": True, "session_id": session_id, "key": key, "value": value, "state": current_state}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_agent_state_get(session_id: str = "default", key: str = "") -> dict:
    """Read named variable(s) from the agentic session state scratchpad.
    session_id: Telegram chat_id or 'default'.
    key: specific variable to read. Empty = return entire state dict.
    """
    try:
        from core_config import SUPABASE_URL, _sbh
        import httpx as _hx, json as _j
        r = _hx.get(
            f"{SUPABASE_URL}/rest/v1/agentic_sessions?session_id=eq.{session_id}&order=created_at.desc&limit=1&select=id,state,step_index,current_step,completed_steps",
            headers=_sbh(True), timeout=10
        )
        rows = r.json() if r.is_success else []
        if not rows:
            return {"ok": False, "error": f"No session found for {session_id}", "state": {}, "value": None}
        state = rows[0].get("state") or {}
        if isinstance(state, str):
            try: state = _j.loads(state)
            except Exception: state = {}
        if key:
            return {"ok": True, "session_id": session_id, "key": key,
                    "value": state.get(key), "found": key in state}
        return {"ok": True, "session_id": session_id, "state": state,
                "step_index": rows[0].get("step_index", 0),
                "current_step": rows[0].get("current_step"),
                "completed_steps": rows[0].get("completed_steps") or []}
    except Exception as e:
        return {"ok": False, "error": str(e), "state": {}}


def t_agent_step_done(session_id: str = "default", step_name: str = "", result: str = "") -> dict:
    """Mark an agentic step as completed and store its result.
    Call after EVERY step completes — prevents re-running completed steps.
    session_id: Telegram chat_id or 'default'.
    step_name: human-readable step label (e.g. 'insert_kb_entry', 'query_tasks').
    result: brief outcome to store (e.g. 'id=9789', 'found 3 rows').
    """
    try:
        from core_config import SUPABASE_URL, _sbh
        import httpx as _hx, json as _j
        from datetime import datetime
        r = _hx.get(
            f"{SUPABASE_URL}/rest/v1/agentic_sessions?session_id=eq.{session_id}&order=created_at.desc&limit=1&select=id,step_index,completed_steps,state",
            headers=_sbh(True), timeout=10
        )
        rows = r.json() if r.is_success else []
        if not rows:
            return {"ok": False, "error": f"No session found for {session_id}"}
        row = rows[0]
        row_id = row["id"]
        completed = row.get("completed_steps") or []
        if isinstance(completed, str):
            try: completed = _j.loads(completed)
            except Exception: completed = []
        step_record = {"step": step_name, "result": result, "ts": datetime.utcnow().isoformat()}
        # Check if already done (dedup)
        if any(s.get("step") == step_name for s in completed):
            return {"ok": True, "already_done": True, "step": step_name,
                    "note": "Step already recorded — not duplicating"}
        completed.append(step_record)
        new_index = (row.get("step_index") or 0) + 1
        patch = _hx.patch(
            f"{SUPABASE_URL}/rest/v1/agentic_sessions?id=eq.{row_id}",
            headers={**_sbh(True), "Prefer": "return=minimal"},
            json={"completed_steps": completed, "step_index": new_index,
                  "current_step": step_name, "last_updated": datetime.utcnow().isoformat()},
            timeout=10
        )
        if not patch.is_success:
            return {"ok": False, "error": f"Patch failed: {patch.status_code}"}
        return {"ok": True, "step": step_name, "result": result,
                "step_index": new_index, "total_steps_done": len(completed)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_agent_session_init(session_id: str = "default", goal: str = "", chat_id: str = "") -> dict:
    """Create or reset an agentic session with a clean state scratchpad.
    Call at the START of any multi-step agentic task.
    session_id: unique identifier (use Telegram chat_id for Telegram sessions).
    goal: what this session is trying to accomplish.
    Returns the session row id for reference.
    """
    try:
        from core_config import SUPABASE_URL, _sbh
        import httpx as _hx
        from datetime import datetime
        # Upsert on session_id — resets state/steps for fresh run
        data = {
            "session_id": session_id,
            "goal": goal or "multi-step agentic task",
            "chat_id": chat_id or session_id,
            "state": {},
            "step_index": 0,
            "status": "active",
            "completed_steps": [],
            "current_step": None,
            "action_log": [],
            "last_updated": datetime.utcnow().isoformat(),
        }
        # Try to find existing
        r = _hx.get(
            f"{SUPABASE_URL}/rest/v1/agentic_sessions?session_id=eq.{session_id}&order=created_at.desc&limit=1&select=id",
            headers=_sbh(True), timeout=10
        )
        rows = r.json() if r.is_success else []
        if rows:
            # Reset existing
            row_id = rows[0]["id"]
            patch = _hx.patch(
                f"{SUPABASE_URL}/rest/v1/agentic_sessions?id=eq.{row_id}",
                headers={**_sbh(True), "Prefer": "return=minimal"},
                json={k: v for k, v in data.items() if k != "session_id"},
                timeout=10
            )
            return {"ok": True, "action": "reset", "session_id": session_id, "row_id": row_id}
        else:
            # Insert new
            ins = _hx.post(
                f"{SUPABASE_URL}/rest/v1/agentic_sessions",
                headers={**_sbh(True), "Prefer": "return=representation"},
                json=data, timeout=10
            )
            new_row = ins.json()[0] if ins.is_success and ins.json() else {}
            return {"ok": True, "action": "created", "session_id": session_id,
                    "row_id": new_row.get("id")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


TOOLS = {
    "get_time":               {"fn": t_get_time, "perm": "READ", "args": ["timezone"],
                               "desc": "Get current UTC date and time. Use for any question about current time, date, or day of week."},
    "get_time":               {"fn": t_get_time, "perm": "READ", "args": ["timezone"],
                               "desc": "Get current UTC date and time. Use for any question about current time, date, or day of week."},
    "get_state":              {"fn": t_state,                  "perm": "READ",    "args": [],
                               "desc": "Get current CORE state: last session, counts, in_progress+pending tasks. session_md=full SESSION.md content (static bootstrap doc). Pass include_operating_context=true to also load operating_context.json."},
    "get_system_health":      {"fn": t_health,                 "perm": "READ",    "args": [],
                               "desc": "Check health of all components: Supabase, Groq, Telegram, GitHub"},
    "external_service_preflight": {"fn": t_external_service_preflight, "perm": "READ", "args": ["targets"],
                               "desc": "Focused pre-flight check before risky external-service calls. targets=supabase,groq,telegram,github. Returns blocked services and full health snapshot."},
    "get_constitution":       {"fn": t_constitution,           "perm": "READ",    "args": [],
                               "desc": "Get CORE immutable constitution"},
    "get_quality_trend":      {"fn": t_get_quality_trend,      "perm": "READ",    "args": ["days"],
                               "desc": "TASK-9.C: Session quality trend for last N days (default 7). Returns daily avg scores, overall trend (improving/stable/declining), best/worst day. Data sourced from hot_reflections quality_score field."},
    "add_evolution_rule":     {"fn": t_add_evolution_rule,     "perm": "WRITE",   "args": ["rule", "domain", "category", "source"],
                               "desc": "TASK-21: Persist a new behavioral rule to knowledge_base (confidence=proven). SESSION.md is static -- not written. Call when any new hard rule, SOP, correction or architectural decision is established this session. Returns reminder to also write to local skill file via Windows-MCP:FileSystem. category=hard_rule|sop|architectural_decision|correction."},
    "debug_fn":               {"fn": t_debug_fn,               "perm": "READ",    "args": ["fn_name", "dry_run", "extra_args"],
                               "desc": "Battle-tested debug harness for any CORE function. Staged: 0_resolve, 1_preflight, 2_execute, 4_write_intercepted. dry_run=true default (skips DB writes)."},
    "railway_logs_live":      {"fn": t_railway_logs_live,      "perm": "READ",    "args": ["lines", "keyword"],
                               "desc": "Fetch live CORE service logs from Oracle VM via journalctl. Returns actual container print() output. lines=count (default 50). keyword=filter. Use to see [RESEARCH]/[COLD]/[SIM] in real-time."},
    "railway_env_get":        {"fn": t_railway_env_get,        "perm": "READ",    "args": ["key"],
                               "desc": "Read CORE env vars from Oracle VM .env file. key=specific name (returns value). Empty = all var names only (not values)."},
    "railway_env_set":        {"fn": t_railway_env_set,        "perm": "WRITE",   "args": ["key", "value"],
                               "desc": "Write a CORE env var to Oracle VM .env file + restart service. Redeploy required for changes to take effect."},
    "railway_service_info":   {"fn": t_railway_service_info,   "perm": "READ",    "args": [],
                               "desc": "Oracle VM service snapshot: systemd state, PID, uptime, latest 3 commits. Replaces Railway service info."},
    "get_training_pipeline":  {"fn": t_get_training_pipeline,  "perm": "READ",    "args": [],
                               "desc": "Full training pipeline status. Returns: hot (total, unprocessed, simulation_ok, last_real, last_simulation), cold (last_run_ts, last_run_mins_ago, threshold, last_patterns_found, last_evolutions_queued, recent_5_summaries), patterns (active_count, stale_count, top), evolutions (pending, applied), quality (7d_avg, trend), health_flags (simulation_dead|cold_stale_Xmin|unprocessed_backlog_X|zero_patterns_last_5_runs|quality_declining), pipeline_ok. Use at session_start or when diagnosing training issues."},
    "search_kb":              {"fn": t_search_kb,              "perm": "READ",    "args": ["query", "domain", "limit"],
                           "desc": "Search knowledge base by query. Returns domain, topic, content, confidence. ALWAYS include id=gt.1 filter behavior is automatic. IF EMPTY: do NOT give up — try sb_query(table='knowledge_base', filters='id=gt.1&domain=like.*core*') as fallback. Use before any write to check duplicates. EXAMPLE: search_kb(query='railway deploy', domain='core_agi', limit='5')"},
    "knowledge_state_packet": {"fn": t_knowledge_state_packet, "perm": "READ", "args": ["domain", "topic", "instruction", "content", "confidence", "source_type", "source_ref", "query", "limit"],
                               "desc": "Canonical knowledge state packet. Bundles KB search with entry verification so downstream tools can reason about knowledge freshness and accuracy."},
    "architecture_review_packet": {"fn": t_architecture_review_packet, "perm": "READ", "args": ["module_name", "architecture_name", "task_context", "goal", "evidence", "domain", "state_hint", "knowledge_domain", "knowledge_topic", "source_type", "source_ref", "learning_rate"],
                               "desc": "Bundle module readiness with KB freshness for architecture decisions. Use when reviewing a framework change, architecture proposal, or knowledge-backed integration."},
    "search_memory":          {"fn": t_search_memory,          "perm": "READ",    "args": ["query", "domain", "limit", "tables"],
                               "desc": "Unified semantic memory search across knowledge_base plus the native semantic tables. Use this when CORE needs one reasoning context for planning, self-correction, ambiguity resolution, or decomposition."},
    "reasoning_packet":       {"fn": t_reasoning_packet,       "perm": "READ",    "args": ["query", "domain", "limit", "tables", "per_table"],
                               "desc": "Build the canonical reasoning packet (query+focus+context+top_hits) that agentic tools should consume. Deterministic, no writes."},
    "generate_synthetic_data": {"fn": t_generate_synthetic_data, "perm": "READ",  "args": ["context", "goal", "principles", "domain", "state_hint", "limit"],
                               "desc": "Generate bounded synthetic memory samples with PrincipleUtilityScore for replay, curriculum, and memory-module training packets."},
    "task_mode_packet":       {"fn": t_task_mode_packet,       "perm": "READ",    "args": ["text", "goal", "source", "message_type", "route", "attachments", "artifact_hint", "content"],
                               "desc": "Build the human-work taxonomy packet. Classifies cowork inputs into analyze/transform/create/inspect/operate/research/coordinate/learn/decide/clarify/interrupt plus subintent/detail, artifact expectation, agentic recommendation, and preferred tool families."},
    "spreadsheet_work_packet": {"fn": t_spreadsheet_work_packet, "perm": "READ",   "args": ["content", "goal", "filename", "sheet_name", "format_hint"],
                               "desc": "Analyze spreadsheet-like content and return a structured work packet: row/column counts, header detection, numeric columns, blank/duplicate rows, analysis focus, and recommended next step."},
    "document_work_packet":   {"fn": t_document_work_packet,   "perm": "READ",    "args": ["content", "goal", "audience", "format_hint"],
                               "desc": "Analyze document/text content and return a structured work packet: summary focus, action items, paragraph/sentence counts, and recommended next step."},
    "presentation_work_packet": {"fn": t_presentation_work_packet, "perm": "READ", "args": ["content", "goal", "audience", "slide_target", "theme"],
                               "desc": "Analyze presentation intent/content and return a structured work packet with slide outline, suggested slide count, and next step."},
    "review_work_packet":     {"fn": t_review_work_packet,     "perm": "READ",    "args": ["content", "goal", "artifact_type", "focus", "rubric"],
                               "desc": "Build a structured review packet for code, documents, spreadsheets, presentations, or generic artifacts. Returns findings, issues, severity, checklist, and recommended next step."},
    "repo_review_packet":     {"fn": t_repo_review_packet,     "perm": "READ",    "args": ["content", "goal", "focus"],
                               "desc": "Specialized review packet for repo diffs and code changes. Returns review checklist, findings, severity, and next step."},
    "document_review_packet": {"fn": t_document_review_packet, "perm": "READ",    "args": ["content", "goal", "focus"],
                               "desc": "Specialized review packet for documents. Returns clarity/completeness/action/fact checks and next step."},
    "spreadsheet_review_packet": {"fn": t_spreadsheet_review_packet, "perm": "READ", "args": ["content", "goal", "focus"],
                               "desc": "Specialized review packet for spreadsheets. Returns totals/duplicates/blanks/formulas/anomalies checks and next step."},
    "presentation_review_packet": {"fn": t_presentation_review_packet, "perm": "READ", "args": ["content", "goal", "focus"],
                               "desc": "Specialized review packet for presentations. Returns story-flow/slide-count/evidence/audience-fit checks and next step."},
    "tool_reliance_assessor": {"fn": t_tool_reliance_assessor, "perm": "READ",    "args": ["query", "domain", "tables", "limit", "per_table", "state_hint", "planned_action"],
                               "desc": "Assess whether CORE should stay memory-first or use more tools. Returns strategy, tool_budget, and recommended tools."},
    "evaluate_state":         {"fn": t_evaluate_state,         "perm": "READ",    "args": ["query", "domain", "tables", "limit", "per_table", "state_hint"],
                               "desc": "Evaluate an environment or system state from unified memory context. Returns readiness, risk, coherence, evidence counts, and a proceed/defer recommendation."},
    "dynamic_relational_graph": {"fn": t_dynamic_relational_graph, "perm": "READ", "args": ["query", "domain", "limit", "tables", "per_table", "state_hint"],
                               "desc": "Build a deterministic relational graph from unified memory context. Returns nodes, edges, density, dominant table, and top retrieved context."},
    "causal_graph":           {"fn": t_causal_graph,           "perm": "READ",    "args": ["query", "domain", "tables", "limit", "per_table", "state_hint", "sequence"],
                               "desc": "Build a lightweight causal graph from unified memory context or a provided sequence. Returns causal order, paths, beliefs, and graph density."},
    "causal_graph_inference": {"fn": t_causal_graph_inference, "perm": "READ",    "args": ["query", "domain", "tables", "limit", "per_table", "state_hint", "sequence", "candidate_actions", "horizon"],
                               "desc": "Fuse causal graph, relational graph, and state evaluation into a transition model usable by world-model planning."},
    "world_model_interface":  {"fn": t_world_model_interface,  "perm": "READ",    "args": ["domain", "state_hint", "current_state", "actions", "horizon"],
                               "desc": "Expose the canonical world-model interface contract and optionally return an uncertainty prediction packet."},
    "predict_with_uncertainty": {"fn": t_predict_with_uncertainty, "perm": "READ", "args": ["domain", "state_hint", "current_state", "actions", "horizon"],
                               "desc": "Predict future states with mean, variance, uncertainty, and action distribution explicitly surfaced."},
    "predictive_state_representation": {"fn": t_predictive_state_representation, "perm": "READ", "args": ["state", "error_signal", "learning_rate", "domain", "state_hint", "actions", "horizon"],
                               "desc": "Build a predictive-state representation packet and optionally simulate action ranking."},
    "dynamic_replay_buffer": {"fn": t_dynamic_replay_buffer, "perm": "READ", "args": ["experiences", "context", "limit", "capacity", "priority_floor", "domain", "state_hint"],
                               "desc": "Rank experiences with priority and contextual weighting using a bounded replay buffer heuristic."},
    "simulated_critic":       {"fn": t_simulated_critic,       "perm": "READ",    "args": ["sequence", "reward_signal", "side_effects", "domain", "state_hint"],
                               "desc": "Score a sequence of states/actions with bounded reward and risk heuristics."},
    "meta_learner":           {"fn": t_meta_learner,           "perm": "READ",    "args": ["state", "error_signal", "observation", "target", "learning_rate", "domain", "state_hint"],
                               "desc": "Adapt a predictive-state representation from an error signal and optional observation."},
    "dynamic_gating_layer":   {"fn": t_dynamic_gating_layer,   "perm": "READ",    "args": ["state", "task_context", "modules", "domain", "state_hint"],
                               "desc": "Gate submodules from current state and task context using bounded weights."},
    "gating_network":         {"fn": t_gating_network,         "perm": "READ",    "args": ["state", "task_context", "modules", "samples", "temperature", "domain", "state_hint"],
                               "desc": "Meta-train a gating network over submodules from bounded samples."},
    "causal_mapping_module":  {"fn": t_causal_mapping_module,  "perm": "READ",    "args": ["causal_graph", "context_embedding", "goal", "domain", "state_hint"],
                               "desc": "Map causal graph structure and context embedding onto a goal translation."},
    "causal_graph_data_generator": {"fn": t_causal_graph_data_generator, "perm": "READ", "args": ["context", "goal", "modules", "symbols", "actions", "domain", "state_hint", "limit"],
                               "desc": "Generate bounded causal graph data from context, module hints, and action signals for analysis and training packets."},
    "principle_search_module": {"fn": t_principle_search_module, "perm": "READ", "args": ["principles", "state", "goal", "task_context", "domain", "state_hint"],
                               "desc": "Rank guiding principles against the current state and task context."},
    "causal_principle_discovery": {"fn": t_causal_principle_discovery, "perm": "READ", "args": ["causal_graph", "context", "goal", "principles", "symbols", "actions", "task_context", "domain", "state_hint", "depth", "rollouts"],
                               "desc": "Discover causal nodes, principle rankings, and symbolic rules from a bounded context packet."},
    "state_reconciliation_buffer": {"fn": t_state_reconciliation_buffer, "perm": "READ", "args": ["states", "context", "limit", "capacity", "domain", "state_hint"],
                               "desc": "Reconcile competing state snapshots before downstream planning."},
    "hierarchical_search_tree": {"fn": t_hierarchical_search_tree, "perm": "READ", "args": ["current_state", "goal", "hwm_levels", "candidate_actions", "horizon", "rollouts", "exploration_weight", "domain", "state_hint"],
                               "desc": "Build a bounded hierarchical search tree and return the best branch."},
    "domain_invariant_feature_packet": {"fn": t_domain_invariant_feature_packet, "perm": "READ", "args": ["current_state", "goal", "modules", "symbols", "actions", "task_context", "hwm_levels", "domain", "state_hint", "limit"],
                               "desc": "Extract domain-invariant features from state, goal, and task context for meta-controller routing and task verification."},
    "module_assessment_packet": {"fn": t_module_assessment_packet, "perm": "READ", "args": ["module_name", "module_description", "task_context", "goal", "evidence", "domain", "state_hint", "learning_rate"],
                               "desc": "Assess a new module's readiness, cost, and overfit risk before promotion. Use for new-module verification, performance impact checks, and meta-learning robustness review."},
    "hierarchical_gated_neuro_symbolic_world_model": {"fn": t_hierarchical_gated_neuro_symbolic_world_model, "perm": "READ", "args": ["current_state", "goal", "modules", "symbols", "actions", "hwm_levels", "horizon", "rollouts", "exploration_weight", "principles", "task_context", "causal_graph", "domain", "state_hint", "full_rollout"],
                               "desc": "Combine hierarchical gating, state reconciliation, causal mapping, principled search, prediction, and critic scoring into one neuro-symbolic world-model packet."},
    "meta_contextual_router": {"fn": t_meta_contextual_router, "perm": "READ",    "args": ["current_state", "goal", "hwm_levels", "domain", "state_hint", "limit"],
                               "desc": "Route a current state toward a probability distribution over hierarchical world-model levels."},
    "adaptive_temporal_filter": {"fn": t_adaptive_temporal_filter, "perm": "READ", "args": ["sequence", "domain", "state_hint", "window", "decay"],
                               "desc": "Rank and smooth temporal context with bounded decay and hint-aware weighting."},
    "temporal_attention": {"fn": t_temporal_attention, "perm": "READ", "args": ["sequence", "domain", "state_hint", "heads", "window"],
                               "desc": "Apply bounded temporal attention over context sequences and return attended ordering."},
    "monte_carlo_tree_search": {"fn": t_monte_carlo_tree_search, "perm": "READ", "args": ["query", "domain", "limit", "tables", "per_table", "state_hint", "candidate_actions", "rollouts", "exploration_weight", "class_path"],
                               "desc": "Run a flexible Monte Carlo Tree Search bridge over CORE reasoning packets. Uses built-in fallback unless class_path points to an external MonteCarloTreeSearch class."},
    "hierarchical_search_controller": {"fn": t_hierarchical_search_controller, "perm": "READ", "args": ["current_state", "goal", "hwm_levels", "domain", "state_hint", "horizon", "candidate_actions", "rollouts", "exploration_weight"],
                               "desc": "Manage multi-level MCTS with WorldModel prediction and state evaluation. Returns a level distribution and bounded plan."},
    "temporal_hierarchical_world_model": {"fn": t_temporal_hierarchical_world_model, "perm": "READ", "args": ["sequence", "current_state", "actions", "domain", "state_hint", "temporal_window", "decay", "horizon"],
                               "desc": "Temporal wrapper around hierarchical world-model reasoning with sequence filtering, attention, and bounded search."},
    "world_model":            {"fn": t_world_model,            "perm": "WRITE",   "args": ["domain", "state_hint", "experience", "current_state", "actions", "horizon"],
                               "desc": "Bounded world-model interface: capture an experience into knowledge_base or predict future states from a current state and candidate actions."},
    "dynamic_router":          {"fn": t_dynamic_router,          "perm": "READ", "args": ["predictions", "confidences", "candidates", "policy_topic", "policy_domain", "policy_override", "query", "domain", "state_hint", "use_state_evaluator", "exploration_weight"],
                               "desc": "Deterministic router over world-model outputs (predictions/confidences) plus optional learned policy from KB (meta/dynamic_router_policy). Returns best candidate with component scores."},
    "meta_representation":    {"fn": t_meta_representation,    "perm": "READ",    "args": ["op", "name", "version", "features", "metadata", "a", "b", "strategy"],
                               "desc": "Create/merge/validate a MetaRepresentation payload for passing structured state between CORE modules. op=new|merge|validate."},
    "task_similarity_metric": {"fn": t_task_similarity_metric, "perm": "READ",    "args": ["task_a", "task_b"],
                               "desc": "Compare two task payloads and return a bounded similarity score for consolidation and routing."},
    "novelty_assessment":     {"fn": t_novelty_assessment,     "perm": "READ",    "args": ["experience", "reference_memory", "limit"],
                               "desc": "Assess how novel an experience is relative to recent memory/task representations and return a routing recommendation."},
    "consolidation_manager":  {"fn": t_consolidation_manager,  "perm": "READ",    "args": ["limit", "similarity_threshold"],
                               "desc": "Cluster similar queued tasks and return a compact consolidation summary for review."},
    "tool_metrics_summary":   {"fn": t_tool_metrics_summary,   "perm": "READ",    "args": ["days", "limit"],
                               "desc": "Centralized tool fleet metrics over tool_stats. Returns fleet totals, health bands, and top failing tools."},
    "active_learning_strategy": {"fn": t_active_learning_strategy, "perm": "READ", "args": ["strategy_name", "budget", "limit", "similarity_threshold"],
                               "desc": "Select high-value tasks for active learning using a pluggable strategy interface."},
    "get_mistakes":           {"fn": t_get_mistakes,           "perm": "READ",    "args": ["domain", "limit"],
                               "desc": "Get recorded mistakes: what_failed, correct_approach, severity, root_cause. Call before any domain operation to avoid repeating known errors. IF EMPTY or fails: fallback to sb_query(table='mistakes', filters='id=gt.1', order='created_at.desc', limit='5', select='domain,what_failed,fix,created_at') — this ALWAYS works. EXAMPLE: get_mistakes(domain='core_agi', limit='5')"},
    "read_file":              {"fn": t_read_file,              "perm": "READ",    "args": ["path", "repo", "start_line", "end_line"],
                               "desc": "Read file from GitHub repo. Optional start_line/end_line for range. Returns total_line_count + truncated flag. Cap 8000 chars. For large files use gh_read_lines instead."},
    "sb_query":               {"fn": t_sb_query,               "perm": "READ",    "args": ["table", "filters", "limit", "order", "select"],
                           "desc": "Raw Supabase PostgREST query — use when dedicated tools return empty. CRITICAL: ALWAYS include id=gt.1 in filters (row id=1 is a probe row, excluding it returns garbage). filters format: 'field=eq.value&field2=gt.0&id=gt.1'. COMMON MISTAKE: forgetting id=gt.1. EXAMPLES: sb_query(table='mistakes', filters='id=gt.1', order='created_at.desc', limit='5', select='domain,what_failed,fix') | sb_query(table='pattern_frequency', filters='id=gt.1', order='frequency.desc', limit='8') | sb_query(table='sessions', filters='id=gt.1', order='created_at.desc', limit='3', select='summary,quality,domain')"},
    "list_evolutions":        {"fn": t_list_evolutions,        "perm": "READ",    "args": ["status"],
                               "desc": "List evolutions. status=pending|synthesized|applied|rejected (default: pending). Use synthesized to see items Claude has already read via synthesize_evolutions."},
    "update_state":           {"fn": t_update_state,           "perm": "WRITE",   "args": ["key", "value", "reason"],
                               "desc": "Write a key-value state update to sessions table. Use get_state_key to read it back. Useful for persisting simulation settings or mid-session flags across tool calls."},
    "set_simulation":         {"fn": t_set_simulation,         "perm": "WRITE",   "args": ["instruction"],
                               "desc": "Set a custom simulation scenario for the background researcher. CORE crafts the Groq prompt and loops it every 60 min. Empty instruction resets to default."},
    "add_knowledge":          {"fn": t_add_knowledge,          "perm": "WRITE",   "args": ["domain", "topic", "instruction", "content", "tags", "confidence", "source_type", "source_ref"],
                               "desc": "Add NEW knowledge that does not yet exist. Returns ok=False if domain+topic already exists -- correct behavior, not an error. Use ONLY for genuinely new knowledge. Use kb_update to overwrite stale or outdated existing knowledge. source_type=manual|ingested|evolved|session (optional). source_ref=URL or session_id (optional)."},
    "log_mistake":            {"fn": t_log_mistake,            "perm": "WRITE",   "args": ["context", "what_failed", "correct_approach", "domain", "root_cause", "how_to_avoid", "severity"],
                               "desc": "Log a mistake so CORE never repeats it. correct_approach=the right way to do it (required). severity=low|medium|high|critical. Always call this when CORE makes an error â€” it is the primary learning mechanism."},
    "notify_owner":           {"fn": t_notify,                 "perm": "WRITE",   "args": ["message", "level"],
                               "desc": "Send Telegram notification. level=info|warn|alert|ok. Use for async events, deploys, errors. Bot: @reinvagnarbot, chat_id=838737537."},
    "sb_insert":              {"fn": t_sb_insert,              "perm": "WRITE",   "args": ["table", "data"],
                               "desc": "Insert single row into Supabase. Schema-validated against _SB_SCHEMA. Blocks tombstone tables. Prefer dedicated tools (add_knowledge, log_mistake, task_add) over raw sb_insert."},
    "sb_bulk_insert":         {"fn": t_sb_bulk_insert,         "perm": "WRITE",   "args": ["table", "rows"],
                               "desc": "Insert multiple rows into Supabase in one HTTP call. rows=JSON array. Use for 5+ rows. Returns rows_attempted + status_code."},
    "get_state_key":          {"fn": t_get_state_key,          "perm": "READ",    "args": ["key"],
                               "desc": "Read back a specific state key written by update_state. Use to retrieve values set via update_state or set_simulation â€” the only way to read back those keys."},
    "task_update":            {"fn": t_task_update,            "perm": "WRITE",   "args": ["task_id", "status", "result"],
                               "desc": "Update a task_queue row status. task_id=UUID or TASK-N string. status=pending/in_progress/done/failed. result=optional outcome note. Use instead of raw sb_query for task status changes."},
    "task_packet":            {"fn": t_task_packet,            "perm": "READ",    "args": ["task_id", "expected_status", "require_result", "require_checkpoint"],
                                "desc": "Canonical task packet. Verifies a task row exists, matches expected status, and surfaces checkpoint/result/error state for task management."},
    "task_tracking_packet":   {"fn": t_task_tracking_packet,   "perm": "READ",    "args": ["task_id", "include_history", "history_limit"],
                               "desc": "Canonical ongoing-task tracking packet. Surfaces the task row, checkpoint history, last checkpoint, age, and stall state for in_progress work."},
    "task_state_packet":      {"fn": t_task_state_packet,      "perm": "READ",    "args": ["task_id", "expected_status", "require_result", "require_checkpoint", "include_history", "history_limit"],
                               "desc": "Canonical task state packet combining tracking and verification into one structured read. Use when you need both the live task row and its verification status together."},
    "task_error_packet":      {"fn": t_task_error_packet,      "perm": "WRITE",   "args": ["task_id", "error", "phase", "summary", "retryable", "next_step", "checkpoint"],
                                "desc": "Record a terminal task failure with structured error metadata, then verify the failed row exists. Use for claim/execute/finalize failures."},
    "task_verification_packet": {"fn": t_task_verification_packet, "perm": "READ", "args": ["task_id", "expected_status", "require_result", "require_checkpoint"],
                               "desc": "Canonical task verification packet. Verifies a task row exists, matches expected status, and has the required checkpoint/result fields. Use after task updates or before trusting task state."},
    "task_verification_bundle": {"fn": t_task_verification_bundle, "perm": "READ", "args": ["task_id", "expected_status", "require_result", "require_checkpoint", "include_history", "history_limit", "session_id", "strict", "require_system_checkpoint", "operation", "target_file", "context", "assumed_state", "sources", "action_type", "owner_token", "sequence", "reward_signal", "side_effects", "principles", "task_context", "goal", "current_state", "hwm_levels", "candidate_actions", "horizon", "rollouts", "exploration_weight", "causal_graph", "domain", "state_hint"],
                               "desc": "Integrated verification bundle for task work. Composes task state, system verification, deploy/trust checks, causal mapping, principle search, bounded critic scoring, and hierarchical planning into one packet. Use when verifying task side effects or step completeness across domains."},
    "task_add":               {"fn": t_task_add,               "perm": "WRITE",   "args": ["title", "description", "priority", "subtasks", "blocked_by"],
                               "desc": "Add a new task to task_queue with proper schema. Sets source=mcp_session automatically. Use instead of raw sb_insert for new tasks â€” enforces correct structure."},
    "kb_update":              {"fn": t_kb_update,              "perm": "WRITE",   "args": ["domain", "topic", "instruction", "content", "confidence", "source_type", "source_ref"],
                               "desc": "Update EXISTING stale or outdated KB knowledge. Overwrites entry at domain+topic. Use when a rule has changed or content is wrong. Do NOT use for new knowledge -- use add_knowledge for that. Will also insert if not found (upsert behavior -- prevents duplicates). source_type=manual|ingested|evolved|session (optional). source_ref=URL or session_id (optional)."},
    "mistakes_since":         {"fn": t_mistakes_since,         "perm": "READ",    "args": ["hours"],
                               "desc": "Return mistakes logged in the last N hours (default 24). Use at session_end to review only this session's errors, not the rolling last-10."},
    "trigger_cold_processor": {"fn": t_trigger_cold_processor, "perm": "WRITE",   "args": [],
                               "desc": "Manually trigger cold processor run. Processes unread hot_reflections, extracts patterns, queues evolutions. Use when cold_stale flag set in training_pipeline health_flags. Response size-guarded to 10 items max."},
    "backfill_patterns":      {"fn": t_backfill_patterns,      "perm": "WRITE",   "args": ["batch_size"],
                               "desc": "TASK-20: Fire-and-forget backfill of pattern_frequency -> KB. Returns job_id immediately. Poll backfill_status for result. batch_size=max per run (default 20)."},
    "ingest_knowledge":       {"fn": t_ingest_knowledge,       "perm": "EXECUTE", "args": ["topic", "sources", "max_per_source", "since_days"],
                               "desc": "Trigger knowledge ingestion pipeline. Fetches topic from public sources (arxiv/docs/medium/reddit/hackernews/stackoverflow), scores by engagement, writes to kb_* tables, injects hot_reflections for CORE to evolve. sources=comma-separated or 'all'. max_per_source=cap per fetcher (default 20). since_days=recency filter (default 7)."},
    "listen":                 {"fn": t_listen,                 "perm": "EXECUTE", "args": [],
                               "desc": "LISTEN MODE: fire-and-forget. Starts background listen job, returns job_id immediately. Then call listen_result to fetch chunks once done."},
    "listen_result":           {"fn": t_listen_result,           "perm": "EXECUTE", "args": [],
                               "desc": "Fetch listen job status + results. Call after listen. Returns status (running|done|error), cycles, patterns_found, evolutions_queued, chunks. If running, wait and call again."},
    "approve_evolution":      {"fn": t_approve_evolution,      "perm": "WRITE",   "args": ["evolution_id"],
                               "desc": "Approve and apply a pending evolution. For change_type=code: inspect diff_content.code_fix FIRST -- never auto-approve code changes without review."},
    "reject_evolution":       {"fn": t_reject_evolution,       "perm": "WRITE",   "args": ["evolution_id", "reason"],
                               "desc": "Reject a pending evolution by ID. reason=why rejected (improves future evolution quality). Use bulk_reject_evolutions for batch cleanup."},
    "bulk_reject_evolutions": {"fn": t_bulk_reject_evolutions, "perm": "WRITE",   "args": ["change_type", "ids", "reason", "include_synthesized"],
                               "desc": "Bulk reject pending evolutions silently. change_type=backlog|knowledge|empty, or comma-separated ids. include_synthesized=true to also reject synthesized items."},
    "gh_search_replace":      {"fn": t_gh_search_replace,      "perm": "EXECUTE", "args": ["path", "old_str", "new_str", "message", "repo", "dry_run", "allow_deletion"],
                               "desc": "Surgical find-and-replace in a GitHub file. old_str must be unique. DELETION GUARD: empty/missing new_str is blocked by default -- pass allow_deletion=true for intentional deletions. For .py files prefer patch_file. Always gh_read_lines first."},
    "gh_read_lines":          {"fn": t_gh_read_lines,          "perm": "READ",    "args": ["path", "start_line", "end_line", "repo"],
                               "desc": "Read specific line range from a GitHub file with line numbers. Use before any edit to get exact content. Preferred over read_file for large files or when you need a specific section."},
    "write_file":             {"fn": t_write_file,             "perm": "EXECUTE", "args": ["path", "content", "message", "repo"],
                               "desc": "Write NEW file to GitHub repo â€” FULL OVERWRITE. BLOCKED for core_main.py and core_tools.py. Use patch_file or gh_search_replace for surgical edits on existing files. Only use for brand new files."},
    "ask":                    {"fn": t_ask,                    "perm": "READ",    "args": ["question", "domain"],
                               "desc": "Ask CORE anything â€” searches KB + mistakes for context, then answers via Groq. Use for domain questions, SOPs, architectural decisions. Uses GROQ_MODEL for primary answers, with Gemini fallback."},
    "stats":                  {"fn": t_stats,                  "perm": "READ",    "args": [],
                               "desc": "Analytics dashboard: domain distribution, top patterns, mistake frequency, evolution queue counts by status, last cold processor run. Use at session start to orient or session end to summarize."},
    "search_mistakes":        {"fn": t_search_mistakes,        "perm": "READ",    "args": ["query", "domain", "limit"],
                               "desc": "Semantic mistake search by natural language query. Use when you want mistakes related to a concept (e.g. 'railway deploy'). Use get_mistakes for domain-filtered list."},
    "changelog_add":          {"fn": t_changelog_add,          "perm": "WRITE",   "args": ["version", "component", "summary", "before", "after", "change_type"],
                               "desc": "Log a completed change to the changelog table + Telegram notify. Call after every deploy. before/after describe what changed. change_type=bugfix|feature|config|refactor. Returns a verification packet."},
    "changelog_verification_packet": {"fn": t_changelog_verification_packet, "perm": "READ", "args": ["version", "component", "summary", "before", "after", "change_type"],
                               "desc": "Verify a changelog row exists and matches the canonical write contract."},
    "changelog_tracking_packet": {"fn": t_changelog_tracking_packet, "perm": "READ", "args": ["limit"],
                               "desc": "Canonical changelog tracking packet. Summarizes latest changelog rows, today rows, missing triggered_by fields, and source evidence."},
    "changelog_state_packet": {"fn": t_changelog_state_packet, "perm": "READ", "args": ["limit", "strict"],
                               "desc": "Canonical changelog state packet. Bundles changelog tracking with verification so downstream tools can reason about freshness and correctness."},
    "changelog_source_packet": {"fn": t_changelog_source_packet, "perm": "READ", "args": ["limit"],
                               "desc": "Return supporting source evidence for changelog context from sessions, hot_reflections, mistakes, and knowledge_base."},
    "mistake_tracking_packet": {"fn": t_mistake_tracking_packet, "perm": "READ", "args": ["limit"],
                               "desc": "Canonical mistake tracking packet. Summarizes recent mistakes, severity/domain mix, and missing context/root_cause/how_to_avoid fields."},
    "bulk_apply":             {"fn": t_bulk_apply,             "perm": "WRITE",   "args": ["executor_override", "dry_run"],
                               "desc": "Apply ALL pending evolution_queue items. executor_override=claude_desktop routes knowledge types to KB. dry_run=true shows plan without applying. Returns slim results to prevent overflow."},
    "list_templates":         {"fn": t_list_templates,         "perm": "READ",    "args": ["limit"],
                               "desc": "List script templates from script_templates table, ordered by use_count. Returns name, description, trigger_pattern. Check before starting a common task."},
    "run_template":           {"fn": t_run_template,           "perm": "EXECUTE", "args": ["name", "params"],
                               "desc": "Fetch script template by name, substitute params={\"key\": \"value\"}, increment use_count. Returns code with substitutions + execution instruction."},
    "redeploy":               {"fn": t_redeploy,               "perm": "EXECUTE", "args": ["reason"],
                               "desc": "Trigger CORE redeploy on Oracle VM: git pull latest from GitHub + restart service. Replaces Railway. Use after code push."},
    "logs":                   {"fn": t_logs,                   "perm": "READ",    "args": ["limit", "keyword"],
                               "desc": "Fetch recent GitHub commit history (NOT live stdout). limit=max commits (default 10). keyword=filter. For live stdout use railway_logs_live."},
    "deploy_status":          {"fn": t_deploy_status,          "perm": "READ",    "args": [],
                               "desc": "Oracle VM deploy status: compares VM git HEAD vs GitHub main. Returns commit_sha, status, description. For real-time status use build_status (Railway GQL)."},
    "build_status":           {"fn": t_build_status,           "perm": "READ",    "args": [],
                               "desc": "Oracle VM build/deploy status: checks if VM is running latest GitHub commit. (BUILDING|DEPLOYING|SUCCESS|FAILED|CRASHED). Short-circuits GitHub API calls on terminal status. Prefer over deploy_status."},
    "crash_report":           {"fn": t_crash_report,           "perm": "READ",    "args": [],
                               "desc": "Detect Railway restart loops. Scans last 5 commits for FAILED status in last 1h. Returns crash_count, loop_detected. Notifies owner + logs mistake if loop detected."},
    "review_evolutions":      {"fn": t_review_evolutions,      "perm": "READ",    "args": [],
                               "desc": "Returns URL to the Railway-hosted evolution review widget (https://<domain>/review). Use check_evolutions for in-session Groq-powered evolution brief instead."},
    "check_evolutions":       {"fn": t_check_evolutions,       "perm": "READ",    "args": ["limit"],
                               "desc": "Gemini-powered evolution brief. Returns priority_actions, new_tools_proposed (for review only -- NOT auto-saved), reject_ids, summary. Use at session start to get actionable evolution plan."},
    "search_in_file":         {"fn": t_search_in_file,         "perm": "READ",    "args": ["path", "pattern", "repo", "regex", "case_sensitive"],
                               "desc": "Search for pattern in a GitHub file."},
    "multi_patch":            {"fn": t_multi_patch,            "perm": "EXECUTE", "args": ["path", "patches", "message", "repo"],
                               "desc": "Apply multiple find-replace patches in one atomic commit. Near-miss protection: FAILS HARD if ANY patch skips -- rolled back, nothing pushed. Returns near_misses + not_found arrays with fix hints. Use patch_file for .py files (adds py_compile gate)."},
    "core_py_fn":             {"fn": t_core_py_fn,             "perm": "READ",    "args": ["fn_name", "file"],
                               "desc": "Read a single function from a CORE source file by name. file= param (default: core_tools.py). Pass file=core_train.py etc to read other modules."},
    "core_py_validate":       {"fn": t_core_py_validate,       "perm": "READ",    "args": [],
                               "desc": "Pre-deploy syntax checker for core_tools.py and core_main.py."},
    "system_map_scan":        {"fn": t_system_map_scan, "perm": "READ", "args": ["trigger"], "desc": "Scan system_map table. trigger=session_start|session_end|manual"},
    "session_start":          {"fn": t_session_start,          "perm": "READ",    "args": [],
                               "desc": "One-call session bootstrap. Returns: health, counts, resume_task (highest priority in_progress -- start here), in_progress_tasks, pending_tasks, recent_mistakes (last 10 all domains), stale_pattern_count, session_md (full SESSION.md static doc for claude.ai bootstrap), system_map. Use get_mistakes(domain=X) for domain-specific lookup before any write."},
    "session_snapshot":       {"fn": t_session_snapshot,       "perm": "READ",    "args": ["scope", "persist"],
                               "desc": "Canonical cross-session continuity snapshot. Captures health, counts, resume_task, checkpoint, quality, training, active goals, and capability context. Persisted into sessions.summary unless persist=false."},
    "session_state_packet":   {"fn": t_session_state_packet,   "perm": "READ",    "args": ["session_id", "strict", "limit"],
                               "desc": "Canonical state packet plus explicit [state_update] history and continuity metadata for session-state debugging."},
    "state_packet":           {"fn": t_state_packet,           "perm": "READ",    "args": ["session_id", "strict"],
                               "desc": "Canonical state continuity packet. Consolidates latest sessions row, agentic session scratchpad, checkpoint, session snapshot, and state_update history with verification metadata."},
    "state_consistency_check": {"fn": t_state_consistency_check, "perm": "READ",  "args": ["session_id", "strict"],
                               "desc": "Verify continuity across sessions, agentic_sessions, checkpoint, and state updates. Use when diagnosing drift or session collapse."},
    "system_verification_packet": {"fn": t_system_verification_packet, "perm": "READ", "args": ["session_id", "strict", "require_checkpoint", "task_sample_limit", "changelog_limit"],
                               "desc": "Canonical system-wide verification packet. Aggregates state, task, changelog, and continuity verification into one scorecard."},
    "code_read_packet":       {"fn": t_code_read_packet,       "perm": "READ",    "args": ["query", "files", "functions", "search_terms"],
                               "desc": "Canonical code-reading packet. Reads files, function bodies, and search hits into one structured packet for code_autonomy and owner review."},
    "repo_map_status":        {"fn": t_repo_map_status,        "perm": "READ",    "args": ["scope", "limit"],
                               "desc": "Canonical repository semantic map status. Returns repo component/chunk/edge counts and the latest scan run."},
    "repo_map_sync":          {"fn": t_repo_map_sync,          "perm": "EXECUTE", "args": ["trigger", "root_path"],
                               "desc": "Sync the CORE semantic repository map into Supabase. Scans files, chunks, and wiring edges."},
    "repo_component_packet":  {"fn": t_repo_component_packet,  "perm": "READ",    "args": ["path", "query", "limit"],
                               "desc": "Build a semantic packet for a file/component or repo query from the CORE repository map."},
    "repo_graph_packet":      {"fn": t_repo_graph_packet,      "perm": "READ",    "args": ["path", "query", "depth", "limit"],
                               "desc": "Build a dependency graph packet from the CORE repository map."},
    "public_evidence_packet": {"fn": t_public_evidence_packet, "perm": "READ",    "args": ["query", "domain", "request_kind", "code_targets"],
                               "desc": "Classify the public evidence family for a query. Returns family, sources, and retrieval hints."},
    "owner_review_cluster_packet": {"fn": t_owner_review_cluster_packet, "perm": "READ", "args": ["limit", "persist"],
                               "desc": "Canonical owner-review cluster packet. Use this to inspect one batchable owner-only cluster before deciding whether to apply or reject it."},
    "owner_review_cluster_close": {"fn": t_owner_review_cluster_close, "perm": "WRITE", "args": ["cluster_id", "cluster_key", "outcome", "reason", "reviewed_by", "dry_run"],
                               "desc": "Batch-close one owner-review cluster by cluster_id or cluster_key after verification. outcome=applied|rejected. Do not guess cluster membership; inspect the cluster packet first."},
    "tool_stats":             {"fn": t_tool_stats,             "perm": "READ",    "args": ["days"],
                               "desc": "TASK-26: Per-tool success/fail rate for last N days (default 7). Returns tools sorted by fail_rate desc. fail_rate>0.2 = flagged. Use to identify flaky tools."},
    "checkpoint":             {"fn": t_checkpoint,             "perm": "WRITE",   "args": ["active_task_id", "last_action", "last_result"],
                               "desc": "TASK-28: Write mid-session checkpoint. Call after every subtask gate to prevent context collapse on long tasks. active_task_id=UUID of current task. last_action=brief description of last completed step. last_result=outcome or next step. session_start returns resume_checkpoint field with this data."},
    "session_end":            {"fn": t_session_end,            "perm": "WRITE",   "args": ["summary", "actions", "domain", "patterns", "quality", "skill_file_updated", "force_close", "active_task_ids", "owner_corrections"],
                               "desc": "One-call session close. actions=pipe-separated strings (| only, NOT comma). quality clamped 0.0-1.0 auto. BEFORE calling: (1) log_mistake for every error, (2) add_knowledge for every new insight, (3) changelog_add for every deploy, (4) update task statuses in task_queue. active_task_ids=pipe-separated UUIDs of tasks touched -- warns if any still pending/in_progress. skill_file_updated gate RETIRED (owner 2026-03-19) -- always pass skill_file_updated=true. All new rules go to Supabase behavioral_rules via add_behavioral_rule only. Returns: training_ok (bool), duration_seconds, reflection_warning if hot reflection failed, task_status_warnings if tasks left open."},
    "agent_session_init": {"fn": t_agent_session_init, "perm": "WRITE",
        "args": ["session_id", "goal", "chat_id"],
        "desc": "Create/reset agentic session with clean state scratchpad. Call at START of any multi-step task. Prevents step bleed between runs."},
    "agent_state_set":    {"fn": t_agent_state_set,    "perm": "WRITE",
        "args": ["session_id", "key", "value"],
        "desc": "Write named variable to agentic session scratchpad. Use after every step that produces data used later (e.g. agent_state_set(key='kb_id', value='9789')). Solves cross-iteration memory loss."},
    "agent_state_get":    {"fn": t_agent_state_get,    "perm": "READ",
        "args": ["session_id", "key"],
        "desc": "Read named variable from agentic session scratchpad. Use INSTEAD of re-querying when you already inserted/found data in a previous step."},
    "agent_step_done":    {"fn": t_agent_step_done,    "perm": "WRITE",
        "args": ["session_id", "step_name", "result"],
        "desc": "Mark a step complete with its result. Prevents re-running completed steps. Call after EVERY successful step."},
    "core_py_rollback":       {"fn": t_core_py_rollback,       "perm": "EXECUTE", "args": ["commit_sha"],
                               "desc": "Emergency restore: fetch any CORE file at commit_sha, write back, redeploy. file= param (default: core_main.py)."},
    "diff":                   {"fn": t_diff,                   "perm": "READ",    "args": ["path", "sha_a", "sha_b"],
                               "desc": "Compare file between two commits."},
    "deploy_and_wait":        {"fn": t_deploy_and_wait,        "perm": "EXECUTE", "args": ["reason", "timeout"],
                               "desc": "Trigger redeploy + poll until success/failure."},
    "synthesize_evolutions":  {"fn": t_synthesize_evolutions,  "perm": "READ",    "args": [],
                                 "desc": "Pure signal fetcher for Claude Desktop architect synthesis. Call when owner says 'synthesize'. Returns all accumulated signals: pending evolutions, top patterns, cold reflection themes, hot gaps, open tasks. NO Groq. NO auto task insertion. Claude reads the signals in chat, acts as architect thinking 6 months ahead, then calls task_add for each new task directly. Groq owns hot_reflection + cold_processor. Claude owns synthesis and blueprint."},
    "project_list":           {"fn": t_project_list,           "perm": "READ",    "args": [],
                               "desc": "List all registered projects: project_id, name, status, last_indexed, folder_path."},

    "project_get":            {"fn": t_project_get,            "perm": "READ",    "args": ["project_ids"],
                               "desc": "Load full context for one or more projects. project_ids=comma-separated slugs. Returns context_md ready for session injection."},
    "project_search":         {"fn": t_project_search,         "perm": "READ",    "args": ["project_id", "query"],
                               "desc": "Search KB entries for a specific project by query string."},
    "project_context_check":  {"fn": t_project_context_check,  "perm": "READ",    "args": [],
                               "desc": "Check for unconsumed prepared project contexts waiting for Claude Desktop."},
    "project_register":       {"fn": t_project_register,       "perm": "WRITE",   "args": ["project_id", "name", "folder_path", "index_path"],
                               "desc": "Register a new project in Supabase projects table."},
    "project_update_kb":      {"fn": t_project_update_kb,      "perm": "WRITE",   "args": ["project_id", "topic", "content", "confidence"],
                               "desc": "Add or update a KB entry for a project. domain=project:{project_id}."},
    "project_update_index":   {"fn": t_project_update_index,   "perm": "WRITE",   "args": ["project_id", "last_indexed"],
                               "desc": "Update last_indexed timestamp for a project in Supabase."},
    "project_index":          {"fn": t_project_index,          "perm": "WRITE",   "args": ["project_id", "topic", "content", "notify"],
                               "desc": "Index content into a project KB. Writes topic+content chunk to domain=project:{id} and updates last_indexed. If no content, timestamp-only ping. notify=true sends Telegram."},
    "read_image_content":     {"fn": t_read_image_content,     "perm": "READ",    "args": ["content_b64", "mime_type", "project_id", "topic", "prompt", "file_name"],
                               "desc": "Extract text/data from image (JPG/PNG) using Claude vision (claude-haiku). content_b64=base64 image bytes. If project_id+topic provided, auto-saves to project KB. Returns extracted_text."},
    "read_pdf_content":       {"fn": t_read_pdf_content,       "perm": "READ",    "args": ["content_b64", "project_id", "topic", "file_name", "max_chars"],
                               "desc": "Extract text from PDF using pdfminer.six. content_b64=base64 PDF bytes. If project_id+topic provided, auto-saves to project KB. Returns extracted_text, pages."},
    "project_prepare":        {"fn": t_project_prepare,        "perm": "WRITE",   "args": ["project_ids"],
                               "desc": "Railway-side: assemble KB context for project(s) and store in project_context for Claude Desktop to consume. Sends Telegram notify."},
    "project_consume":        {"fn": t_project_consume,        "perm": "WRITE",   "args": ["project_id"],
                               "desc": "Mark project_context rows as consumed after Claude Desktop has loaded them."},
    "predict_failure":        {"fn": t_predict_failure,        "perm": "READ",    "args": ["operation", "context", "domain", "session_id"],
                               "desc": "AGI-03: Causal failure predictor. Dual-pass search (what_failed + root_cause). Returns predicted_failure_modes=[{mode,probability,root_cause,prevention}] causal chain. Writes prediction to causal_predictions table for counterfactual analysis. session_id optional."},
    "ping_health":            {"fn": t_ping_health,            "perm": "READ",    "args": [],
                               "desc": "Hit live Railway / endpoint."},
    "patch_file":             {"fn": t_patch_file,            "perm": "EXECUTE", "args": ["path", "patches", "message", "repo", "dry_run", "allow_deletion"],
                               "desc": "Server-side patch with near-miss protection. Fetches file, applies find-replace patches, py_compile check, pushes. FAILS HARD if ANY patch skips (near_miss or not_found) -- partial application is blocked and rolled back. On near_miss: returns fix_hint telling you to call gh_read_lines and copy old_str exactly. 7 normalization tiers before giving up. Use instead of multi_patch for .py files."},
    "validate_syntax":        {"fn": t_validate_syntax,        "perm": "READ",    "args": ["path", "repo"],
                               "desc": "Fetch a .py file from GitHub and run py_compile server-side. Returns ok/error with line number. Use before any deploy."},
    "append_to_file":         {"fn": t_append_to_file,         "perm": "EXECUTE", "args": ["path", "content_to_append", "message", "repo"],
                               "desc": "Append content to a GitHub file server-side. Runs py_compile before push for .py files. Use to add new functions without fetching file into Claude context."},
    "verify_live":            {"fn": t_verify_live,            "perm": "READ",    "args": ["expected_text", "timeout"],
                               "desc": "Poll /state until expected_text appears."},
    "sb_patch":               {"fn": t_sb_patch,               "perm": "WRITE",   "args": ["table", "filters", "data"],
                               "desc": "Update rows in a Supabase table. filters=PostgREST filter string (e.g. id=eq.abc123) REQUIRED -- rejected if empty. data=JSON fields to update e.g. {\"status\": \"done\"}. Returns updated_fields list. Use for: updating task status, marking flags, changing any field. Never call without filters."},
    "sb_upsert":              {"fn": t_sb_upsert,              "perm": "WRITE",   "args": ["table", "data", "on_conflict"],
                               "desc": "Insert a row or update it if already exists. on_conflict=column(s) defining uniqueness (e.g. domain,topic for knowledge_base -- project_id for projects -- name for script_templates). Use instead of sb_insert when you want to avoid duplicates or update an existing entry. data=full row as JSON."},
    "sb_delete":              {"fn": t_sb_delete,              "perm": "WRITE",   "args": ["table", "filters", "confirm"],
                               "desc": "Delete rows from a Supabase table. filters=PostgREST filter string REQUIRED. confirm=DELETE (literal string) to execute -- omit for dry run showing rows that would be deleted. PROTECTED tables (sessions, mistakes, hot_reflections, cold_reflections, pattern_frequency, changelog, evolution_queue) cannot be deleted from. ALLOWED: knowledge_base, task_queue, project_context, script_templates, system_map, projects. Always dry-run first."},
}


# -- MCP JSON-RPC handler ------------------------------------------------------
def _mcp_tool_schema(name, tool):
    # Params that should be typed as array (not string) in MCP schema
    _ARRAY_PARAMS = {"patches", "project_ids", "ids", "files", "edits", "actions"}
    props = {}
    for a in tool["args"]:
        # Support both plain string args and rich dict args {"name": x, "type": y, "description": z}
        if isinstance(a, dict):
            arg_name = a.get("name", str(a))
            arg_type = a.get("type", "string")
            arg_desc = a.get("description", arg_name)
            if arg_name in _ARRAY_PARAMS or arg_type == "array":
                props[arg_name] = {
                    "type": "array",
                    "description": arg_desc,
                    "items": {"type": "object"}
                }
            else:
                props[arg_name] = {"type": arg_type, "description": arg_desc}
        else:
            if a in _ARRAY_PARAMS:
                props[a] = {
                    "type": "array",
                    "description": a,
                    "items": {"type": "object"}
                }
            else:
                props[a] = {"type": "string", "description": a}
    return {"name": name, "description": tool.get("desc", name),
            "inputSchema": {"type": "object", "properties": props}}


def _mcp_tool_result_is_failure(result) -> tuple[bool, str]:
    """Classify MCP tool results that signal failure without raising.

    Many tools return structured dicts with ok=false instead of throwing.
    The JSON-RPC dispatcher should treat those as failures for telemetry and
    operator visibility, even though the transport still returns a valid result.
    """
    if not isinstance(result, dict):
        return False, ""
    if result.get("ok") is False:
        msg = result.get("error") or result.get("message") or result.get("error_code") or "tool returned ok=false"
        return True, str(msg)[:240]
    if result.get("error_code") or result.get("error"):
        msg = result.get("error") or result.get("message") or result.get("error_code") or "tool returned error payload"
        return True, str(msg)[:240]
    return False, ""


def handle_jsonrpc(body: dict, session_id: str = "") -> dict:
    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")
    def ok(r):     return {"jsonrpc": "2.0", "id": req_id, "result": r}
    def err(c, m): return {"jsonrpc": "2.0", "id": req_id, "error": {"code": c, "message": m}}

    if method == "initialize":
        return ok({"protocolVersion": MCP_PROTOCOL_VERSION,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "CORE v6.0", "version": "6.0"}})
    elif method == "notifications/initialized": return None
    elif method == "ping": return ok({})
    elif method == "tools/list":
        return ok({"tools": [_mcp_tool_schema(n, t) for n, t in TOOLS.items()]})
    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        if tool_name not in TOOLS:
            return err(-32601, f"Unknown tool: {tool_name}")
        tool = TOOLS[tool_name]
        from core_config import L
        if not L.mcp(session_id):
            return err(-32000, "Rate limit exceeded")
        try:
            # TASK-23.D: Coerce numeric string args to int/float at dispatcher level
            _INT_ARGS = {"start_line", "end_line", "limit", "hours", "lines", "priority", "max_results", "since_days", "max_per_source"}
            _FLOAT_ARGS = {"confidence", "quality"}
            coerced_args = {}
            for k, v in tool_args.items():
                if k in _INT_ARGS and isinstance(v, str):
                    try: coerced_args[k] = int(v)
                    except (ValueError, TypeError): coerced_args[k] = v
                elif k in _FLOAT_ARGS and isinstance(v, str):
                    try: coerced_args[k] = float(v)
                    except (ValueError, TypeError): coerced_args[k] = v
                else:
                    coerced_args[k] = v
            arg_names = {d["name"] if isinstance(d, dict) else d for d in tool["args"]} if tool["args"] else set()
            result = tool["fn"](**{k: v for k, v in coerced_args.items() if not arg_names or k in arg_names})
            failed, failure_msg = _mcp_tool_result_is_failure(result)
            text = json.dumps(result, default=str)
            # TASK-26.B: Track success in tool_stats (fire-and-forget, non-fatal)
            _track_tool_stat(tool_name, success=not failed, error=failure_msg if failed else "")
            return ok({"content": [{"type": "text", "text": text}]})
        except Exception as e:
            # TASK-26.B: Track failure in tool_stats (fire-and-forget, non-fatal)
            _track_tool_stat(tool_name, success=False, error=str(e)[:200])
            return err(-32603, f"Tool error: {e}")
    return err(-32601, f"Unknown method: {method}")

# -- brain layer reconciliation ------------------------------------------------

def _reconcile_brain_tables(rows: list, inserted: list, tombstoned: list) -> None:
    """Auto-sync brain layer: diff live Supabase tables vs system_map brain entries.
    Uses management API POST /v1/projects/{ref}/database/query with SUPABASE_PAT.
    PAT is stored in knowledge_base (domain=system.config, topic=supabase_pat).
    Inserts new tables, tombstones dropped tables. Skips already-tombstoned entries.
    Called from t_system_map_scan(trigger='session_end') only.
    """
    try:
        # Fetch PAT from KB (never hardcoded in source)
        pat_rows = sb_get(
            "knowledge_base",
            "select=content&domain=eq.system.config&topic=eq.supabase_pat&limit=1",
            svc=True
        )
        if not pat_rows or not pat_rows[0].get("content"):
            print("[SMAP] brain reconcile skipped: supabase_pat not found in KB")
            return
        pat = pat_rows[0]["content"].strip()
        ref = SUPABASE_REF

        # Query live tables from management API
        mgmt_resp = httpx.post(
            f"https://api.supabase.com/v1/projects/{ref}/database/query",
            headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json"},
            json={"query": "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"},
            timeout=15
        )
        mgmt_resp.raise_for_status()
        live_tables = {row["table_name"] for row in mgmt_resp.json()}

        # Get registered brain table entries (active only)
        registered_brain = {
            row["name"]: row
            for row in rows
            if row.get("layer") == "brain"
            and row.get("item_type") == "table"
            and row.get("status") != "tombstone"
        }
        registered_names = set(registered_brain.keys())

        # Insert tables present in DB but missing from system_map
        missing = live_tables - registered_names
        for tname in sorted(missing):
            try:
                sb_post_critical("system_map", {
                    "layer": "brain",
                    "component": "supabase",
                    "item_type": "table",
                    "name": tname,
                    "role": f"Supabase table: {tname}",
                    "responsibility": "auto-registered by brain reconciliation",
                    "status": "active",
                    "updated_by": "session_end_auto",
                    "last_updated": datetime.utcnow().isoformat(),
                })
                inserted.append(f"brain:{tname}")
            except Exception as _ie:
                print(f"[SMAP] brain insert {tname} failed: {_ie}")

        # Tombstone tables in system_map that no longer exist in DB
        removed = registered_names - live_tables
        for tname in sorted(removed):
            try:
                row_id = registered_brain[tname]["id"]
                sb_patch("system_map", f"id=eq.{row_id}", {
                    "status": "tombstone",
                    "notes": "auto-tombstoned by brain reconciliation: table not found in DB",
                    "last_updated": datetime.utcnow().isoformat(),
                    "updated_by": "session_end_auto",
                })
                tombstoned.append(f"brain:{tname}")
            except Exception as _te:
                print(f"[SMAP] brain tombstone {tname} failed: {_te}")

    except Exception as e:
        print(f"[SMAP] _reconcile_brain_tables error: {e}")


# -- executor source file reconciliation ---------------------------------------

def _reconcile_executor_files(rows: list, inserted: list, tombstoned: list) -> None:
    """Auto-sync executor layer: diff live .py files in GitHub repo root vs
    system_map executor file entries. Inserts new files, tombstones removed ones.
    Uses GitHub API list repo contents -- no GITHUB_PAT scope issues.
    Called from t_system_map_scan(trigger='session_end') only.
    """
    try:
        h = _ghh()
        r = httpx.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/",
            headers=h, timeout=10
        )
        r.raise_for_status()
        live_py = {
            item["name"] for item in r.json()
            if item.get("type") == "file" and item["name"].endswith(".py")
        }

        registered_files = {
            row["name"]: row
            for row in rows
            if row.get("layer") == "executor"
            and row.get("item_type") == "file"
            and row.get("status") != "tombstone"
        }
        registered_names = set(registered_files.keys())

        # Insert .py files present in repo but missing from system_map
        missing = live_py - registered_names
        for fname in sorted(missing):
            try:
                sb_upsert("system_map", {
                    "layer": "executor",
                    "component": "railway",
                    "item_type": "file",
                    "name": fname,
                    "role": f"Source file: {fname}",
                    "responsibility": "auto-registered by executor file reconciliation",
                    "status": "active",
                    "updated_by": "session_end_auto",
                    "last_updated": datetime.utcnow().isoformat(),
                }, on_conflict="name,component,item_type")
                inserted.append(f"executor:{fname}")
            except Exception as _ie:
                print(f"[SMAP] executor insert {fname} failed: {_ie}")

        # Tombstone files in system_map that no longer exist in repo
        removed = registered_names - live_py
        for fname in sorted(removed):
            try:
                row_id = registered_files[fname]["id"]
                sb_patch("system_map", f"id=eq.{row_id}", {
                    "status": "tombstone",
                    "notes": "auto-tombstoned by executor reconciliation: file not found in repo root",
                    "last_updated": datetime.utcnow().isoformat(),
                    "updated_by": "session_end_auto",
                })
                tombstoned.append(f"executor:{fname}")
            except Exception as _te:
                print(f"[SMAP] executor tombstone {fname} failed: {_te}")

    except Exception as e:
        print(f"[SMAP] _reconcile_executor_files error: {e}")


# -- skeleton doc reconciliation -----------------------------------------------

def _reconcile_skeleton_docs(rows: list, inserted: list, tombstoned: list) -> None:
    """Auto-sync skeleton layer: diff live .md and .json files in GitHub repo root
    vs system_map skeleton file entries. Inserts new docs, tombstones removed ones.
    Called from t_system_map_scan(trigger='session_end') only.
    """
    try:
        h = _ghh()
        r = httpx.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/",
            headers=h, timeout=10
        )
        r.raise_for_status()
        live_docs = {
            item["name"] for item in r.json()
            if item.get("type") == "file"
            and (item["name"].endswith(".md") or item["name"].endswith(".json")
                 or item["name"].endswith(".txt"))
        }

        registered_docs = {
            row["name"]: row
            for row in rows
            if row.get("layer") == "skeleton"
            and row.get("item_type") == "file"
            and row.get("status") != "tombstone"
        }
        registered_names = set(registered_docs.keys())

        # Insert docs present in repo but missing from system_map
        missing = live_docs - registered_names
        for dname in sorted(missing):
            try:
                sb_post_critical("system_map", {
                    "layer": "skeleton",
                    "component": "github",
                    "item_type": "file",
                    "name": dname,
                    "role": f"Repository file: {dname}",
                    "responsibility": "auto-registered by skeleton doc reconciliation",
                    "status": "active",
                    "updated_by": "session_end_auto",
                    "last_updated": datetime.utcnow().isoformat(),
                })
                inserted.append(f"skeleton:{dname}")
            except Exception as _ie:
                print(f"[SMAP] skeleton insert {dname} failed: {_ie}")

        # Tombstone docs in system_map that no longer exist in repo
        removed = registered_names - live_docs
        for dname in sorted(removed):
            try:
                row_id = registered_docs[dname]["id"]
                sb_patch("system_map", f"id=eq.{row_id}", {
                    "status": "tombstone",
                    "notes": "auto-tombstoned by skeleton reconciliation: file not found in repo root",
                    "last_updated": datetime.utcnow().isoformat(),
                    "updated_by": "session_end_auto",
                })
                tombstoned.append(f"skeleton:{dname}")
            except Exception as _te:
                print(f"[SMAP] skeleton tombstone {dname} failed: {_te}")

    except Exception as e:
        print(f"[SMAP] _reconcile_skeleton_docs error: {e}")



# â”€â”€ TASK-4: Binance Integration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

import hmac as _hmac
import hashlib as _hashlib
import time as _time
import urllib.parse as _urlparse

def _binance_sign(secret: str, params: dict) -> str:
    """Sign Binance request with HMAC-SHA256."""
    qs = _urlparse.urlencode(params)
    return _hmac.new(secret.encode(), qs.encode(), _hashlib.sha256).hexdigest()

def _binance_headers(api_key: str) -> dict:
    return {"X-MBX-APIKEY": api_key, "Content-Type": "application/x-www-form-urlencoded"}

def _binance_get(path: str, params: dict = None) -> dict:
    """Unsigned Binance GET (public endpoints)."""
    import httpx as _httpx
    try:
        r = _httpx.get(f"https://api.binance.com{path}", params=params or {}, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def _binance_signed_get(path: str, params: dict) -> dict:
    """Signed Binance GET (account endpoints)."""
    import httpx as _httpx
    api_key = os.getenv("BINANCE_API_KEY", "")
    secret  = os.getenv("BINANCE_SECRET_KEY", "")
    if not api_key or not secret:
        return {"error": "BINANCE_API_KEY or BINANCE_SECRET_KEY not set"}
    params["timestamp"] = int(_time.time() * 1000)
    params["signature"] = _binance_sign(secret, params)
    try:
        r = _httpx.get(f"https://api.binance.com{path}", params=params,
                       headers=_binance_headers(api_key), timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def _binance_signed_post(path: str, params: dict) -> dict:
    """Signed Binance POST (order endpoints)."""
    import httpx as _httpx
    api_key = os.getenv("BINANCE_API_KEY", "")
    secret  = os.getenv("BINANCE_SECRET_KEY", "")
    if not api_key or not secret:
        return {"error": "BINANCE_API_KEY or BINANCE_SECRET_KEY not set"}
    params["timestamp"] = int(_time.time() * 1000)
    params["signature"] = _binance_sign(secret, params)
    try:
        r = _httpx.post(f"https://api.binance.com{path}", data=params,
                        headers=_binance_headers(api_key), timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def t_crypto_price(symbol: str = "BTCUSDT") -> dict:
    """Get current Binance spot price for a symbol. symbol=e.g. BTCUSDT, BNBUSDT, ETHUSDT."""
    try:
        symbol = symbol.upper().replace("/", "").replace("-", "")
        data = _binance_get("/api/v3/ticker/price", {"symbol": symbol})
        if "error" in data:
            return {"ok": False, "error": data["error"]}
        if "price" not in data:
            return {"ok": False, "error": f"Symbol not found or invalid: {symbol}", "raw": data}
        price = float(data["price"])
        stats = _binance_get("/api/v3/ticker/24hr", {"symbol": symbol})
        result = {"ok": True, "symbol": symbol, "price": price}
        if "priceChangePercent" in stats:
            result["change_24h_pct"] = float(stats["priceChangePercent"])
            result["high_24h"] = float(stats["highPrice"])
            result["low_24h"] = float(stats["lowPrice"])
            result["volume_24h"] = float(stats["volume"])
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_current_price_in_usd(symbol: str = "BTCUSDT") -> dict:
    """Return a normalized current_price_in_usd packet for price calculations."""
    try:
        result = t_crypto_price(symbol=symbol)
        if not result.get("ok"):
            return result
        price = float(result.get("price") or 0.0)
        result["current_price_in_usd"] = price
        result["summary"] = f"{result.get('symbol') or symbol} current_price_in_usd={price:.8f}"
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}




def t_crypto_balance(asset: str = "") -> dict:
    """Get Binance account balances. asset=optional filter e.g. BTC. Returns all non-zero balances if empty."""
    try:
        data = _binance_signed_get("/api/v3/account", {"recvWindow": 5000})
        if "error" in data:
            return {"ok": False, "error": data["error"]}
        if "balances" not in data:
            return {"ok": False, "error": "Unexpected response", "raw": data}
        balances = [
            {"asset": b["asset"], "free": float(b["free"]), "locked": float(b["locked"])}
            for b in data["balances"]
            if float(b["free"]) > 0 or float(b["locked"]) > 0
        ]
        if asset:
            balances = [b for b in balances if b["asset"].upper() == asset.upper()]
        return {"ok": True, "balances": balances, "count": len(balances)}
    except Exception as e:
        return {"ok": False, "error": str(e)}




def t_crypto_trade(symbol: str = "", side: str = "", quantity: str = "",
                   confirm: str = "", order_type: str = "MARKET") -> dict:
    """Execute a Binance spot trade. REQUIRES confirm='CONFIRM' to execute.
    symbol=e.g. BTCUSDT. side=BUY or SELL. quantity=amount of BASE asset.
    order_type=MARKET (default) or LIMIT (requires price param).
    NEVER executes without explicit confirm='CONFIRM' from owner."""
    if not symbol or not side or not quantity:
        return {"ok": False, "error": "symbol, side, and quantity are all required"}
    if confirm != "CONFIRM":
        # Dry run -- show what would be executed
        price_data = t_crypto_price(symbol)
        est_price = price_data.get("price", "unknown")
        est_value = float(quantity) * float(est_price) if est_price != "unknown" else "unknown"
        return {
            "ok": False,
            "dry_run": True,
            "message": f"DRY RUN -- pass confirm='CONFIRM' to execute",
            "would_execute": {
                "symbol": symbol.upper(),
                "side": side.upper(),
                "quantity": quantity,
                "order_type": order_type,
                "estimated_price_usdt": est_price,
                "estimated_value_usdt": est_value,
            }
        }
    # Execute trade
    symbol = symbol.upper().replace("/", "").replace("-", "")
    side   = side.upper()
    if side not in ("BUY", "SELL"):
        return {"ok": False, "error": "side must be BUY or SELL"}
    params = {
        "symbol":   symbol,
        "side":     side,
        "type":     order_type.upper(),
        "quantity": quantity,
    }
    result = _binance_signed_post("/api/v3/order", params)
    if "orderId" not in result:
        return {"ok": False, "error": "Order failed", "raw": result}
    # Log to Supabase trades table
    try:
        fills = result.get("fills", [])
        avg_price = (
            sum(float(f["price"]) * float(f["qty"]) for f in fills) /
            sum(float(f["qty"]) for f in fills)
        ) if fills else None
        sb_post("trades", {
            "order_id":    str(result["orderId"]),
            "symbol":      symbol,
            "side":        side,
            "quantity":    float(quantity),
            "price":       avg_price,
            "status":      result.get("status", "UNKNOWN"),
            "confirmed_by": "ki",
            "raw_response": result,
        })
    except Exception as log_err:
        print(f"[BINANCE] trade log error: {log_err}")
    notify(f"[TRADE EXECUTED] {side} {quantity} {symbol}\nOrder ID: {result['orderId']}\nStatus: {result.get('status')}")
    return {
        "ok":       True,
        "order_id": result["orderId"],
        "symbol":   symbol,
        "side":     side,
        "quantity": quantity,
        "status":   result.get("status"),
        "fills":    result.get("fills", []),
    }



# â”€â”€ TASK-4: Register Binance tools in TOOLS dict (must be after function defs) â”€
TOOLS["crypto_price"]   = {"fn": t_crypto_price,   "perm": "READ",    "args": ["symbol"],
                            "desc": "Get current Binance spot price + 24h stats for a symbol. symbol=e.g. BTCUSDT, ETHUSDT, BNBUSDT (default BTCUSDT). No API key required. Returns price, 24h change %, high, low, volume."}
TOOLS["current_price_in_usd"] = {"fn": t_current_price_in_usd, "perm": "READ", "args": ["symbol"],
                            "desc": "Normalized current_price_in_usd alias for BTC/crypto price calculations. Returns the live spot price under a stable current_price_in_usd field."}
TOOLS["crypto_balance"] = {"fn": t_crypto_balance, "perm": "READ",    "args": ["asset"],
                            "desc": "Get Binance account balances. Requires BINANCE_API_KEY + BINANCE_SECRET_KEY env vars. asset=optional filter e.g. BTC. Returns all non-zero balances if asset empty."}
TOOLS["crypto_trade"]   = {"fn": t_crypto_trade,   "perm": "EXECUTE", "args": ["symbol", "side", "quantity", "confirm", "order_type"],
                            "desc": "Execute a Binance spot trade. REQUIRES confirm=CONFIRM to execute -- omit for dry run showing estimated value. symbol=e.g. BTCUSDT. side=BUY or SELL. quantity=base asset amount. order_type=MARKET (default) or LIMIT. Logs all trades to Supabase trades table. Sends Telegram notify on execution. NEVER execute without owner CONFIRM."}


# -- TASK-5.2: Task State Validator ------------------------------------------

def t_task_health() -> dict:
    """Check task_queue for stale/abandoned tasks.
    Flags: in_progress tasks older than 24hr, pending tasks with no updates older than 7 days.
    Returns: {ok, stale_in_progress: [...], stale_pending: [...], total_stale, warning}
    Called at session_start to surface abandoned work before starting new tasks."""
    try:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        threshold_in_progress = now - timedelta(hours=24)
        threshold_pending = now - timedelta(days=7)

        # Fetch in_progress tasks older than 24hr (UUID PK -- no id=gt.1 filter)
        stale_ip_rows = sb_get(
            "task_queue",
            "select=id,task,status,created_at,updated_at&status=eq.in_progress&order=created_at.asc",
            svc=True
        ) or []

        # Fetch pending tasks older than 7 days (UUID PK -- no id=gt.1 filter)
        stale_pend_rows = sb_get(
            "task_queue",
            "select=id,task,status,created_at,updated_at&status=eq.pending&order=created_at.asc",
            svc=True
        ) or []

        stale_in_progress = []
        for row in stale_ip_rows:
            ts_str = row.get("updated_at") or row.get("created_at") or ""
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts < threshold_in_progress:
                    task_raw = row.get("task", "")
                    title = task_raw[:80] if isinstance(task_raw, str) else str(task_raw)[:80]
                    hours_stale = int((now - ts).total_seconds() / 3600)
                    stale_in_progress.append({
                        "id": str(row.get("id", "")),
                        "title": title,
                        "hours_stale": hours_stale,
                        "last_update": ts_str,
                    })
            except Exception:
                pass

        stale_pending = []
        for row in stale_pend_rows:
            ts_str = row.get("updated_at") or row.get("created_at") or ""
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts < threshold_pending:
                    task_raw = row.get("task", "")
                    title = task_raw[:80] if isinstance(task_raw, str) else str(task_raw)[:80]
                    days_stale = int((now - ts).total_seconds() / 86400)
                    stale_pending.append({
                        "id": str(row.get("id", "")),
                        "title": title,
                        "days_stale": days_stale,
                        "last_update": ts_str,
                    })
            except Exception:
                pass

        total_stale = len(stale_in_progress) + len(stale_pending)
        warning = None
        if total_stale > 0:
            parts = []
            if stale_in_progress:
                parts.append(f"{len(stale_in_progress)} in_progress task(s) stuck >24hr")
            if stale_pending:
                parts.append(f"{len(stale_pending)} pending task(s) untouched >7d")
            warning = "STALE TASKS: " + ", ".join(parts) + ". Review before starting new work."

        return {
            "ok": True,
            "stale_in_progress": stale_in_progress,
            "stale_pending": stale_pending,
            "total_stale": total_stale,
            "warning": warning,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}


TOOLS["task_health"] = {"fn": t_task_health, "perm": "READ", "args": [],
                         "desc": "TASK-5.2: Check task_queue for stale/abandoned tasks. Flags: in_progress >24hr, pending >7d. Returns stale_in_progress, stale_pending, total_stale, warning. Call at session_start to surface abandoned work before starting new tasks."}


# -- TASK-6: Mandatory Verification Gate System --------------------------------

def t_verify_before_deploy(
    operation: str = "",
    target_file: str = "",
    context: str = "",
    assumed_state: str = "",
    sources: str = "supabase",
    action_type: str = "deploy",
    owner_token: str = "",
) -> dict:
    """TASK-6.1: Pre-deploy verification enforcer.
    Checks multiple gates before any deploy/write:
      (a) validate_syntax
      (b) target file specified
      (c) predict_failure
      (d) trust calibration
      (e) action reversibility gate
      (f) optional external-state verification if assumed_state is provided
    Returns: verification_score 0.0-1.0, passed_gates list, failed_gates list, blocked (True if score < 0.8).
    HARD RULE: Never deploy without calling this first. Score < 0.8 = do not proceed."""
    try:
        passed = []
        failed = []
        warnings = []

        # Gate A: validate_syntax on target file
        if target_file and target_file.endswith(".py"):
            try:
                syn = t_validate_syntax(target_file)
                if syn.get("ok"):
                    passed.append("validate_syntax")
                else:
                    failed.append(f"validate_syntax: {syn.get('error', 'failed')}")
            except Exception as e:
                failed.append(f"validate_syntax: exception({e})")
        else:
            # Non-Python file or no file specified -- syntax check not applicable
            passed.append("validate_syntax_skipped_non_py")

        # Gate B: target file was read before patch (heuristic -- check if target_file provided)
        if target_file:
            passed.append("target_file_specified")
        else:
            failed.append("target_file_not_specified: always name the file being patched")

        # Gate C: predict_failure check
        try:
            pf = t_predict_failure(operation=operation or "deploy", context=context or target_file, domain="core_agi.deployment")
            pf_warnings = pf.get("warnings", [])
            if pf_warnings:
                warnings.extend(pf_warnings[:3])
                # Warnings are non-blocking but lower score
                passed.append(f"predict_failure_warnings({len(pf_warnings)})")
            else:
                passed.append("predict_failure_clean")
        except Exception as e:
            # predict_failure failure is non-blocking
            passed.append(f"predict_failure_unavailable({e})")

        # Gate D: trust calibration
        try:
            trust = t_trust_map(action_type or "deploy")
            if trust.get("ok") and trust.get("verification_required") is False:
                passed.append(f"trust_map_{trust.get('action_type', action_type or 'deploy')}_low_risk")
            elif trust.get("ok"):
                passed.append(f"trust_map_{trust.get('action_type', action_type or 'deploy')}_{trust.get('risk_level', 'unknown')}")
            else:
                failed.append(f"trust_map: {trust.get('error', 'failed')}")
        except Exception as e:
            failed.append(f"trust_map: exception({e})")

        # Gate E: action reversibility gate
        try:
            act = t_action_gate(action=operation or target_file or context or action_type, owner_token=owner_token)
            if act.get("blocked"):
                failed.append(f"action_gate: blocked({act.get('classification', 'unknown')})")
            else:
                passed.append(f"action_gate:{act.get('classification', 'unknown')}")
        except Exception as e:
            failed.append(f"action_gate: exception({e})")

        # Gate F: optional live state verification
        if assumed_state:
            try:
                ext = t_verify_external_state(assumed_state=assumed_state, sources=sources)
                drifted = int(ext.get("drifted_count") or 0)
                if drifted > 0:
                    failed.append(f"verify_external_state: drifted({drifted})")
                    warnings.append(f"external_state_drift({drifted})")
                else:
                    passed.append("verify_external_state_clean")
            except Exception as e:
                failed.append(f"verify_external_state: exception({e})")

        # Score: 1.0 per gate passed, 0.0 per gate failed. Normalize to 0.0-1.0.
        total_gates = len(passed) + len(failed)
        score = round(len(passed) / total_gates, 2) if total_gates > 0 else 0.0
        # Warnings reduce score gently, but should not dominate the hard gates.
        warning_penalty = min(0.15, 0.02 * len(warnings))
        score = max(0.0, score - warning_penalty)
        blocked = score < 0.8

        return {
            "ok": True,
            "verification_score": score,
            "passed_gates": passed,
            "failed_gates": failed,
            "warnings": warnings,
            "blocked": blocked,
            "verification_packet": {
                "operation": operation,
                "target_file": target_file,
                "action_type": action_type,
                "assumed_state_present": bool(assumed_state),
                "sources": sources,
                "owner_token_provided": bool(owner_token),
                "warning_penalty": warning_penalty,
            },
            "message": (
                f"BLOCKED: verification_score={score} < 0.8. Fix failed gates before deploying."
                if blocked else
                f"CLEAR: verification_score={score}. Proceed with deploy."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True, "blocked": True}


TOOLS["verify_before_deploy"] = {
    "fn": t_verify_before_deploy,
    "perm": "READ",
    "args": [
        {"name": "operation", "type": "string", "description": "What is being deployed"},
        {"name": "target_file", "type": "string", "description": "File being patched (e.g. core_tools.py)"},
        {"name": "context", "type": "string", "description": "Optional extra context for failure prediction"},
        {"name": "assumed_state", "type": "string", "description": "Optional JSON string of assumed live state to verify before deploying"},
        {"name": "sources", "type": "string", "description": "Comma-separated verification sources (supabase|github|railway)"},
        {"name": "action_type", "type": "string", "description": "Verification trust type (default deploy)"},
        {"name": "owner_token", "type": "string", "description": "Literal OWNER_CONFIRMED for irreversible actions"},
    ],
    "desc": "TASK-6.1: Pre-deploy verification enforcer. Call BEFORE any patch_file or gh_search_replace. Checks: validate_syntax, target_file named, predict_failure, trust map, action gate, and optional live-state verification. Returns verification_score 0.0-1.0. blocked=True if score < 0.8 -- do not deploy. HARD RULE: never deploy without calling this first.",
}


# -- TASK-V8: V8 Architecture Tools -------------------------------------------

def t_get_behavioral_rules(domain: str = None, page: str = "1", page_size: str = "200") -> dict:
    """Load active behavioral rules from behavioral_rules table.
    If domain provided: return universal rules + domain-specific rules.
    If domain=None: return universal rules only.
    Filter: active=true, id=gt.1, order by priority asc.
    If table does not exist (migration pending): return empty list with migration_needed flag.
    Supports pagination: page=1-based page number, page_size=rows per page (default 200, max 500)."""
    try:
        try:
            pg = max(1, int(page))
            ps = min(500, max(10, int(page_size)))
        except Exception:
            pg, ps = 1, 200
        offset = (pg - 1) * ps
        if domain and domain.strip():
            filters = f"active=eq.true&id=gt.1&confidence=gte.0.5&domain=in.(universal,{domain.strip()})&order=priority.asc&limit={ps}&offset={offset}"  # P1-07
        else:
            filters = f"active=eq.true&id=gt.1&confidence=gte.0.5&domain=eq.universal&order=priority.asc&limit={ps}&offset={offset}"  # P1-07
        rows = sb_get("behavioral_rules", f"select={_sel_force('behavioral_rules', ['trigger','pointer','full_rule','domain','priority','tested','confidence'])}&{filters}", svc=True)
        if rows is None:
            return {"ok": True, "rules": [], "migration_needed": True, "warning": "behavioral_rules table may not exist yet"}
        result = {"ok": True, "rules": rows or [], "count": len(rows or []), "domain": domain or "universal", "page": pg, "page_size": ps}
        if len(rows or []) == ps:
            result["has_more"] = True
            result["next_page"] = pg + 1
        return result
    except Exception as e:
        err = str(e)
        if "not found" in err.lower() or "does not exist" in err.lower():
            return {"ok": True, "rules": [], "migration_needed": True, "warning": f"behavioral_rules table not found: {err}"}
        return {"ok": False, "error": err, "error_code": "exception", "retry_hint": True}

TOOLS["get_behavioral_rules"] = {"fn": t_get_behavioral_rules, "perm": "READ",
    "args": [
        {"name": "domain", "type": "string", "description": "Domain to load rules for (e.g. railway, code, postgres). Returns universal + domain rules."},
        {"name": "page", "type": "string", "description": "Page number (1-based, default 1)"},
        {"name": "page_size", "type": "string", "description": "Rows per page (default 200, max 500)"},
    ],
    "desc": "Load active behavioral rules from behavioral_rules table. domain= filters to universal + domain-specific rules. Returns trigger, pointer, full_rule, priority. Supports pagination (page, page_size). IF question is about why CORE deviated from rules or did not follow protocol: (1) call this to get active rules, (2) then sb_query(table='sessions', filters='id=gt.1', order='created_at.desc', limit='3', select='summary,quality,domain,actions') to see what actually happened, (3) compare rules vs actual session actions to identify deviation. NOTE: domain='rarl' contains simulation research data NOT operational history — for CORE execution behavior use domain='core_agi'. EXAMPLE: get_behavioral_rules(domain='core_agi', page='1', page_size='20')"}

def t_add_behavioral_rule(trigger: str = "", pointer: str = "", full_rule: str = "",
                           domain: str = "universal", priority: str = "5",
                           source: str = "core_discovered", confidence: str = "0.8",
                           expires_at: str = None) -> dict:
    """Add new behavioral rule to behavioral_rules table.
    Validates trigger against valid enum. Checks for duplicate trigger+domain+pointer."""
    VALID_TRIGGERS = {
        "before_any_act","during_action","post_action","before_domain_work",
        "during_code_write","post_deploy","before_auth","during_deploy","post_mistake",
        "before_ddl","during_supabase_write","post_discovery","before_code","post_new_service",
        "before_deploy","on_failure","post_new_credential","before_project_doc","on_blocked",
        "before_tool_call","on_tool_silent","session_open","before_api_call","on_empty_result",
        "session_close","before_supabase_write","on_conflict","before_supabase_read",
        "on_missing_credential","reasoning_preflight","before_adding_tool","on_version_mismatch",
        "before_training_change","on_interrupted_session","before_routing_change",
        "before_irreversible_act","post_action","during_deploy",
    }
    VALID_DOMAINS = {
        "universal","postgres","railway","github","supabase","groq","powershell","zapier",
        "project","auth","code","reasoning","failure_recovery","local_pc","telegram",
    }
    try:
        preflight = _require_external_service_preflight("supabase", "add_behavioral_rule")
        if preflight:
            return preflight
        trigger = (trigger or "").strip()
        pointer = (pointer or "").strip()
        full_rule = (full_rule or "").strip()
        domain = (domain or "").strip()
        source = (source or "").strip()
        if trigger not in VALID_TRIGGERS:
            return {"ok": False, "error_code": "invalid_trigger", "message": f"Invalid trigger '{trigger}'. Valid: {sorted(VALID_TRIGGERS)}", "retry_hint": False, "domain": "behavioral_rules"}
        if domain not in VALID_DOMAINS:
            return {"ok": False, "error_code": "invalid_domain", "message": f"Invalid domain '{domain}'. Valid: {sorted(VALID_DOMAINS)}", "retry_hint": False, "domain": "behavioral_rules"}
        try:
            prio = int(priority)
        except Exception:
            prio = 5
        try:
            conf = float(confidence)
            conf = max(0.0, min(1.0, conf))
        except Exception:
            conf = 0.8
        # Per-session rate limit: max 10 insertions per session
        _br_counter = getattr(t_add_behavioral_rule, "_session_insert_count", 0)
        if _br_counter >= 10:
            return {"ok": False, "error_code": "rate_limited", "message": f"Behavioral rule rate limit reached ({_br_counter}/10 this session). Prevents evolution queue flooding. Owner can reset by restarting session.", "retry_hint": False}
        # Duplicate check
        existing = sb_get("behavioral_rules", f"select={_sel_force('behavioral_rules', ['id','pointer'])}&active=eq.true&trigger=eq.{trigger}&domain=eq.{domain}&limit=5", svc=True) or []
        for ex in existing:
            if (ex.get("pointer") or "").strip().lower() == (pointer or "").strip().lower():
                return {
                    "ok": True,
                    "action": "skipped_duplicate",
                    "message": "Rule with same trigger+domain+pointer already exists",
                    "existing_id": ex.get("id"),
                    "trigger": trigger,
                    "domain": domain,
                }
        row = {"trigger": trigger, "pointer": pointer, "full_rule": full_rule,
               "domain": domain, "priority": prio, "source": source, "confidence": conf, "active": True, "tested": False}
        if expires_at:
            row["expires_at"] = expires_at
        ok = sb_post("behavioral_rules", row)
        verify_rows = sb_get(
            "behavioral_rules",
            f"select={_sel_force('behavioral_rules', ['id','trigger','domain','pointer','full_rule','source'])}"
            f"&trigger=eq.{trigger}&domain=eq.{domain}&pointer=eq.{pointer}&order=id.desc&limit=3",
            svc=True,
        ) or []
        matched = next(
            (
                r for r in verify_rows
                if (r.get("pointer") or "").strip().lower() == pointer.lower()
                and (r.get("trigger") or "").strip() == trigger
                and (r.get("domain") or "").strip() == domain
            ),
            None,
        )
        if ok and matched:
            t_add_behavioral_rule._session_insert_count = getattr(t_add_behavioral_rule, "_session_insert_count", 0) + 1
            return {
                "ok": True,
                "action": "inserted_verified",
                "trigger": trigger,
                "domain": domain,
                "rule_id": matched.get("id"),
                "session_inserts": getattr(t_add_behavioral_rule, "_session_insert_count", 0),
            }
        if not ok and matched:
            t_add_behavioral_rule._session_insert_count = getattr(t_add_behavioral_rule, "_session_insert_count", 0) + 1
            return {
                "ok": True,
                "action": "recovered_partial",
                "message": "Insert reported failure, but verification found the row in behavioral_rules.",
                "trigger": trigger,
                "domain": domain,
                "rule_id": matched.get("id"),
                "session_inserts": getattr(t_add_behavioral_rule, "_session_insert_count", 0),
                "retry_hint": False,
            }
        return {
            "ok": False,
            "action": "insert_failed",
            "message": "Behavioral rule insert did not verify in Supabase.",
            "trigger": trigger,
            "domain": domain,
            "retry_hint": True,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["add_behavioral_rule"] = {"fn": t_add_behavioral_rule, "perm": "WRITE",
    "args": [
        {"name": "trigger", "type": "string"}, {"name": "pointer", "type": "string"},
        {"name": "full_rule", "type": "string"}, {"name": "domain", "type": "string"},
        {"name": "priority", "type": "string"}, {"name": "source", "type": "string"},
        {"name": "confidence", "type": "string"}, {"name": "expires_at", "type": "string"},
    ],
    "desc": "TASK-V8: Add new behavioral rule to behavioral_rules table. Validates trigger+domain enums. Checks for duplicate trigger+domain+pointer. source=owner|core_discovered|evolution|cold_processor."}


def t_update_behavioral_rule(rule_id: str = "", active: str = None, full_rule: str = None,
                              confidence: str = None, tested: str = None) -> dict:
    """Update an existing behavioral rule by id."""
    try:
        if not rule_id:
            return {"ok": False, "error_code": "missing_id", "message": "rule_id is required", "retry_hint": False, "domain": "behavioral_rules"}
        updates = {}
        if active is not None:
            updates["active"] = str(active).strip().lower() in ("true","1","yes")
        if full_rule:
            updates["full_rule"] = full_rule
        if confidence:
            try:
                updates["confidence"] = max(0.0, min(1.0, float(confidence)))
            except Exception:
                pass
        if tested is not None:
            updates["tested"] = str(tested).strip().lower() in ("true","1","yes")
        if not updates:
            return {"ok": False, "error_code": "no_updates", "message": "No valid update fields provided", "retry_hint": False, "domain": "behavioral_rules"}
        updates["updated_at"] = datetime.utcnow().isoformat()
        ok = sb_patch("behavioral_rules", f"id=eq.{rule_id}", updates)
        return {"ok": ok, "rule_id": rule_id, "updated_fields": list(updates.keys())}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["update_behavioral_rule"] = {"fn": t_update_behavioral_rule, "perm": "WRITE",
    "args": [{"name": "rule_id","type":"string"},{"name":"active","type":"string"},
             {"name":"full_rule","type":"string"},{"name":"confidence","type":"string"},{"name":"tested","type":"string"}],
    "desc": "TASK-V8: Update behavioral rule by id. Fields: active (true/false), full_rule, confidence (0.0-1.0), tested (true/false)."}


def t_get_infrastructure(component: str = None) -> dict:
    """Load infrastructure_map entries. Filter by component or return all active."""
    try:
        if component and component.strip():
            filters = f"status=neq.tombstone&id=gt.1&component=eq.{component.strip()}&order=id.asc"
        else:
            filters = "status=neq.tombstone&id=gt.1&order=id.asc"
        rows = sb_get("infrastructure_map",
            f"select=component,label,url,service_id,env_id,project_id,token_ref,fallback_component,status,check_interval_min,notes&{filters}", svc=True)
        if rows is None:
            return {"ok": True, "components": [], "migration_needed": True}
        return {"ok": True, "components": rows or [], "count": len(rows or [])}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["get_infrastructure"] = {"fn": t_get_infrastructure, "perm": "READ",
    "args": [{"name": "component", "type": "string", "description": "Filter by component name (railway, supabase, github, groq, telegram, local_pc)"}],
    "desc": "TASK-V8: Load infrastructure_map entries. Returns service URLs, IDs, token_refs (key names only -- not actual values). Component filter optional."}


def t_update_infrastructure_status(component: str = "", status: str = "", notes: str = None,
                                    owner_confirm: str = "false") -> dict:
    """Update infrastructure component status. status=tombstone requires owner_confirm=true."""
    try:
        if not component or not status:
            return {"ok": False, "error_code": "missing_params", "message": "component and status are required", "retry_hint": False, "domain": "infrastructure_map"}
        if status == "tombstone" and str(owner_confirm).strip().lower() not in ("true","1","yes"):
            return {"ok": False, "error_code": "permission_denied", "message": "Tombstoning a component requires owner_confirm=true. This is an irreversible action.", "retry_hint": False, "domain": "infrastructure_map"}
        preflight = _require_external_service_preflight("supabase", "update_infrastructure_status")
        if preflight:
            return preflight
        updates = {"status": status, "last_checked": datetime.utcnow().isoformat()}
        if notes:
            updates["notes"] = notes
        ok = sb_patch("infrastructure_map", f"component=eq.{component}", updates)
        return {"ok": ok, "component": component, "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["update_infrastructure_status"] = {"fn": t_update_infrastructure_status, "perm": "WRITE",
    "args": [{"name":"component","type":"string"},{"name":"status","type":"string"},
             {"name":"notes","type":"string"},{"name":"owner_confirm","type":"string"}],
    "desc": "TASK-V8: Update infrastructure component status (active|degraded|tombstone). tombstone requires owner_confirm=true."}


def t_add_infrastructure(component: str = "", label: str = "", url: str = None,
                          token_ref: str = None, fallback_component: str = None,
                          notes: str = None) -> dict:
    """Add new infrastructure component to infrastructure_map."""
    try:
        if not component or not label:
            return {"ok": False, "error_code": "missing_params", "message": "component and label are required", "retry_hint": False, "domain": "infrastructure_map"}
        row = {"component": component, "label": label, "status": "active"}
        if url: row["url"] = url
        if token_ref: row["token_ref"] = token_ref
        if fallback_component: row["fallback_component"] = fallback_component
        if notes: row["notes"] = notes
        ok = sb_post("infrastructure_map", row)
        return {"ok": ok, "component": component}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["add_infrastructure"] = {"fn": t_add_infrastructure, "perm": "WRITE",
    "args": [{"name":"component","type":"string"},{"name":"label","type":"string"},
             {"name":"url","type":"string"},{"name":"token_ref","type":"string"},
             {"name":"fallback_component","type":"string"},{"name":"notes","type":"string"}],
    "desc": "TASK-V8: Add new infrastructure component to infrastructure_map."}


def t_get_credentials_index(service: str = None) -> dict:
    """Load credentials_index entries (pointers only -- no actual values).
    Returns key_name, location, env_var_name for each service."""
    try:
        if service and service.strip():
            filters = f"active=eq.true&id=gt.1&service=eq.{service.strip()}&order=id.asc"
        else:
            filters = "active=eq.true&id=gt.1&order=service.asc"
        rows = sb_get("credentials_index",
            "select=service,key_name,location,env_var_name,required_for,notes,last_verified,expires_at&" + filters,
            svc=True)
        if rows is None:
            return {"ok": True, "credentials": [], "migration_needed": True}
        return {"ok": True, "credentials": rows or [], "count": len(rows or [])}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["get_credentials_index"] = {"fn": t_get_credentials_index, "perm": "READ",
    "args": [{"name": "service", "type": "string", "description": "Filter by service name (railway, supabase, github, groq, telegram)"}],
    "desc": "TASK-V8: Load credentials_index entries (pointers only -- NOT actual credentials). Returns key_name + where to find each credential."}


def t_add_credentials_index(service: str = "", key_name: str = "", location: str = "",
                              env_var_name: str = None, required_for: str = None,
                              notes: str = None) -> dict:
    """Add new credential pointer to credentials_index."""
    try:
        if not service or not key_name or not location:
            return {"ok": False, "error_code": "missing_params", "message": "service, key_name, location are required", "retry_hint": False, "domain": "credentials_index"}
        row = {"service": service, "key_name": key_name, "location": location, "active": True}
        if env_var_name: row["env_var_name"] = env_var_name
        if required_for:
            row["required_for"] = [x.strip() for x in required_for.split(",") if x.strip()]
        if notes: row["notes"] = notes
        ok = sb_post("credentials_index", row)
        return {"ok": ok, "service": service, "key_name": key_name}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["add_credentials_index"] = {"fn": t_add_credentials_index, "perm": "WRITE",
    "args": [{"name":"service","type":"string"},{"name":"key_name","type":"string"},
             {"name":"location","type":"string"},{"name":"env_var_name","type":"string"},
             {"name":"required_for","type":"string"},{"name":"notes","type":"string"}],
    "desc": "TASK-V8: Add new credential pointer to credentials_index. Stores key_name and location ONLY -- never actual credential values."}


def t_log_quality_metrics(session_id: str = "", quality_score: str = "0.8",
                           tasks_completed: str = "0", mistakes_made: str = "0",
                           owner_corrections: str = "0", assumptions_caught: str = "0",
                           domain: str = None, notes: str = None) -> dict:
    """Log quality score for current session. Called at session close."""
    try:
        try:
            q = max(0.0, min(1.0, float(quality_score)))
        except Exception:
            q = 0.8
        row = {
            "quality_score": q,
            "tasks_completed": int(tasks_completed) if str(tasks_completed).isdigit() else 0,
            "mistakes_made": int(mistakes_made) if str(mistakes_made).isdigit() else 0,
            "owner_corrections": int(owner_corrections) if str(owner_corrections).isdigit() else 0,
            "assumptions_caught": int(assumptions_caught) if str(assumptions_caught).isdigit() else 0,
        }
        if session_id: row["session_id"] = session_id
        if domain: row["domain"] = domain
        if notes: row["notes"] = notes
        ok = sb_post("quality_metrics", row)
        return {"ok": ok, "quality_score": q}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["log_quality_metrics"] = {"fn": t_log_quality_metrics, "perm": "WRITE",
    "args": [{"name":"session_id","type":"string"},{"name":"quality_score","type":"string"},
             {"name":"tasks_completed","type":"string"},{"name":"mistakes_made","type":"string"},
             {"name":"owner_corrections","type":"string"},{"name":"assumptions_caught","type":"string"},
             {"name":"domain","type":"string"},{"name":"notes","type":"string"}],
    "desc": "TASK-V8: Log session quality metrics to quality_metrics table. quality_score=0.0-1.0. Call at session close."}


def t_get_quality_alert() -> dict:
    """Check quality_metrics for last 7 days. Returns alert if 7d avg < 0.75 or 3 consecutive declining."""
    try:
        rows = sb_get("quality_metrics",
            "select=quality_score,date,domain&id=gt.1&order=created_at.desc&limit=30", svc=True) or []
        if len(rows) < 3:
            return {"ok": True, "alert": False, "trend": "insufficient_data", "message": "Need 3+ sessions for quality trend analysis", "count": len(rows)}
        scores = [float(r.get("quality_score", 0)) for r in rows[:7] if r.get("quality_score") is not None]
        if not scores:
            return {"ok": True, "alert": False, "trend": "no_data"}
        avg_7d = round(sum(scores) / len(scores), 3)
        # Check 3 consecutive declining
        consecutive_decline = False
        if len(scores) >= 3:
            consecutive_decline = scores[0] < scores[1] < scores[2]
        alert = avg_7d < 0.75 or consecutive_decline
        trend = "declining" if consecutive_decline or (len(scores) >= 2 and scores[0] < scores[-1]) else "stable"
        return {"ok": True, "alert": alert, "trend": trend, "7d_avg": avg_7d, "sample_count": len(scores),
                "message": f"Quality {'ALERT' if alert else 'OK'}: 7d_avg={avg_7d}, trend={trend}"}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["get_quality_alert"] = {"fn": t_get_quality_alert, "perm": "READ", "args": [],
    "desc": "TASK-V8: Check quality_metrics for last 7 days. Returns alert=true if 7d_avg < 0.75 or 3 consecutive declining sessions."}


def t_log_reasoning(session_id: str = "", action_planned: str = "", preflight_result: str = "",
                     assumptions_caught: str = "0", queries_triggered: str = "0",
                     owner_confirm_needed: str = "false", domain: str = None) -> dict:
    """Log cognitive pre-flight result. Called after each reasoning_preflight run."""
    try:
        if not action_planned:
            return {"ok": False, "error_code": "missing_params", "message": "action_planned is required", "retry_hint": False, "domain": "reasoning_log"}
        row = {
            "action_planned": action_planned[:500],
            "preflight_result": (preflight_result or "")[:500],
            "assumptions_caught": int(assumptions_caught) if str(assumptions_caught).isdigit() else 0,
            "queries_triggered": int(queries_triggered) if str(queries_triggered).isdigit() else 0,
            "owner_confirm_needed": str(owner_confirm_needed).strip().lower() in ("true","1","yes"),
            "behavioral_rule_proposed": False,
        }
        if session_id: row["session_id"] = session_id
        if domain: row["domain"] = domain
        ok = sb_post("reasoning_log", row)
        return {"ok": ok, "action_planned": action_planned[:80]}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["log_reasoning"] = {"fn": t_log_reasoning, "perm": "WRITE",
    "args": [{"name":"session_id","type":"string"},{"name":"action_planned","type":"string"},
             {"name":"preflight_result","type":"string"},{"name":"assumptions_caught","type":"string"},
             {"name":"queries_triggered","type":"string"},{"name":"owner_confirm_needed","type":"string"},
             {"name":"domain","type":"string"}],
    "desc": "TASK-V8: Log cognitive pre-flight result to reasoning_log. Call after each reasoning_preflight run."}


# -- GAP-DATA-01: Weekly brain backup -----------------------------------------

def t_backup_brain(dry_run: str = "false") -> dict:
    """Export critical Supabase tables to private GitHub backup repo (no secret scanning).
    Uses BACKUP_REPO env var (pockiesaints7/core-agi-backups). Bypasses L.gh() rate limiter.
    dry_run=true lists what would be exported without writing."""
    import os as _os
    from datetime import datetime as _dt
    import json as _json
    import base64 as _b64
    dry = str(dry_run).strip().lower() in ("true", "1", "yes")
    date_str = _dt.utcnow().strftime("%Y-%m-%d")
    backup_repo = _os.environ.get("BACKUP_REPO", GITHUB_REPO)
    # UUID-PK tables: no id=gt.1. Bigserial tables: use id=gt.1.
    tables = [
        ("behavioral_rules", "select=id,trigger,pointer,full_rule,domain,priority,active,confidence&order=id.asc&limit=500&id=gt.1"),
        ("task_queue",       "select=id,task,status,priority,source,created_at&order=priority.desc&limit=50"),
        ("sessions",         "select=id,summary,domain,created_at&order=created_at.desc&limit=30"),
        ("mistakes",         "select=id,domain,what_failed,correct_approach,severity,root_cause,created_at&order=id.desc&limit=500&id=gt.1"),
        ("infrastructure_map", "select=*&order=id.asc&id=gt.1"),
        ("credentials_index",  "select=id,service,key_name,location,env_var_name,required_for&order=id.asc&id=gt.1"),
    ]
    if dry:
        return {"ok": True, "dry_run": True, "date": date_str, "backup_repo": backup_repo,
                "tables": ["knowledge_base (paginated 200/chunk)"] + [t[0] for t in tables],
                "message": f"Would write all tables to {backup_repo}/backups/{date_str}/"}
    results = []
    errors  = []
    # Collect all payloads (Supabase reads)
    file_payloads = {}
    KB_CHUNK = 200
    kb_offset = 1
    kb_chunk_num = 1
    kb_total = 0
    try:
        while True:
            qs = f"select=id,domain,topic,instruction,content,confidence,active&order=id.asc&limit={KB_CHUNK}&id=gt.{kb_offset}"
            chunk = sb_get("knowledge_base", qs, svc=True) or []
            if not chunk:
                break
            file_payloads[f"backups/{date_str}/knowledge_base_{kb_chunk_num}.json"] = _json.dumps(chunk, default=str, indent=2)
            kb_total += len(chunk)
            kb_chunk_num += 1
            kb_offset = chunk[-1]["id"]
            if len(chunk) < KB_CHUNK:
                break
        results.append({"table": "knowledge_base", "rows": kb_total, "chunks": kb_chunk_num - 1})
    except Exception as e:
        errors.append({"table": "knowledge_base", "error": str(e)})
    for table_name, qs in tables:
        try:
            rows = sb_get(table_name, qs, svc=True) or []
            file_payloads[f"backups/{date_str}/{table_name}.json"] = _json.dumps(rows, default=str, indent=2)
            results.append({"table": table_name, "rows": len(rows)})
        except Exception as e:
            errors.append({"table": table_name, "error": str(e)})
    if errors:
        notify(f"[BACKUP] Supabase fetch errors: {[e['table'] for e in errors]}")
        return {"ok": False, "date": date_str, "exported": results, "errors": errors}
    # Write each file directly via GitHub Contents API to PRIVATE backup repo
    gh_headers = _ghh()
    write_errors = []
    for path, content in file_payloads.items():
        try:
            sha = None
            r = httpx.get(f"https://api.github.com/repos/{backup_repo}/contents/{path}",
                          headers=gh_headers, timeout=10)
            if r.is_success:
                sha = r.json().get("sha")
            payload = {"message": f"[backup] {date_str}: {path.split('/')[-1]}",
                       "content": _b64.b64encode(content.encode("utf-8")).decode("ascii")}
            if sha:
                payload["sha"] = sha
            put = httpx.put(f"https://api.github.com/repos/{backup_repo}/contents/{path}",
                            headers=gh_headers, json=payload, timeout=30)
            if not put.is_success:
                write_errors.append({"path": path, "status": put.status_code, "body": put.text[:200]})
        except Exception as e:
            write_errors.append({"path": path, "error": str(e)})
    for r in results:
        r["ok"] = len(write_errors) == 0
    try:
        sb_post("sessions", {"summary": f"[state_update] last_backup_ts: {_dt.utcnow().isoformat()}",
                             "actions": [f"backup_brain: {len(file_payloads)} files -> {backup_repo}/backups/{date_str}/"],
                             "interface": "mcp"})
    except Exception:
        pass
    ok = len(write_errors) == 0
    if ok:
        notify(f"[BACKUP] Complete: {len(file_payloads)} files ({kb_total} KB rows) -> {backup_repo}/backups/{date_str}/")
    else:
        notify(f"[BACKUP] Write errors ({len(write_errors)}): {[e.get('path','?').split('/')[-1] for e in write_errors[:5]]}")
    return {"ok": ok, "date": date_str, "backup_repo": backup_repo, "files": len(file_payloads),
            "exported": results, "errors": write_errors}
TOOLS["backup_brain"] = {"fn": t_backup_brain, "perm": "READ",
    "args": [{"name": "dry_run", "type": "string", "description": "true=list what would be exported without writing (default false)"}],
    "desc": "GAP-DATA-01: Export critical Supabase tables to GitHub /backups/YYYY-MM-DD/. Runs weekly. dry_run=true for safe preview."}


def t_maintenance_purge(table: str = "hot_reflections", older_than_days: int = 14, dry_run: bool = True):
    """Purge old rows from maintenance tables. dry_run=True (default) only counts, never deletes.
    Tables: hot_reflections (processed rows older than N days), reasoning_log (older than N days), sessions (older than N days).
    Safety: dry_run must be explicitly False to delete. Never purges tombstone tables."""
    ALLOWED_TABLES = {"hot_reflections", "reasoning_log", "sessions"}
    # Load schema registry to verify column names before building any filter
    _load_schema_registry()
    try:
        older_than_days = int(older_than_days)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"older_than_days must be an integer, got: {older_than_days!r}"}
    if table not in ALLOWED_TABLES:
        return {"ok": False, "error": f"Table '{table}' not in allowed purge list: {ALLOWED_TABLES}"}
    if older_than_days < 7:
        return {"ok": False, "error": "older_than_days must be >= 7 to prevent accidental recent-data deletion"}
    cutoff = (datetime.utcnow() - timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Build count query
    try:
        count_filter = f"created_at=lt.{cutoff}"
        if table == "hot_reflections":
            count_filter += "&processed_by_cold=eq.true"
        rows = sb_get(table, count_filter + "&select=id", svc=True)
        count = len(rows) if isinstance(rows, list) else 0
    except Exception as e:
        return {"ok": False, "error": f"Count query failed: {str(e)}"}
    if dry_run is True or str(dry_run).lower() in ("true", "1", "yes"):
        return {
            "ok": True,
            "dry_run": True,
            "table": table,
            "older_than_days": older_than_days,
            "cutoff": cutoff,
            "would_delete": count,
            "note": "Pass dry_run=False to execute deletion"
        }
    # Execute deletion
    delete_filter = f"created_at=lt.{cutoff}"
    if table == "hot_reflections":
        delete_filter += "&processed_by_cold=eq.true"
    ok = sb_delete(table, delete_filter)
    print(f"[PURGE] {table}: deleted ~{count} rows older than {older_than_days}d. ok={ok}")
    return {
        "ok": ok,
        "dry_run": False,
        "table": table,
        "older_than_days": older_than_days,
        "cutoff": cutoff,
        "deleted_approx": count
    }
TOOLS["maintenance_purge"] = {"fn": t_maintenance_purge, "perm": "WRITE",
    "args": [
        {"name": "table", "type": "string", "description": "Table to purge: hot_reflections | reasoning_log | sessions (default: hot_reflections)"},
        {"name": "older_than_days", "type": "string", "description": "Delete rows older than N days (min 7, default 14)"},
        {"name": "dry_run", "type": "string", "description": "true=count only, false=execute deletion (default: true -- must explicitly pass false to delete)"}
    ],
    "desc": "GAP-DB-02/03: Purge old rows from maintenance tables. dry_run=true default -- never deletes without explicit dry_run=false. Tables: hot_reflections (processed), reasoning_log, sessions."}


# =============================================================================
# AGI AGENTIC LAYER — AGI-11/12/13/14
# All tools registered, verify + test in next session.
# =============================================================================

# --- AGI-11: Execution Quality Layer -----------------------------------------

def t_reason_chain(action: str = "", domain: str = "general"):
    """AGI-11/S1: Fetch Supabase context for causal reasoning before any non-trivial action.
    Returns domain mistakes + KB entries for Claude to reason on natively.
    Claude generates: chain, failure_modes, confidence, proceed_recommended."""
    if not action:
        return {"ok": False, "error": "action is required"}
    try:
        from core_reasoning_packet import build_reasoning_packet
        pkt = build_reasoning_packet(
            action,
            domain=domain,
            tables=["knowledge_base", "mistakes", "behavioral_rules", "hot_reflections", "output_reflections", "evolution_queue", "conversation_episodes"],
            limit=10,
            per_table=2,
        )
        packet = pkt.get("packet") or {}
        grouped = {}
        for h in packet.get("top_hits") or []:
            grouped.setdefault(h.get("table") or "unknown", []).append(h)
        mistakes = grouped.get("mistakes") or []
        kb = grouped.get("knowledge_base") or []
        tool_policy = ToolRelianceAdvisor.assess_packet(packet, planned_action=action, state_hint="")
        return {
            "ok": True,
            "action": action,
            "domain": domain,
            "packet_focus": packet.get("focus", ""),
            "memory_by_table": packet.get("memory_by_table", {}),
            "recent_mistakes": [{"what_failed": m.get("title"), "details": m.get("body")} for m in mistakes[:5]],
            "relevant_kb": [{"topic": k.get("title"), "content": k.get("body")} for k in kb[:5]],
            "context": packet.get("context", ""),
            "tool_strategy": tool_policy.get("tool_strategy", "tool_required"),
            "tool_budget": tool_policy.get("tool_budget", 2),
            "tool_reliance": tool_policy,
            "instruction": "Claude: using this context, generate the causal chain, failure_modes, confidence (0.0-1.0), and proceed_recommended for the planned action."
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "action": action}

TOOLS["reason_chain"] = {"fn": t_reason_chain, "perm": "READ",
    "args": [
        {"name": "action", "type": "string", "description": "The planned action to reason about"},
        {"name": "domain", "type": "string", "description": "Domain context (e.g. deployment, supabase, code) default: general"}
    ],
    "desc": "AGI-11: Causal reasoning chain before any non-trivial action. Returns chain, failure_modes, confidence, proceed_recommended, and tool_strategy. Call before every write/deploy/multi-step execution."}


def t_action_gate(action: str = "", owner_token: str = ""):
    """AGI-11/S3: Hard reversibility gate. Classifies action as read/reversible_write/irreversible.
    irreversible requires owner_token to proceed. Returns blocked=True if irreversible and no token."""
    try:
        if not action:
            return {"ok": False, "error": "action is required"}
        IRREVERSIBLE_KEYWORDS = [
            "drop", "delete", "destroy", "purge", "force_close", "truncate",
            "overwrite production", "permanent", "cannot be undone", "hard delete"
        ]
        REVERSIBLE_KEYWORDS = [
            "insert", "update", "patch", "upsert", "deploy", "redeploy",
            "add", "create", "write", "push", "post"
        ]
        action_lower = action.lower()
        if any(kw in action_lower for kw in IRREVERSIBLE_KEYWORDS):
            classification = "irreversible"
        elif any(kw in action_lower for kw in REVERSIBLE_KEYWORDS):
            classification = "reversible_write"
        else:
            classification = "read"
        # F.7: require literal phrase -- any non-empty string previously bypassed this (security theater)
        _valid_token = (owner_token == "OWNER_CONFIRMED")
        blocked = classification == "irreversible" and not _valid_token
        return {
            "ok": True,
            "action": action,
            "classification": classification,
            "blocked": blocked,
            "message": "BLOCKED: pass owner_token='OWNER_CONFIRMED' to proceed" if blocked else "Proceed",
            "requires_owner_token": classification == "irreversible",
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_assert_source(value: str = "", declared_source: str = "memory", field_name: str = ""):
    """AGI-11/S4: Assumption detection at point of use.
    declared_source: session_query | owner_input | memory | skill_file
    Returns flagged=True if source=memory — forces CORE to query instead."""
    try:
        if not value:
            return {"ok": False, "error": "value is required"}
        # F.8: skill_file on owner PC, not accessible from Railway -- same reliability risk as memory
        TRUSTED_SOURCES = {"session_query", "owner_input"}
        UNTRUSTED_SOURCES = {"memory", "skill_file"}
        flagged = declared_source in UNTRUSTED_SOURCES
        return {
            "ok": True,
            "value": value[:100],
            "field_name": field_name,
            "declared_source": declared_source,
            "flagged": flagged,
            "trusted": not flagged,
            "instruction": "Query this value from its source before using" if flagged else "Source verified — safe to use",
            "risk": "high" if flagged else "low"
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_goal_check(proposed_action: str = "", session_id: str = "", register_goal: str = ""):
    """AGI-11/S5: Goal-action alignment check.
    register_goal: set the session goal (call at task start).
    proposed_action: check if this action aligns with the registered goal.
    Returns aligned=True/False with reasoning."""
    # Store/retrieve goal from agentic_sessions table
    try:
        if register_goal:
            sb_post("agentic_sessions", {
                "session_id": session_id or "default",
                "goal": register_goal,
                "created_at": datetime.utcnow().isoformat()
            })
            return {"ok": True, "goal_registered": register_goal, "session_id": session_id or "default"}
        if not proposed_action:
            return {"ok": False, "error": "proposed_action or register_goal required"}
        # Load current goal
        rows = sb_get("agentic_sessions", f"session_id=eq.{session_id or 'default'}&order=created_at.desc&limit=1", svc=True)
        if not rows:
            return {"ok": False, "error": "No goal registered for this session. Call with register_goal first.", "aligned": None}
        goal = rows[0].get("goal", "")
        return {
            "ok": True,
            "goal": goal,
            "proposed_action": proposed_action,
            "instruction": "Claude: does the proposed_action directly advance the goal? Return aligned=true/false, alignment_score (0.0-1.0), reasoning, and recommendation (proceed|defer|reconsider)."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["goal_check"] = {"fn": t_goal_check, "perm": "WRITE",
    "args": [
        {"name": "register_goal", "type": "string", "description": "Set the session goal at task start (call once)"},
        {"name": "proposed_action", "type": "string", "description": "Check if this action aligns with the registered goal"},
        {"name": "session_id", "type": "string", "description": "Session identifier (default: default)"}
    ],
    "desc": "AGI-11: Goal-action alignment. Register goal at task start, then check each major action against it. Returns aligned=true/false with reasoning score. Catches goal drift."}


def t_mid_task_correct(anomaly: str = "", last_action: str = "", last_result: str = "", task_state: str = ""):
    """AGI-11/S7: Fetch Supabase context for mid-task self-correction.
    Returns similar past mistakes for Claude to diagnose the anomaly natively.
    Claude generates: root_cause, plan_adjustment, corrected_next_step."""
    if not anomaly:
        return {"ok": False, "error": "anomaly description is required"}
    try:
        from core_reasoning_packet import build_reasoning_packet
        pkt = build_reasoning_packet(
            anomaly,
            domain="general",
            tables=["mistakes", "hot_reflections", "output_reflections", "knowledge_base", "conversation_episodes"],
            limit=10,
            per_table=2,
        )
        packet = pkt.get("packet") or {}
        similar = [h for h in (packet.get("top_hits") or []) if (h.get("table") in ("mistakes", "hot_reflections", "output_reflections", "knowledge_base"))]
        tool_policy = ToolRelianceAdvisor.assess_packet(packet, planned_action=anomaly, state_hint=task_state)
        return {
            "ok": True,
            "anomaly": anomaly,
            "last_action": last_action,
            "last_result": last_result,
            "task_state": task_state,
            "packet_focus": packet.get("focus", ""),
            "memory_by_table": packet.get("memory_by_table", {}),
            "context": packet.get("context", ""),
            "tool_strategy": tool_policy.get("tool_strategy", "tool_required"),
            "tool_budget": tool_policy.get("tool_budget", 2),
            "tool_reliance": tool_policy,
            "similar_past_mistakes": [{"semantic_table": m.get("table"), "title": m.get("title"), "details": m.get("body")} for m in similar],
            "instruction": "Claude: using this context, diagnose root_cause, what_went_wrong, plan_adjustment, corrected_next_step, and whether to abort."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["mid_task_correct"] = {"fn": t_mid_task_correct, "perm": "READ",
    "args": [
        {"name": "anomaly", "type": "string", "description": "Description of the unexpected output or anomaly detected"},
        {"name": "last_action", "type": "string", "description": "The last action taken before the anomaly"},
        {"name": "last_result", "type": "string", "description": "The unexpected result that triggered this correction"},
        {"name": "task_state", "type": "string", "description": "Current task state summary"}
    ],
    "desc": "AGI-11: Mid-task self-correction. Call when anomaly detected instead of pushing through. Returns root_cause, plan_adjustment, corrected_next_step. Replaces log-and-continue pattern."}


# --- AGI-12: Task Intelligence Layer -----------------------------------------

def t_decompose_task(goal: str = "", domain: str = "general"):
    """AGI-12/S1: Fetch Supabase context for task decomposition.
    Returns domain KB + past mistakes for Claude to decompose the goal natively.
    Claude generates: subtasks with dependencies, execution_order, parallel_candidates."""
    if not goal:
        return {"ok": False, "error": "goal is required"}
    try:
        from core_reasoning_packet import build_reasoning_packet
        pkt = build_reasoning_packet(
            goal,
            domain=domain,
            tables=["knowledge_base", "mistakes", "behavioral_rules", "hot_reflections", "output_reflections", "evolution_queue", "conversation_episodes"],
            limit=12,
            per_table=2,
        )
        packet = pkt.get("packet") or {}
        grouped = {}
        for h in packet.get("top_hits") or []:
            grouped.setdefault(h.get("table") or "unknown", []).append(h)
        kb = grouped.get("knowledge_base") or []
        mistakes = grouped.get("mistakes") or []
        tool_policy = ToolRelianceAdvisor.assess_packet(packet, planned_action=goal, state_hint="")
        return {
            "ok": True,
            "goal": goal,
            "domain": domain,
            "packet_focus": packet.get("focus", ""),
            "memory_by_table": packet.get("memory_by_table", {}),
            "context": packet.get("context", ""),
            "tool_strategy": tool_policy.get("tool_strategy", "tool_required"),
            "tool_budget": tool_policy.get("tool_budget", 2),
            "tool_reliance": tool_policy,
            "domain_kb": [{"topic": k.get("title"), "content": k.get("body")} for k in kb],
            "domain_mistakes": [{"what_failed": m.get("title"), "details": m.get("body")} for m in mistakes],
            "instruction": "Claude: using this context, decompose the goal into subtasks with id, title, description, depends_on, effort_estimate. Return execution_order, parallel_candidates, total_effort, critical_path."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["decompose_task"] = {"fn": t_decompose_task, "perm": "READ",
    "args": [
        {"name": "goal", "type": "string", "description": "High-level goal or task description to decompose"},
        {"name": "domain", "type": "string", "description": "Domain context for better decomposition (default: general)"}
    ],
    "desc": "AGI-12: Autonomous task decomposition. Takes a high-level goal, returns subtasks with dependencies, execution order, parallel candidates, and effort estimates."}


def t_resolve_ambiguity(instruction: str = ""):
    """AGI-12/S2: Fetch Supabase context for ambiguity resolution before acting.
    Returns relevant behavioral rules + KB for Claude to resolve ambiguity natively.
    Claude generates: ambiguities list, safe_to_proceed, interpretation_chosen."""
    if not instruction:
        return {"ok": False, "error": "instruction is required"}
    try:
        from core_reasoning_packet import build_reasoning_packet
        pkt = build_reasoning_packet(
            instruction,
            domain="core_agi",
            tables=["knowledge_base", "behavioral_rules", "output_reflections", "hot_reflections"],
            limit=10,
            per_table=3,
        )
        packet = pkt.get("packet") or {}
        grouped = {}
        for h in packet.get("top_hits") or []:
            grouped.setdefault(h.get("table") or "unknown", []).append(h)
        rules = grouped.get("behavioral_rules") or []
        kb = grouped.get("knowledge_base") or []
        tool_policy = ToolRelianceAdvisor.assess_packet(packet, planned_action=instruction, state_hint="")
        return {
            "ok": True,
            "instruction": instruction,
            "packet_focus": packet.get("focus", ""),
            "memory_by_table": packet.get("memory_by_table", {}),
            "context": packet.get("context", ""),
            "tool_strategy": tool_policy.get("tool_strategy", "tool_required"),
            "tool_budget": tool_policy.get("tool_budget", 2),
            "tool_reliance": tool_policy,
            "relevant_rules": [{"trigger": r.get("title"), "full_rule": r.get("body")} for r in rules],
            "relevant_kb": [{"topic": k.get("title"), "content": k.get("body")} for k in kb],
            "instruction_to_claude": "Claude: identify ambiguities in the instruction, list interpretations, pick lowest-risk one. Return ambiguities[], safe_to_proceed, interpretation_chosen, confidence, needs_owner_clarification, clarification_question."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["resolve_ambiguity"] = {"fn": t_resolve_ambiguity, "perm": "READ",
    "args": [
        {"name": "instruction", "type": "string", "description": "The instruction or task description to analyze for ambiguity"}
    ],
    "desc": "AGI-12: Structured ambiguity resolution. Identifies unclear elements, lists interpretations, picks lowest-risk one. Returns safe_to_proceed. Call before acting on ambiguous instructions."}


def t_scope_tracker(planned_scope: str = "", actions_taken: str = "", task_id: str = ""):
    """AGI-12/S3: Return planned vs actual scope for Claude to detect creep natively.
    No Supabase fetch needed — pure input comparison.
    Claude generates: scope_exceeded, drift_level, unplanned_items, recommendation."""
    try:
        if not planned_scope or not actions_taken:
            return {"ok": False, "error": "planned_scope and actions_taken are required"}
        actions_list = [a.strip() for a in actions_taken.split(",") if a.strip()]
        return {
            "ok": True,
            "task_id": task_id,
            "planned_scope": planned_scope,
            "actions_taken": actions_list,
            "action_count": len(actions_list),
            "instruction": "Claude: compare planned_scope vs actions_taken. Return scope_exceeded (bool), drift_level (none|minor|moderate|severe), planned_items[], unplanned_items[], overlap_percent (0-100), recommendation (continue|flag_owner|stop), summary."
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_lookahead(current_action: str = "", current_state: str = "", steps_ahead: int = 2):
    """AGI-12/S4: Fetch Supabase context for multi-step consequence modeling.
    Returns past failure patterns for Claude to model N+1 and N+2 states natively.
    Claude generates: next_states, blocking_detected, recommendation."""
    if not current_action:
        return {"ok": False, "error": "current_action is required"}
    try:
        steps_ahead = int(steps_ahead)
    except (TypeError, ValueError):
        steps_ahead = 2
    try:
        # Get top patterns for consequence modeling
        patterns = sb_get("pattern_frequency", "order=frequency.desc&limit=10&id=gt.1", svc=True) or []
        mistakes = sb_get("mistakes", "order=created_at.desc&limit=8&id=gt.1", svc=True) or []
        return {
            "ok": True,
            "current_action": current_action,
            "current_state": current_state,
            "steps_ahead": steps_ahead,
            # F.3: fix field names -- pattern_frequency table uses pattern_key and frequency, not pattern/freq
            "top_failure_patterns": [{"pattern": p.get("pattern_key"), "domain": p.get("domain"), "freq": p.get("frequency")} for p in patterns],
            "recent_mistakes": [{"context": m.get("context"), "what_failed": m.get("what_failed"), "root_cause": m.get("root_cause")} for m in mistakes],
            "instruction": f"Claude: model the next {steps_ahead} system states after this action. Return next_states (step, state_after, risk, risk_description), blocking_detected, blocking_reason, recommendation (proceed|reconsider|abort), summary."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["lookahead"] = {"fn": t_lookahead, "perm": "READ",
    "args": [
        {"name": "current_action", "type": "string", "description": "The action about to be executed"},
        {"name": "current_state", "type": "string", "description": "Summary of current system state"},
        {"name": "steps_ahead", "type": "string", "description": "How many steps to model ahead (default: 2)"}
    ],
    "desc": "AGI-12: Multi-step lookahead. Models N+1 and N+2 system states after an action. Returns blocking_detected if a future state is risky. Prevents locally-correct globally-wrong decisions."}


def t_sequence_plan(subtasks: str = ""):
    """AGI-12/S5: Return subtask list for Claude to sequence natively.
    No Supabase fetch needed — pure reasoning from input.
    Claude generates: sequential order, parallel_groups, dependency_map, race_condition_risks."""
    try:
        if not subtasks:
            return {"ok": False, "error": "subtasks list is required"}
        return {
            "ok": True,
            "subtasks": subtasks,
            "instruction": "Claude: analyze these subtasks for dependencies and side effects. Return sequential[], parallel_groups[], recommended_order[], dependency_map{}, race_condition_risks[]."
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_negative_space(task_description: str = "", domain: str = "general"):
    """AGI-12/S6: Fetch all domain mistakes from Supabase for negative space reasoning.
    Returns full mistake history for Claude to enumerate forbidden actions natively.
    Claude generates: forbidden_actions[], common_mistakes_in_domain[], summary."""
    if not task_description:
        return {"ok": False, "error": "task_description is required"}
    try:
        mistakes = sb_get("mistakes", f"domain=eq.{domain}&order=created_at.desc&limit=20&id=gt.1", svc=True) or []
        if not mistakes:
            # Fall back to all domains if no domain-specific mistakes
            mistakes = sb_get("mistakes", "order=created_at.desc&limit=15&id=gt.1", svc=True) or []
        return {
            "ok": True,
            "task_description": task_description,
            "domain": domain,
            "domain_mistakes": [{"context": m.get("context"), "what_failed": m.get("what_failed"), "root_cause": m.get("root_cause"), "how_to_avoid": m.get("how_to_avoid")} for m in mistakes],
            "instruction": "Claude: from this mistake history, enumerate the forbidden_actions (tempting but wrong) for this task. Include action, reason, risk (medium|high|critical), tempting_because. Also list common_mistakes_in_domain and a summary."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["negative_space"] = {"fn": t_negative_space, "perm": "READ",
    "args": [
        {"name": "task_description", "type": "string", "description": "The task to analyze for wrong-but-tempting actions"},
        {"name": "domain", "type": "string", "description": "Domain context for domain-specific mistake patterns (default: general)"}
    ],
    "desc": "AGI-12: Negative space reasoning. Enumerates forbidden actions — tempting but wrong for this task. Returns forbidden_actions with reasons and risk levels. Call at task start."}


# --- AGI-13: State & Context Integrity Layer ---------------------------------

def t_validate_output(value: str = "", target_field: str = "", table: str = ""):
    """AGI-13/S1: Semantic output validation before any write.
    Validates against operating_context.json schema. Returns safe_to_write."""
    try:
        if not value or not target_field or not table:
            return {"ok": False, "error": "value, target_field, and table are required"}
        schema = _load_schema_registry()
        violations = []
        if schema:
            table_schema = schema.get("tables", {}).get(table, {})
            # allowed_values lives at table level keyed by field name
            allowed_values = table_schema.get("allowed_values", {}).get(target_field, [])
            # field type lives in columns dict
            field_type = table_schema.get("columns", {}).get(target_field, "")
            if allowed_values and value not in allowed_values:
                violations.append({"field": target_field, "reason": f"Value '{value}' not in allowed: {allowed_values}"})
            if field_type == "uuid":
                import re as _uuid_re
                if not _uuid_re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', str(value).lower()):
                    violations.append({"field": target_field, "reason": f"Value is not a valid UUID"})
        safe_to_write = len(violations) == 0
        return {
            "ok": True,
            "value": value[:100],
            "target_field": target_field,
            "table": table,
            "safe_to_write": safe_to_write,
            "violations": violations,
            "schema_found": schema is not None
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}


def _validate_tool_output_packet(tool_name: str, result: Any, success: Any = None) -> dict:
    """Validate an arbitrary tool output shape before downstream layers consume it."""
    warnings = []
    failed_checks = []
    fatal = False
    shape = type(result).__name__
    ok_field = None
    completeness_monitor = _completeness_monitor_packet(tool_name, result, success=success)

    if not tool_name:
        failed_checks.append("tool_name_missing")
        fatal = True

    if not isinstance(result, dict):
        # Some read-only discovery tools return lists of rows directly.
        # Treat these as valid evidence for the known list-returning tools
        # rather than failing the whole pipeline.
        if tool_name in {"search_kb"} and isinstance(result, list):
            if not result:
                warnings.append("empty_list_output")
            return {
                "ok": True,
                "tool": tool_name or "?",
                "shape": shape,
                "fatal": False,
                "blocked": False,
                "warnings": warnings,
                "failed_checks": failed_checks,
                "validation_score": 1.0 if result else 0.85,
                "summary": f"{tool_name or '?'} returned list output ({shape})",
            }
        failed_checks.append(f"non_dict_output:{shape}")
        fatal = True
        return {
            "ok": True,
            "tool": tool_name or "?",
            "shape": shape,
            "fatal": fatal,
            "blocked": True,
            "warnings": warnings,
            "failed_checks": failed_checks,
            "validation_score": 0.0,
            "summary": f"{tool_name or '?'} returned non-dict output ({shape})",
        }

    ok_field = result.get("ok", None)
    if ok_field is None:
        warnings.append("missing_ok_field")

    if success is not None:
        try:
            success_bool = bool(success)
            if ok_field is not None and bool(ok_field) != success_bool:
                warnings.append("success_ok_mismatch")
        except Exception:
            warnings.append("success_unparseable")

    # Hard contradictions: tool says ok but carries an explicit error payload.
    error_text = str(result.get("error") or result.get("message") or "").strip()
    if bool(ok_field) and error_text:
        failed_checks.append("ok_true_with_error_payload")
        fatal = True

    # Structural issues that should not abort the pipeline but should be visible.
    if result.get("blocked") is True:
        warnings.append("blocked=True")
    if result.get("status") and str(result.get("status")).lower() in {"degraded", "partial", "timeout", "error", "stale"}:
        warnings.append(f"degraded_status:{result.get('status')}")
    if result.get("verified") is False:
        warnings.append("verified=False")
    if result.get("safe_to_write") is False:
        warnings.append("safe_to_write=False")
    if result.get("validation_passed") is False:
        warnings.append("validation_passed=False")
    if completeness_monitor.get("missing_required_fields"):
        warnings.append("missing_required_fields")
    if completeness_monitor.get("schema_supplied") and completeness_monitor.get("blocked"):
        failed_checks.append("schema_required_fields_missing")
        fatal = True
    if completeness_monitor.get("completeness_score") is not None:
        try:
            score = float(completeness_monitor.get("completeness_score"))
            if score < 0.8:
                warnings.append(f"completeness_monitor_low:{score:.2f}")
        except Exception:
            warnings.append("completeness_monitor_score_unparseable")
    if result.get("verification_score") is not None:
        try:
            score = float(result.get("verification_score"))
            if score < 0.8:
                warnings.append(f"low_verification_score:{score:.2f}")
        except Exception:
            warnings.append("verification_score_unparseable")

    validation_score = 1.0
    validation_score -= 0.45 if fatal else 0.0
    validation_score -= 0.10 * len(warnings)
    validation_score = max(0.0, round(validation_score, 2))
    blocked = fatal or validation_score < 0.8

    return {
        "ok": True,
        "tool": tool_name,
        "shape": shape,
        "fatal": fatal,
        "blocked": blocked,
        "warnings": warnings,
        "failed_checks": failed_checks,
        "ok_field": ok_field,
        "completeness_monitor": completeness_monitor,
        "summary": (
            f"{tool_name}: {'fatal' if fatal else 'ok'} "
            f"(warnings={len(warnings)}, failed_checks={len(failed_checks)})"
        ),
        "validation_score": validation_score,
    }


def t_validate_tool_output(tool_name: str = "", result_json: str = "", success: str = "", required_fields: str = "", schema_json: str = ""):
    """Validate a tool output payload from JSON text."""
    try:
        parsed = json.loads(result_json) if result_json else {}
    except Exception as exc:
        return {
            "ok": True,
            "tool": tool_name or "?",
            "fatal": True,
            "blocked": True,
            "warnings": ["result_json_parse_failed"],
            "failed_checks": ["invalid_json"],
            "error": str(exc),
            "validation_score": 0.0,
        }
    success_hint = None
    if success != "":
        try:
            success_hint = str(success).strip().lower() not in ("false", "0", "no", "off", "null")
        except Exception:
            success_hint = None
    completeness = _completeness_monitor_packet(
        tool_name,
        parsed,
        required_fields=required_fields,
        schema_json=schema_json,
        success=success_hint,
    )
    validation = _validate_tool_output_packet(tool_name, parsed, success=success_hint)
    validation["completeness_monitor"] = completeness
    if completeness.get("blocked"):
        validation["blocked"] = True
        validation["warnings"].append("completeness_monitor_blocked")
        if required_fields or schema_json or completeness.get("schema_supplied"):
            validation["fatal"] = True
            validation["failed_checks"].append("completeness_required_fields_missing")
            try:
                validation["validation_score"] = round(
                    min(float(validation.get("validation_score", 0.0)), float(completeness.get("completeness_score", 0.0))),
                    2,
                )
            except Exception:
                validation["validation_score"] = 0.0
            validation["summary"] = (
                f"{tool_name or '?'}: fatal (completeness={completeness.get('completeness_score', 0.0):.2f}, "
                f"missing={len(completeness.get('missing_required_fields') or [])})"
            )
    return validation


def t_completeness_monitor_packet(tool_name: str = "", result_json: str = "", success: str = "", required_fields: str = "", schema_json: str = ""):
    """Monitor payload completeness and schema adherence for output-generation pipelines."""
    try:
        parsed = json.loads(result_json) if result_json else {}
    except Exception as exc:
        return {
            "ok": True,
            "tool": tool_name or "?",
            "fatal": True,
            "blocked": True,
            "warnings": ["result_json_parse_failed"],
            "failed_checks": ["invalid_json"],
            "error": str(exc),
            "completeness_score": 0.0,
            "summary": f"{tool_name or '?'} completeness=error | invalid_json",
        }
    success_hint = None
    if success != "":
        try:
            success_hint = str(success).strip().lower() not in ("false", "0", "no", "off", "null")
        except Exception:
            success_hint = None
    return _completeness_monitor_packet(
        tool_name,
        parsed,
        required_fields=required_fields,
        schema_json=schema_json,
        success=success_hint,
    )


def _coerce_field_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, (list, tuple, set)):
        return [str(item).strip() for item in parsed if str(item or "").strip()]
    return [part.strip() for part in text.split(",") if part.strip()]


def _completeness_monitor_packet(
    tool_name: str,
    result: Any,
    required_fields: Any = None,
    schema_json: str = "",
    success: Any = None,
) -> dict:
    """Monitor payload completeness and schema adherence for tool outputs."""
    warnings = []
    missing_required = []
    extra_fields = []
    structural_notes = []
    schema_supplied = False
    schema_fields = []
    required_list = _coerce_field_list(required_fields)
    shape = type(result).__name__
    data_keys = []

    if schema_json:
        try:
            parsed_schema = json.loads(schema_json)
            if isinstance(parsed_schema, dict):
                schema_supplied = True
                schema_fields = _coerce_field_list(
                    parsed_schema.get("required")
                    or parsed_schema.get("fields")
                    or parsed_schema.get("columns")
                    or parsed_schema.get("properties")
                )
                if not required_list:
                    required_list = schema_fields[:]
        except Exception:
            warnings.append("schema_json_parse_failed")

    if isinstance(result, dict):
        data_keys = sorted(result.keys())
        if not required_list:
            required_list = _coerce_field_list(result.get("required_fields") or result.get("_required_fields"))
        if not schema_supplied:
            schema_candidate = result.get("schema") or result.get("_schema")
            if isinstance(schema_candidate, dict):
                schema_supplied = True
                schema_fields = _coerce_field_list(
                    schema_candidate.get("required")
                    or schema_candidate.get("fields")
                    or schema_candidate.get("columns")
                    or schema_candidate.get("properties")
                )
                if not required_list:
                    required_list = schema_fields[:]
            elif isinstance(result.get("schema_json"), str) and result.get("schema_json"):
                try:
                    parsed_schema = json.loads(result.get("schema_json"))
                    if isinstance(parsed_schema, dict):
                        schema_supplied = True
                        schema_fields = _coerce_field_list(
                            parsed_schema.get("required")
                            or parsed_schema.get("fields")
                            or parsed_schema.get("columns")
                            or parsed_schema.get("properties")
                        )
                        if not required_list:
                            required_list = schema_fields[:]
                except Exception:
                    warnings.append("result_schema_json_parse_failed")
        if not required_list:
            inferred = ["ok", "summary"] if "summary" in result else ["ok"]
            required_list = inferred if inferred else ["ok"]
        for field in required_list:
            value = result.get(field, None)
            if value is None or value == "" or value == [] or value == {}:
                missing_required.append(field)
        if schema_fields:
            extra_fields = sorted([key for key in data_keys if key not in set(schema_fields)])
            if extra_fields:
                structural_notes.append("schema_extra_fields")
        field_count = len(data_keys)
        present_count = sum(1 for key in data_keys if result.get(key) not in (None, "", [], {}, ()))
        field_coverage = round(present_count / max(1, field_count), 2)
        required_score = round((len(required_list) - len(missing_required)) / max(1, len(required_list)), 2) if required_list else 1.0
        completeness_score = round((required_score * 0.7) + (field_coverage * 0.3), 2)
        status = "complete" if not missing_required else ("partial" if completeness_score >= 0.66 else "incomplete")
    elif isinstance(result, list):
        field_count = len(result)
        dict_rows = [row for row in result if isinstance(row, dict)]
        if dict_rows:
            shared_keys = set(dict_rows[0].keys())
            for row in dict_rows[1:]:
                shared_keys &= set(row.keys())
            if not required_list:
                required_list = sorted(shared_keys)[:6]
            present_rows = 0
            row_scores = []
            for row in dict_rows:
                if required_list:
                    row_missing = [field for field in required_list if row.get(field) in (None, "", [], {}, ())]
                    if not row_missing:
                        present_rows += 1
                    missing_required.extend([f"{field}" for field in row_missing if field not in missing_required])
                    row_scores.append(round((len(required_list) - len(row_missing)) / max(1, len(required_list)), 2))
                else:
                    row_scores.append(1.0)
            completeness_score = round(sum(row_scores) / max(1, len(row_scores)), 2) if row_scores else 0.0
            required_score = round(present_rows / max(1, len(dict_rows)), 2)
            status = "complete" if not missing_required else ("partial" if completeness_score >= 0.66 else "incomplete")
            schema_supplied = bool(schema_json)
            data_keys = sorted(shared_keys)
        else:
            completeness_score = 0.5 if result else 0.0
            required_score = 0.0
            status = "partial" if result else "empty"
            structural_notes.append("non_dict_rows")
    else:
        completeness_score = 0.0
        required_score = 0.0
        status = "empty" if not result else "unsupported_shape"
        structural_notes.append(f"unsupported_shape:{shape}")

    if result and isinstance(result, dict) and bool(result.get("ok")) and str(result.get("summary") or "").strip():
        completeness_score = min(1.0, round(completeness_score + 0.05, 2))
    if success is not None:
        try:
            if bool(success) and completeness_score < 0.5:
                warnings.append("success_with_sparse_payload")
        except Exception:
            warnings.append("success_unparseable")

    if missing_required:
        warnings.append("missing_required_fields")
    if extra_fields and schema_supplied:
        warnings.append("schema_extra_fields")
    if completeness_score < 0.8:
        warnings.append(f"low_completeness:{completeness_score:.2f}")
    if required_score < 0.8 and required_list:
        warnings.append(f"low_required_coverage:{required_score:.2f}")

    blocked = bool(missing_required) and (schema_supplied or required_fields is not None)
    return {
        "ok": True,
        "tool": tool_name or "?",
        "shape": shape,
        "schema_supplied": schema_supplied,
        "required_fields": required_list,
        "missing_required_fields": missing_required,
        "extra_fields": extra_fields,
        "data_keys": data_keys,
        "field_coverage": field_coverage if isinstance(result, dict) else None,
        "required_coverage": required_score,
        "completeness_score": completeness_score,
        "status": status,
        "blocked": blocked,
        "warnings": warnings,
        "structural_notes": structural_notes,
        "summary": (
            f"{tool_name or '?'} completeness={completeness_score:.2f} "
            f"| required={required_score:.2f} | missing={len(missing_required)}"
        ),
    }


def t_prompt_scaffold_packet(
    prompt: str = "",
    objective: str = "",
    audience: str = "",
    constraints: str = "",
    output_format: str = "",
    context: str = "",
    examples: str = "",
    tone: str = "",
) -> dict:
    """Turn a vague prompt into a structured scaffold plus missing-field hints."""
    try:
        text = " ".join(
            part.strip()
            for part in [prompt, objective, audience, constraints, output_format, context, examples, tone]
            if str(part or "").strip()
        ).strip()
        prompt_text = (prompt or "").strip()
        missing: list[str] = []
        questions: list[str] = []
        if not prompt_text:
            missing.append("prompt")
            questions.append("What do you want CORE to do?")
        if not objective:
            missing.append("objective")
            questions.append("What is the desired outcome?")
        if not context:
            missing.append("context")
            questions.append("What background or files should CORE use?")
        if not constraints:
            missing.append("constraints")
            questions.append("What rules, limits, or boundaries must CORE follow?")
        if not output_format:
            missing.append("output_format")
            questions.append("What output format do you want?")
        if not audience:
            missing.append("audience")
        if not tone:
            missing.append("tone")
        if not examples:
            missing.append("examples")

        vague_signals = ("make it better", "help me", "do this", "fix this", "improve this", "not enough")
        is_vague = not prompt_text or len(prompt_text) < 40 or any(sig in prompt_text.lower() for sig in vague_signals)
        prompt_quality_score = 1.0
        prompt_quality_score -= min(0.65, 0.15 * len({m for m in missing if m in {"prompt", "objective", "context", "constraints", "output_format"}}))
        if is_vague:
            prompt_quality_score -= 0.15
        if len(prompt_text) < 12:
            prompt_quality_score -= 0.1
        prompt_quality_score = round(max(0.0, prompt_quality_score), 2)

        scaffold_lines = [
            f"Task: {prompt_text or objective or 'Describe the task clearly.'}",
            f"Objective: {objective or 'State the expected outcome.'}",
            f"Context: {context or 'Provide the relevant files, data, or background.'}",
            f"Constraints: {constraints or 'State hard limits, safety rules, and scope.'}",
            f"Output format: {output_format or 'Describe the desired output format.'}",
        ]
        if audience:
            scaffold_lines.append(f"Audience: {audience}")
        if tone:
            scaffold_lines.append(f"Tone: {tone}")
        if examples:
            scaffold_lines.append(f"Examples: {examples}")
        scaffold_lines.append("If any of the above is missing, ask for it before acting.")
        return {
            "ok": True,
            "prompt": prompt_text,
            "objective": objective.strip(),
            "audience": audience.strip(),
            "constraints": constraints.strip(),
            "output_format": output_format.strip(),
            "context": context.strip(),
            "examples": examples.strip(),
            "tone": tone.strip(),
            "missing_fields": missing,
            "clarification_questions": questions[:6],
            "prompt_quality_score": prompt_quality_score,
            "is_vague": is_vague,
            "scaffold_prompt": "\n".join(scaffold_lines),
            "summary": (
                f"prompt_scaffold=ok | quality={prompt_quality_score:.2f} | "
                f"missing={len(missing)} | vague={int(is_vague)}"
            ),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "summary": f"prompt_scaffold=error | {exc}"}


TOOLS["validate_tool_output"] = {
    "fn": t_validate_tool_output,
    "perm": "READ",
    "args": [
        {"name": "tool_name", "type": "string", "description": "Tool whose output is being validated"},
        {"name": "result_json", "type": "string", "description": "JSON string of the tool output payload"},
        {"name": "success", "type": "string", "description": "Optional success hint from the executor"},
        {"name": "required_fields", "type": "string", "description": "Comma-separated required fields for completeness checks"},
        {"name": "schema_json", "type": "string", "description": "Optional JSON schema or field contract for adherence checks"},
    ],
    "desc": "Validate a tool output payload for structural correctness, completeness, contradictory fields, and degraded output markers. Use when a tool result looks malformed, incomplete, or suspicious.",
}

TOOLS["completeness_monitor_packet"] = {
    "fn": t_completeness_monitor_packet,
    "perm": "READ",
    "args": [
        {"name": "tool_name", "type": "string", "description": "Tool or pipeline producing the payload"},
        {"name": "result_json", "type": "string", "description": "JSON string of the payload being inspected"},
        {"name": "success", "type": "string", "description": "Optional success hint from the executor"},
        {"name": "required_fields", "type": "string", "description": "Comma-separated required fields for completeness checks"},
        {"name": "schema_json", "type": "string", "description": "Optional JSON schema or field contract for adherence checks"},
    ],
    "desc": "Monitor payload completeness and schema adherence for output-generation pipelines. Use when you need required fields, completeness coverage, or schema contract checks before downstream consumption.",
}

TOOLS["prompt_scaffold_packet"] = {
    "fn": t_prompt_scaffold_packet,
    "perm": "READ",
    "args": [
        {"name": "prompt", "type": "string", "description": "Original human prompt or task request"},
        {"name": "objective", "type": "string", "description": "Desired outcome or goal"},
        {"name": "audience", "type": "string", "description": "Target audience or consumer of the output"},
        {"name": "constraints", "type": "string", "description": "Hard limits, boundaries, or safety rules"},
        {"name": "output_format", "type": "string", "description": "Requested output format"},
        {"name": "context", "type": "string", "description": "Relevant background, files, or evidence"},
        {"name": "examples", "type": "string", "description": "Optional examples or reference patterns"},
        {"name": "tone", "type": "string", "description": "Desired tone or style"},
    ],
    "desc": "Transform a vague prompt into a structured scaffold with missing-field hints and clarification questions. Use when a request lacks instructions, context, constraints, or output format.",
}

def t_tag_certainty(conclusion: str = "", basis: str = "inferred"):
    """AGI-13/S2: Tag a conclusion with its certainty level.
    basis: observed | inferred | assumed
    Returns certainty level, decay_rate, requires_verification."""
    try:
        if not conclusion:
            return {"ok": False, "error": "conclusion is required"}
        BASIS_MAP = {
            "observed": {"certainty": "confirmed", "decay_rate": "slow", "requires_verification": False},
            "inferred": {"certainty": "inferred", "decay_rate": "medium", "requires_verification": True},
            "assumed": {"certainty": "uncertain", "decay_rate": "fast", "requires_verification": True}
        }
        basis = basis.lower()
        if basis not in BASIS_MAP:
            basis = "assumed"
        tag = BASIS_MAP[basis]
        return {
            "ok": True,
            "conclusion": conclusion[:200],
            "basis": basis,
            "certainty": tag["certainty"],
            "decay_rate": tag["decay_rate"],
            "requires_verification": tag["requires_verification"],
            "safe_for_irreversible_action": basis == "observed",
            "instruction": "Verify before using in irreversible action" if tag["requires_verification"] else "Safe to use"
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_progress_model(task_id: str = "", subtasks_total: int = 0, subtasks_done: int = 0, actions_taken: int = 0):
    """AGI-13/S3: Progress visibility within a task.
    Returns percent_complete, estimated_steps_remaining, on_track, drift_detected."""
    try:
        subtasks_total = int(subtasks_total)
        subtasks_done = int(subtasks_done)
        actions_taken = int(actions_taken)
    except (TypeError, ValueError):
        return {"ok": False, "error": "subtasks_total, subtasks_done, actions_taken must be integers"}
    if subtasks_total == 0:
        return {"ok": False, "error": "subtasks_total must be > 0"}
    percent_complete = round((subtasks_done / subtasks_total) * 100, 1)
    avg_actions_per_subtask = actions_taken / max(subtasks_done, 1)
    estimated_steps_remaining = round(avg_actions_per_subtask * (subtasks_total - subtasks_done))
    on_track = avg_actions_per_subtask <= 10
    drift_detected = avg_actions_per_subtask > 15
    return {
        "ok": True,
        "task_id": task_id,
        "percent_complete": percent_complete,
        "subtasks_done": subtasks_done,
        "subtasks_total": subtasks_total,
        "actions_taken": actions_taken,
        "avg_actions_per_subtask": round(avg_actions_per_subtask, 1),
        "estimated_steps_remaining": estimated_steps_remaining,
        "on_track": on_track,
        "drift_detected": drift_detected,
        "status_label": f"{percent_complete}% complete — {'on track' if on_track else 'drifting'}"
    }

TOOLS["progress_model"] = {"fn": t_progress_model, "perm": "READ",
    "args": [
        {"name": "task_id", "type": "string", "description": "Task UUID for reference"},
        {"name": "subtasks_total", "type": "string", "description": "Total number of subtasks"},
        {"name": "subtasks_done", "type": "string", "description": "Number of subtasks completed"},
        {"name": "actions_taken", "type": "string", "description": "Total tool calls made so far in this task"}
    ],
    "desc": "AGI-13: Progress visibility. Returns percent_complete, estimated_steps_remaining, on_track, drift_detected. Call after each subtask gate and include in checkpoint."}


def t_cognitive_load(session_duration_mins: int = 0, tool_calls_made: int = 0, context_size_estimate: int = 0):
    """AGI-13/S4: Session complexity monitoring.
    Returns load_level (low/medium/high/critical) and recommendation."""
    try:
        session_duration_mins = int(session_duration_mins)
        tool_calls_made = int(tool_calls_made)
        context_size_estimate = int(context_size_estimate)
    except (TypeError, ValueError):
        return {"ok": False, "error": "All parameters must be integers"}
    score = 0
    if session_duration_mins > 60: score += 2
    elif session_duration_mins > 30: score += 1
    if tool_calls_made > 50: score += 2
    elif tool_calls_made > 25: score += 1
    if context_size_estimate > 80000: score += 2
    elif context_size_estimate > 40000: score += 1
    LEVELS = {0: "low", 1: "low", 2: "medium", 3: "medium", 4: "high", 5: "high", 6: "critical"}
    RECS = {"low": "continue", "medium": "checkpoint", "high": "summarize", "critical": "close"}
    load_level = LEVELS.get(score, "critical")
    recommendation = RECS[load_level]
    return {
        "ok": True,
        "load_level": load_level,
        "recommendation": recommendation,
        "score": score,
        "session_duration_mins": session_duration_mins,
        "tool_calls_made": tool_calls_made,
        "context_size_estimate": context_size_estimate,
        "action": f"Load={load_level}: {recommendation}"
    }

TOOLS["cognitive_load"] = {"fn": t_cognitive_load, "perm": "READ",
    "args": [
        {"name": "session_duration_mins", "type": "string", "description": "Session length in minutes"},
        {"name": "tool_calls_made", "type": "string", "description": "Total tool calls made this session"},
        {"name": "context_size_estimate", "type": "string", "description": "Estimated context window tokens used"}
    ],
    "desc": "AGI-13: Cognitive load monitoring. Returns load_level (low/medium/high/critical) and recommendation (continue/checkpoint/summarize/close). Call periodically on long sessions."}


def t_partial_complete(task_id: str = "", completed_subtasks: str = "", incomplete_subtasks: str = "", reason_stopped: str = ""):
    """AGI-13/S5: Clean partial completion protocol.
    Writes structured partial state to partial_states table.
    Ensures task is always in known recoverable state."""
    if not task_id or not reason_stopped:
        return {"ok": False, "error": "task_id and reason_stopped are required"}
    record = {
        "task_id": task_id,
        "completed_subtasks": completed_subtasks,
        "incomplete_subtasks": incomplete_subtasks,
        "reason_stopped": reason_stopped,
        "system_state_snapshot": json.dumps({
            "timestamp": datetime.utcnow().isoformat(),
            "completed": completed_subtasks.split(",") if completed_subtasks else [],
            "incomplete": incomplete_subtasks.split(",") if incomplete_subtasks else [],
            "reason": reason_stopped
        }),
        "created_at": datetime.utcnow().isoformat()
    }
    try:
        sb_post("partial_states", record)
        return {
            "ok": True,
            "task_id": task_id,
            "completed_subtasks": completed_subtasks,
            "incomplete_subtasks": incomplete_subtasks,
            "reason_stopped": reason_stopped,
            "message": "Partial state saved. Task can be resumed from this checkpoint."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["partial_complete"] = {"fn": t_partial_complete, "perm": "WRITE",
    "args": [
        {"name": "task_id", "type": "string", "description": "UUID of the task being suspended"},
        {"name": "completed_subtasks", "type": "string", "description": "Comma-separated list of completed subtask IDs"},
        {"name": "incomplete_subtasks", "type": "string", "description": "Comma-separated list of incomplete subtask IDs"},
        {"name": "reason_stopped", "type": "string", "description": "Why the task was suspended (blocker/crash/owner-close/timeout)"}
    ],
    "desc": "AGI-13: Clean partial completion protocol. Saves structured partial state when task is suspended. Ensures always-recoverable state. Call on any non-clean task exit."}


def t_verify_external_state(assumed_state: str = "", sources: str = "supabase"):
    """AGI-13/S6: Live external state verification before committing.
    Re-queries actual values from Supabase/GitHub/Railway and compares against assumed state.
    assumed_state: JSON string of {key: assumed_value} pairs.
    Supported key patterns (auto-routed):
      task:<uuid>:status       → task_queue.status
      task:<uuid>:next_step    → task_queue.next_step
      kb:<domain>:<topic>:confidence → knowledge_base.confidence
      session:last:quality     → sessions.quality_score (latest)
      evolution:<id>:status    → evolution_queue.status
      Any other key            → searched in sessions state_updates
    """
    if not assumed_state:
        return {"ok": False, "error": "assumed_state JSON string is required"}
    try:
        state = json.loads(assumed_state)
    except json.JSONDecodeError:
        return {"ok": False, "error": 'assumed_state must be valid JSON: {"key": "assumed_value"}'}

    drifted = []
    checked = []

    for key, assumed_value in state.items():
        live_value = None
        source = "unknown"
        error = None
        try:
            parts = key.split(":")
            # task:<uuid>:<field>
            if parts[0] == "task" and len(parts) >= 3:
                uuid, field = parts[1], parts[2]
                rows = sb_get("task_queue", f"select={field}&id=eq.{uuid}&limit=1", svc=True) or []
                live_value = rows[0].get(field) if rows else None
                source = "task_queue"
            # kb:<domain>:<topic>:<field>
            elif parts[0] == "kb" and len(parts) >= 4:
                domain, topic, field = parts[1], parts[2], parts[3]
                rows = sb_get("knowledge_base",
                    f"select={field}&domain=eq.{domain}&topic=eq.{topic}&limit=1", svc=True) or []
                live_value = rows[0].get(field) if rows else None
                source = "knowledge_base"
            # evolution:<id>:<field>
            elif parts[0] == "evolution" and len(parts) >= 3:
                eid, field = parts[1], parts[2]
                rows = sb_get("evolution_queue", f"select={field}&id=eq.{eid}&limit=1", svc=True) or []
                live_value = rows[0].get(field) if rows else None
                source = "evolution_queue"
            # session:last:<field>
            elif parts[0] == "session" and len(parts) >= 3:
                field = parts[2]
                rows = sb_get("sessions", f"select={field}&order=id.desc&limit=1", svc=True) or []
                live_value = rows[0].get(field) if rows else None
                source = "sessions"
            # fallback: scan state_update keys in sessions
            else:
                rows = sb_get("sessions",
                    f"select=summary&summary=like.*%5Bstate_update%5D+{key}:*&order=id.desc&limit=1",
                    svc=True) or []
                if rows:
                    raw = rows[0].get("summary", "")
                    prefix = f"[state_update] {key}: "
                    live_value = raw[len(prefix):].strip() if raw.startswith(prefix) else None
                source = "sessions_state"
        except Exception as e:
            error = str(e)

        # Compare
        match = (str(live_value) == str(assumed_value)) if live_value is not None else None
        entry = {
            "key": key,
            "assumed": assumed_value,
            "live": live_value,
            "source": source,
            "match": match,
        }
        if error:
            entry["error"] = error
        if match is False:
            drifted.append(entry)
        checked.append(entry)

    all_match = len(drifted) == 0
    return {
        "ok": True,
        "sources_checked": sources,
        "assumed_keys": list(state.keys()),
        "all_match": all_match,
        "drifted_count": len(drifted),
        "drifted": drifted,
        "checked": checked,
        "note": "Live re-query complete. Drifted keys indicate state changed since assumed." if drifted
                else "All keys match assumed state.",
    }

TOOLS["verify_external_state"] = {"fn": t_verify_external_state, "perm": "READ",
    "args": [
        {"name": "assumed_state", "type": "string", "description": 'JSON string of assumed key:value pairs e.g. {"task_status": "pending"}'},
        {"name": "sources", "type": "string", "description": "Comma-separated sources to verify against: supabase|github|railway (default: supabase)"}
    ],
    "desc": "AGI-13: External state verification. Checks assumed state against live sources before committing actions that depend on it. Returns drifted keys. Call before actions depending on earlier state."}


def t_verification_packet(
    operation: str = "",
    target_file: str = "",
    context: str = "",
    assumed_state: str = "",
    sources: str = "supabase",
    action_type: str = "deploy",
    owner_token: str = "",
) -> dict:
    """Aggregate the existing verification surfaces into one canonical packet.

    This is the higher-level verification mechanism for CORE actions that need
    deploy safety, reversibility checks, and optional live-state validation.
    """
    try:
        deploy_check = t_verify_before_deploy(
            operation=operation,
            target_file=target_file,
            context=context,
            assumed_state=assumed_state,
            sources=sources,
            action_type=action_type,
            owner_token=owner_token,
        )
        trust_check = t_trust_map(action_type or "deploy")
        action_check = t_action_gate(action=operation or target_file or context or action_type, owner_token=owner_token)
        state_check = None
        if assumed_state:
            state_check = t_verify_external_state(assumed_state=assumed_state, sources=sources)

        combined_checks = {
            "deploy": deploy_check,
            "trust": trust_check,
            "action": action_check,
        }
        if state_check is not None:
            combined_checks["state"] = state_check

        failed = []
        warnings = []
        for name, check in combined_checks.items():
            if not isinstance(check, dict):
                continue
            if check.get("blocked") is True:
                failed.append(name)
            if check.get("error"):
                failed.append(name)
            if name == "state" and int(check.get("drifted_count") or 0) > 0:
                failed.append("state_drift")
            for warn in check.get("warnings", []) or []:
                warnings.append(f"{name}:{warn}")

        score = float(deploy_check.get("verification_score") or 0.0)
        if state_check is not None and int(state_check.get("drifted_count") or 0) > 0:
            score = max(0.0, score - 0.15)
        if action_check.get("blocked"):
            score = max(0.0, score - 0.25)
        if trust_check.get("verification_required"):
            score = min(1.0, score + 0.05)
        score = max(0.0, min(1.0, round(score, 2) - 0.03 * len(warnings)))
        blocked = bool(failed) or score < 0.8

        return {
            "ok": True,
            "verification_score": score,
            "blocked": blocked,
            "failed_checks": sorted(set(failed)),
            "warnings": warnings,
            "deploy_check": deploy_check,
            "trust_check": trust_check,
            "action_check": action_check,
            "state_check": state_check,
            "message": (
                f"BLOCKED: verification_score={score} with failed_checks={sorted(set(failed))}"
                if blocked else
                f"CLEAR: verification_score={score}. Proceed with caution."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "blocked": True}


TOOLS["verification_packet"] = {
    "fn": t_verification_packet,
    "perm": "READ",
    "args": [
        {"name": "operation", "type": "string", "description": "What is about to happen"},
        {"name": "target_file", "type": "string", "description": "File being touched, if any"},
        {"name": "context", "type": "string", "description": "Optional extra context"},
        {"name": "assumed_state", "type": "string", "description": "Optional JSON string of assumed live state"},
        {"name": "sources", "type": "string", "description": "Comma-separated verification sources"},
        {"name": "action_type", "type": "string", "description": "Trust type: read|reversible_write|deploy|irreversible|schema_change|groq_call"},
        {"name": "owner_token", "type": "string", "description": "Literal OWNER_CONFIRMED for irreversible actions"},
    ],
    "desc": "Canonical verification packet that combines deploy verification, trust calibration, reversibility gating, and optional live-state checks into one decision surface.",
}


# --- AGI-14: Safety & Resilience Layer ---------------------------------------

def t_resource_model(planned_actions: str = ""):
    """AGI-14/S1: Resource awareness and consumption estimation.
    Returns Groq token estimate, Railway compute impact, Supabase write count, throttle recommendations."""
    try:
        if not planned_actions:
            return {"ok": False, "error": "planned_actions is required"}
        actions_list = [a.strip() for a in planned_actions.split(",") if a.strip()]
        groq_calls = sum(1 for a in actions_list if any(k in a.lower() for k in ["reason", "groq", "analyze", "generate", "synthesize", "check"]))
        sb_writes = sum(1 for a in actions_list if any(k in a.lower() for k in ["insert", "update", "patch", "upsert", "write", "post"]))
        deploys = sum(1 for a in actions_list if any(k in a.lower() for k in ["deploy", "patch_file", "redeploy"]))
        token_estimate = groq_calls * 2000
        GROQ_SESSION_LIMIT = 50000
        SUPABASE_BURST_LIMIT = 20
        DEPLOY_LIMIT = 3
        within_limits = token_estimate < GROQ_SESSION_LIMIT and sb_writes < SUPABASE_BURST_LIMIT and deploys <= DEPLOY_LIMIT
        throttle_recommended = sb_writes > 10 or deploys > 2
        batch_candidates = [a for a in actions_list if any(k in a.lower() for k in ["insert", "post", "write"])]
        return {
            "ok": True,
            "planned_action_count": len(actions_list),
            "groq_calls_estimated": groq_calls,
            "token_estimate": token_estimate,
            "supabase_writes": sb_writes,
            "deploys": deploys,
            "within_limits": within_limits,
            "throttle_recommended": throttle_recommended,
            "batch_candidates": batch_candidates,
            "warnings": [
                f"Token estimate {token_estimate} approaching Groq session limit" if token_estimate > 40000 else None,
                f"{sb_writes} Supabase writes -- consider batching" if sb_writes > 10 else None,
                f"{deploys} deploys planned -- high Railway compute" if deploys > 2 else None
            ]
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_impact_model(action: str = "", current_state: str = ""):
    """AGI-14/S2: Fetch infrastructure map + pending tasks for system impact modeling.
    Returns live system context for Claude to model impact natively.
    Claude generates: affected_components, timing_risks, side_effects, proceed_recommended."""
    if not action:
        return {"ok": False, "error": "action is required"}
    try:
        infra = sb_get("infrastructure_map", "status=eq.active&id=gt.1", svc=True) or []
        pending_tasks = sb_get("task_queue", "status=eq.pending&order=priority.desc&limit=5", svc=True) or []
        return {
            "ok": True,
            "action": action,
            "current_state": current_state,
            "active_infrastructure": [{"component": i.get("component"), "label": i.get("label"), "notes": i.get("notes")} for i in infra],
            "pending_tasks": [{"task": t.get("task", "{}")[:120], "priority": t.get("priority")} for t in pending_tasks],
            "instruction": "Claude: given this infrastructure and pending tasks, model the system impact of the action. Return affected_components[], timing_safe, timing_risks[], side_effects[], pending_task_interference[], proceed_recommended, summary."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["impact_model"] = {"fn": t_impact_model, "perm": "READ",
    "args": [
        {"name": "action", "type": "string", "description": "The action to model impact for"},
        {"name": "current_state", "type": "string", "description": "Summary of current system state"}
    ],
    "desc": "AGI-14: System impact modeling. Identifies affected components, timing risks, side effects on pending tasks. Returns proceed_recommended. Call before touching shared infrastructure."}


def t_trust_map(action_type: str = ""):
    """AGI-14/S3: Trust calibration per action type.
    Returns verification_required and verification_steps for the given action."""
    try:
        TRUST_MAP = {
            "read": {"trust": "high", "verification_required": False, "verification_steps": [], "risk_level": "low"},
            "reversible_write": {"trust": "medium", "verification_required": True, "verification_steps": ["verify_output", "check_schema"], "risk_level": "medium"},
            "deploy": {"trust": "low", "verification_required": True, "verification_steps": ["validate_syntax", "verify_before_deploy", "predict_failure", "health_check_after"], "risk_level": "high"},
            "irreversible": {"trust": "critical", "verification_required": True, "verification_steps": ["action_gate", "owner_confirmation", "reason_chain", "lookahead"], "risk_level": "critical"},
            "schema_change": {"trust": "low", "verification_required": True, "verification_steps": ["dry_run_first", "verify_before_deploy", "owner_confirmation"], "risk_level": "high"},
            "groq_call": {"trust": "medium", "verification_required": False, "verification_steps": ["validate_json_output"], "risk_level": "low"},
        }
        action_lower = action_type.lower()
        if not action_type or action_lower not in TRUST_MAP:
            return {
                "ok": True,
                "available_types": list(TRUST_MAP.keys()),
                "message": "Pass one of the available action types to get trust calibration"
            }
        entry = TRUST_MAP[action_lower]
        return {
            "ok": True,
            "action_type": action_lower,
            "trust_level": entry["trust"],
            "risk_level": entry["risk_level"],
            "verification_required": entry["verification_required"],
            "verification_steps": entry["verification_steps"],
            "instruction": f"Run {entry['verification_steps']} before proceeding" if entry["verification_required"] else "Low risk — proceed with standard care"
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_contradiction_check(new_instruction: str = "", domain: str = "general"):
    """AGI-14/S4: Fetch behavioral_rules from Supabase for contradiction detection.
    Returns rules for Claude to check the new instruction against natively.
    Claude generates: conflict_detected, conflicting_rules, recommendation."""
    if not new_instruction:
        return {"ok": False, "error": "new_instruction is required"}
    try:
        rules = sb_get("behavioral_rules", f"domain=eq.{domain}&active=eq.true&select={_sel_force('behavioral_rules', ['id','trigger','full_rule'])}&id=gt.1", svc=True) or []
        if not rules:
            rules = sb_get("behavioral_rules", f"domain=eq.universal&active=eq.true&select={_sel_force('behavioral_rules', ['id','trigger','full_rule'])}&id=gt.1", svc=True) or []
        return {
            "ok": True,
            "new_instruction": new_instruction,
            "domain": domain,
            "rules_checked": len(rules),
            "existing_rules": [{"id": r.get("id"), "trigger": r.get("trigger"), "full_rule": (r.get("full_rule") or "")[:300]} for r in rules[:20]],
            "instruction": "Claude: check if new_instruction contradicts any existing_rules. Return conflict_detected (bool), conflicting_rules (list of {rule_id, trigger, conflict_description, severity}), recommendation (override|defer|ask_owner|safe_to_proceed), reasoning."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["contradiction_check"] = {"fn": t_contradiction_check, "perm": "READ",
    "args": [
        {"name": "new_instruction", "type": "string", "description": "The new instruction to check for conflicts"},
        {"name": "domain", "type": "string", "description": "Domain to search rules in (default: general)"}
    ],
    "desc": "AGI-14: Contradiction detection. Checks new instructions against existing behavioral_rules for conflicts. Returns conflict_detected, conflicting_rules, recommendation. Call when owner gives new behavioral instruction."}


def t_circuit_breaker(failed_step: str = "", dependent_steps: str = "", failure_reason: str = ""):
    """AGI-14/S5: Failure cascade prevention.
    Analyzes which downstream steps depend on the failed step.
    Returns cascade_risk, safe_to_continue, steps_to_suspend."""
    try:
        if not failed_step:
            return {"ok": False, "error": "failed_step is required"}
        dependent_list = [s.strip() for s in dependent_steps.split(",") if s.strip()] if dependent_steps else []
        return {
            "ok": True,
            "failed_step": failed_step,
            "failure_reason": failure_reason,
            "dependent_steps": dependent_list,
            "instruction": "Claude: analyze cascade risk for these dependent steps given the failed step and reason. Return cascade_risk (list of {step, dependency, impact: blocked|degraded|unaffected, severity}), safe_to_continue, steps_to_suspend[], steps_safe_to_run[], recommended_action (suspend_all|skip_dependents|retry_failed|ask_owner), summary."
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_loop_detect(action: str = "", context_hash: str = "", session_id: str = "default", clear: bool = False):
    """AGI-14/S6: Action loop detection within a session.
    Maintains hash of actions taken with timestamps. Returns loop_detected only if same
    action+context was attempted within the last 60 seconds (TTL-based, not full session).
    clear=True resets the session action log."""
    _LOOP_TTL_SECS = 60  # only flag as loop if repeated within this window
    if not action and not clear:
        return {"ok": False, "error": "action is required (or pass clear=True to reset)"}
    try:
        clear_bool = clear is True or str(clear).lower() in ("true", "1", "yes")
        # Build action fingerprint
        fingerprint = f"{action.lower().strip()}::{context_hash}"
        now_ts = time.time()
        # Load existing action log from agentic_sessions
        # Format: list of {"fp": fingerprint, "ts": timestamp}
        rows = sb_get("agentic_sessions", f"session_id=eq.{session_id}&select=action_log", svc=True)
        action_log = []
        row_exists = bool(rows)
        if rows and rows[0].get("action_log"):
            raw = rows[0]["action_log"]
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            # Support both old format (list of strings) and new format (list of dicts)
            for entry in parsed:
                if isinstance(entry, dict):
                    action_log.append(entry)
                else:
                    # Migrate old string format — assign old ts so it expires immediately
                    action_log.append({"fp": entry, "ts": 0})
        if clear_bool:
            if row_exists:
                sb_patch("agentic_sessions", f"session_id=eq.{session_id}", {"action_log": json.dumps([])})
            return {"ok": True, "cleared": True, "session_id": session_id}

        # Evict expired entries (older than TTL)
        action_log = [e for e in action_log if now_ts - e.get("ts", 0) < _LOOP_TTL_SECS]

        # Check for loop — only within TTL window
        recent_fps = [e["fp"] for e in action_log]
        loop_detected = fingerprint in recent_fps
        previous_attempt = recent_fps.count(fingerprint)

        # Append new entry with timestamp
        action_log.append({"fp": fingerprint, "ts": now_ts})
        # Keep last 100 entries
        action_log = action_log[-100:]

        if row_exists:
            sb_patch("agentic_sessions", f"session_id=eq.{session_id}", {"action_log": json.dumps(action_log)})
        else:
            sb_post("agentic_sessions", {"session_id": session_id, "action_log": json.dumps(action_log), "created_at": datetime.utcnow().isoformat()})
        return {
            "ok": True,
            "action": action,
            "session_id": session_id,
            "loop_detected": loop_detected,
            "previous_attempts": previous_attempt,
            "ttl_seconds": _LOOP_TTL_SECS,
            "total_actions_logged": len(action_log),
            "recommendation": "Change approach or surface to owner" if loop_detected else "No loop — proceed",
            "instruction": f"STOP — this exact action was attempted {previous_attempt}x in last {_LOOP_TTL_SECS}s" if loop_detected else "Safe to proceed"
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["loop_detect"] = {"fn": t_loop_detect, "perm": "WRITE",
    "args": [
        {"name": "action", "type": "string", "description": "The action about to be taken"},
        {"name": "context_hash", "type": "string", "description": "Optional hash of relevant context to distinguish similar actions"},
        {"name": "session_id", "type": "string", "description": "Session identifier for scoping the loop log (default: default)"},
        {"name": "clear", "type": "string", "description": "true=reset this session's action log"}
    ],
    "desc": "AGI-14: Action loop detection. Tracks actions taken this session by fingerprint. Returns loop_detected if same action attempted before. CORE must change approach if loop detected — never retry blindly."}


# --- Gemini diagnostic test --------------------------------------------------

def t_test_gemini():
    """Test gemini_chat() end-to-end from Railway. Returns response or error."""
    try:
        from core_config import gemini_chat, _GEMINI_KEYS, _GEMINI_MODEL
        key_count = len(_GEMINI_KEYS)
        result = gemini_chat("you are a test assistant", "reply with just the word OK", max_tokens=10)
        return {"ok": True, "response": result, "key_count": key_count, "model": _GEMINI_MODEL}
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["test_gemini"] = {"fn": t_test_gemini, "perm": "READ", "args": [],
    "desc": "Diagnostic: test gemini_chat() end-to-end from Railway. Returns response or error."}


# --- AGI-01: Cross-Domain Synthesis ------------------------------------------

def t_synthesize_cross_domain():
    """AGI-01: Manual trigger for cross-domain synthesis.
    I.4: Runs in background thread -- _run_cross_domain_synthesis() is a long Groq call
    that blocks the MCP socket if run synchronously. Now fires async and returns immediately."""
    try:
        import threading
        from core_train import _run_cross_domain_synthesis
        t = threading.Thread(target=_run_cross_domain_synthesis, daemon=True)
        t.start()
        return {
            "ok": True,
            "message": "Cross-domain synthesis started in background. Check Railway logs for [SYNTH] output and Telegram for results.",
            "instruction": "Check railway_logs_live keyword=SYNTH to monitor progress. Returns when complete via Telegram."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["synthesize_cross_domain"] = {"fn": t_synthesize_cross_domain, "perm": "WRITE",
    "args": [],
    "desc": "AGI-01: Manual trigger for weekly cross-domain synthesis. Reads top patterns per domain, finds structural similarities via Groq, writes insights to knowledge_base(domain=synthesis). Also runs automatically every Wednesday."}


# -- interface MCP server reconciliation (16.D) --------------------------------
def _reconcile_interface_services(rows: list, inserted: list, tombstoned: list) -> None:
    """Auto-sync interface layer: ensure all known external MCP servers are tracked.
    This is a declarative manifest -- interface services don't auto-appear/disappear
    like files do. We register known servers and flag unknown ones as degraded.
    Called from t_system_map_scan(trigger='session_end') only.
    """
    # Canonical interface manifest -- update here when MCP servers are added/removed
    _INTERFACE_MANIFEST = {
        "zapier_mcp":          {"component": "zapier",           "role": "Zapier MCP bridge -- 164 tools (Gmail,GCal,Sheets,Docs,Drive,Notion,Todoist,GitHub,Webhooks)"},
        "desktop_commander":   {"component": "desktop_commander","role": "Local PC filesystem + process control -- 24 tools"},
        "windows_mcp":         {"component": "windows_mcp",      "role": "Windows OS automation -- 17 tools (PowerShell,App,Click,Type,Clipboard,Screenshot)"},
        "github_mcp":          {"component": "github_mcp",       "role": "GitHub API direct -- 26 tools (issues,PRs,commits,file_contents,branches)"},
        "filesystem_mcp":      {"component": "filesystem_mcp",   "role": "Local filesystem R/W -- 14 tools. Allowed: C:/, E:/, F:/"},
        "memory_mcp":          {"component": "memory_mcp",       "role": "Knowledge graph -- 9 tools (entities, relations, observations)"},
        "puppeteer_mcp":       {"component": "puppeteer_mcp",    "role": "Browser automation -- 7 tools (navigate, click, fill, screenshot, evaluate)"},
        "sqlite_mcp":          {"component": "sqlite_mcp",       "role": "SQLite local DB -- 6 tools (read_query, write_query, create_table)"},
        "pdf_tools_mcp":       {"component": "pdf_tools_mcp",    "role": "PDF fill/analyze/extract -- 11 tools"},
        "telegram":            {"component": "telegram",         "role": "Owner notification channel -- async events + Telegram bot"},
        "groq":                {"component": "groq",             "role": "LLM inference -- llama-3.3-70b + llama-3.1-8b-instant"},
        "claude.ai":           {"component": "claude",           "role": "Primary interface -- claude.ai / Claude Desktop MCP client"},
    }
    try:
        registered_interface = {
            row["name"]: row
            for row in rows
            if row.get("layer") == "interface"
            and row.get("item_type") == "service"
            and row.get("status") != "tombstone"
        }
        for svc_name, meta in _INTERFACE_MANIFEST.items():
            if svc_name not in registered_interface:
                try:
                    sb_post_critical("system_map", {
                        "layer": "interface",
                        "component": meta["component"],
                        "item_type": "service",
                        "name": svc_name,
                        "role": meta["role"],
                        "responsibility": "auto-registered by interface reconciliation",
                        "status": "active",
                        "updated_by": "session_end_auto",
                        "last_updated": datetime.utcnow().isoformat(),
                    })
                    inserted.append(f"interface:{svc_name}")
                    print(f"[SMAP] interface inserted: {svc_name}")
                except Exception as _ie:
                    print(f"[SMAP] interface insert {svc_name} failed: {_ie}")
    except Exception as e:
        print(f"[SMAP] _reconcile_interface_services error: {e}")


# -- local_pc skill file version reconciliation (16.E) -------------------------
def _reconcile_skill_versions(rows: list, inserted: list, tombstoned: list) -> None:
    """Auto-sync local_pc skill file entries: ensure current V8 is active,
    older versions (V3-V7) are tombstoned, no stale actives accumulate.
    Called from t_system_map_scan(trigger='session_end') only.
    """
    _SKILL_CURRENT = "CORE_AGI_SKILL_V8.md"
    _SKILL_STALE_PATTERN = "CORE_AGI_SKILL_V"  # prefix for all versioned skill files

    try:
        skill_rows = {
            row["name"]: row
            for row in rows
            if row.get("layer") == "local_pc"
            and row.get("item_type") == "file"
            and _SKILL_STALE_PATTERN in row.get("name", "")
            and row.get("status") != "tombstone"
        }

        # Ensure current skill is registered and active
        if _SKILL_CURRENT not in skill_rows:
            try:
                sb_post_critical("system_map", {
                    "layer": "local_pc",
                    "component": "local_pc",
                    "item_type": "file",
                    "name": _SKILL_CURRENT,
                    "role": "PRIMARY active skill file -- CORE identity + boot sequence",
                    "responsibility": "Loaded by Claude Desktop at session open. Current version.",
                    "status": "active",
                    "updated_by": "session_end_auto",
                    "last_updated": datetime.utcnow().isoformat(),
                })
                inserted.append(f"local_pc:{_SKILL_CURRENT}")
                print(f"[SMAP] skill inserted: {_SKILL_CURRENT}")
            except Exception as _ie:
                print(f"[SMAP] skill insert {_SKILL_CURRENT} failed: {_ie}")

        # Tombstone all non-current skill versions
        for sname, row in skill_rows.items():
            if sname != _SKILL_CURRENT:
                try:
                    sb_patch("system_map", f"id=eq.{row['id']}", {
                        "status": "tombstone",
                        "notes": f"auto-tombstoned: superseded by {_SKILL_CURRENT}",
                        "last_updated": datetime.utcnow().isoformat(),
                        "updated_by": "session_end_auto",
                    })
                    tombstoned.append(f"local_pc:{sname}")
                    print(f"[SMAP] skill tombstoned: {sname}")
                except Exception as _te:
                    print(f"[SMAP] skill tombstone {sname} failed: {_te}")

    except Exception as e:
        print(f"[SMAP] _reconcile_skill_versions error: {e}")



# =============================================================================
# SYSTEM MAP INTELLIGENCE LAYER
# =============================================================================
# What system_map IS:
#   CORE's living self-model. Ground truth of every component that makes up CORE:
#   tools, source files, Supabase tables, GitHub docs, local PC files, external
#   MCP services. If system_map is wrong, CORE makes wrong assumptions.
#
# When to sync:
#   - After every deploy (auto-wired via t_redeploy)
#   - Every 6h (background_researcher loop)
#   - At session_start if drift detected
#   - Manually via sync_system_map tool
#
# What sync does:
#   - Reconciles all 6 layers: tools, brain tables, executor files,
#     skeleton docs, interface services, local_pc skill versions
#   - Updates key_facts (volatile metrics: tool_count, row_count, line_count)
#   - Scores component health (active/degraded/tombstone)
#   - Notifies owner if significant drift found
# =============================================================================

def _update_volatile_key_facts(rows: list) -> list:
    """Update key_facts for all is_volatile=true rows with live metrics.
    Currently tracks: tool_count (core_tools.py), line_count (.py files),
    row_count (brain tables). Extends as new volatiles are registered.
    Returns list of updated component names.
    """
    updated = []
    live_tool_count = len(TOOLS)

    # Build live line counts for .py files in GitHub repo
    try:
        h = _ghh()
        r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/", headers=h, timeout=10)
        r.raise_for_status()
        github_files = {item["name"]: item.get("size", 0) for item in r.json() if item.get("type") == "file"}
    except Exception:
        github_files = {}

    for row in rows:
        if not row.get("is_volatile") or row.get("status") == "tombstone":
            continue
        row_id = row.get("id")
        if not row_id:
            continue
        kf = dict(row.get("key_facts") or {})
        new_kf = dict(kf)
        changed = False
        name = row.get("name", "")
        component = row.get("component", "")

        # core_tools.py -- track live tool_count
        if name == "core_tools.py" and component == "railway":
            if kf.get("tool_count") != live_tool_count:
                new_kf["tool_count"] = live_tool_count
                new_kf["last_tool_sync"] = datetime.utcnow().isoformat()
                changed = True

        # Any .py file in GitHub -- track approx line count from file size
        if name.endswith(".py") and component == "railway" and name in github_files:
            approx_lines = github_files[name] // 40  # ~40 bytes per line average
            if abs(kf.get("approx_lines", 0) - approx_lines) > 50:  # only update if diff > 50 lines
                new_kf["approx_lines"] = approx_lines
                new_kf["file_size_bytes"] = github_files[name]
                changed = True

        # Brain tables -- track row_count snapshot
        if row.get("layer") == "brain" and row.get("item_type") == "table":
            try:
                count_r = httpx.get(
                    f"{SUPABASE_URL}/rest/v1/{name}?select=id&limit=1",
                    headers=_sbh_count_svc(), timeout=8
                )
                row_count = int(count_r.headers.get("content-range", "*/0").split("/")[-1])
                if kf.get("row_count") != row_count:
                    new_kf["row_count"] = row_count
                    new_kf["row_count_ts"] = datetime.utcnow().isoformat()
                    changed = True
            except Exception:
                pass

        if changed:
            try:
                sb_patch("system_map", f"id=eq.{row_id}", {
                    "key_facts": new_kf,
                    "last_updated": datetime.utcnow().isoformat(),
                    "updated_by": "sync_system_map",
                })
                updated.append(name)
            except Exception as _ue:
                print(f"[SMAP] key_facts update failed for {name}: {_ue}")

    return updated


def t_sync_system_map(trigger: str = "manual", notify_on_changes: str = "true") -> dict:
    """Full system map sync -- CORE's self-model maintenance tool.

    PURPOSE: system_map is CORE's ground truth of what it is made of.
    This tool keeps it accurate so CORE never makes wrong assumptions about
    what tools exist, what tables are live, what files are active.

    WHEN TO CALL:
    - After any deploy (auto-called by t_redeploy)
    - At session_start if system_map_drift > 0 (auto-called by session_start)
    - When adding/removing MCP servers (manual)
    - When suspecting system drift (manual)

    WHAT IT DOES:
    1. Reconciles all 6 layers:
       - executor/tools    -- diffs TOOLS dict vs registered tools
       - brain/tables      -- diffs live Supabase tables vs registered tables
       - executor/files    -- diffs GitHub .py files vs registered files
       - skeleton/docs     -- diffs GitHub .md/.json files vs registered docs
       - interface/services-- checks known MCP servers are registered
       - local_pc/files    -- checks skill file versions, tombstones stale ones
    2. Updates key_facts volatile metrics (tool_count, row_count, line_count)
    3. Returns full drift report with insert/tombstone counts per layer
    4. Notifies owner via Telegram if significant changes detected

    trigger: manual|post_deploy|session_start|scheduled
    notify_on_changes: true=notify owner if any inserts/tombstones found
    """
    _notify = str(notify_on_changes).strip().lower() not in ("false", "0", "no")
    try:
        preflight = _require_external_service_preflight("supabase,github", "sync_system_map")
        if preflight:
            return preflight
        rows = sb_get(
            "system_map",
            "select=id,layer,component,item_type,name,role,responsibility,key_facts,is_volatile,status,notes"
            "&order=layer,component,name&limit=2000",
            svc=True
        )
        if not isinstance(rows, list):
            return {"ok": False, "error": "system_map query failed"}

        inserted = []
        tombstoned = []
        kf_updated = []

        # --- Layer reconciliation ---
        live_tool_names = set(TOOLS.keys())
        registered_tools = {
            row["name"]: row for row in rows
            if row.get("component") == "railway"
            and row.get("item_type") == "tool"
            and row.get("status") != "tombstone"
        }
        # Tools: insert missing
        for tool_name in sorted(live_tool_names - set(registered_tools.keys())):
            try:
                desc = TOOLS[tool_name].get("desc", "")
                perm = TOOLS[tool_name].get("perm", "READ")
                sb_upsert("system_map", {
                    "layer": "executor", "component": "railway", "item_type": "tool",
                    "name": tool_name, "role": (desc or f"MCP tool: {tool_name}")[:400],
                    "responsibility": f"perm={perm} -- auto-registered by sync_system_map",
                    "status": "active", "updated_by": f"sync_{trigger}",
                    "last_updated": datetime.utcnow().isoformat(),
                }, on_conflict="name,component,item_type")
                inserted.append(f"tool:{tool_name}")
            except Exception as _ie:
                print(f"[SMAP] tool insert {tool_name}: {_ie}")
        # Tools: tombstone removed
        for tool_name in sorted(set(registered_tools.keys()) - live_tool_names):
            try:
                sb_patch("system_map", f"id=eq.{registered_tools[tool_name]['id']}", {
                    "status": "tombstone",
                    "notes": f"auto-tombstoned by sync_{trigger}: not in TOOLS dict",
                    "last_updated": datetime.utcnow().isoformat(), "updated_by": f"sync_{trigger}",
                })
                tombstoned.append(f"tool:{tool_name}")
            except Exception as _te:
                print(f"[SMAP] tool tombstone {tool_name}: {_te}")

        # All other layers via existing reconcilers
        _reconcile_brain_tables(rows, inserted, tombstoned)
        _reconcile_executor_files(rows, inserted, tombstoned)
        _reconcile_skeleton_docs(rows, inserted, tombstoned)
        _reconcile_interface_services(rows, inserted, tombstoned)
        _reconcile_skill_versions(rows, inserted, tombstoned)

        # Update volatile key_facts metrics
        kf_updated = _update_volatile_key_facts(rows)

        # Build drift report
        drift = {
            "tools": {"live": len(live_tool_names), "registered": len(registered_tools)},
            "total_inserted": len(inserted),
            "total_tombstoned": len(tombstoned),
            "kf_updated": kf_updated,
            "inserted": inserted,
            "tombstoned": tombstoned,
        }
        has_changes = bool(inserted or tombstoned or kf_updated)

        # Notify owner if significant drift
        if _notify and (inserted or tombstoned):
            msg_parts = [f"[SMAP] system_map sync ({trigger})"]
            if inserted:   msg_parts.append(f"Inserted ({len(inserted)}): {', '.join(inserted[:8])}{'...' if len(inserted)>8 else ''}")
            if tombstoned: msg_parts.append(f"Tombstoned ({len(tombstoned)}): {', '.join(tombstoned[:8])}{'...' if len(tombstoned)>8 else ''}")
            if kf_updated: msg_parts.append(f"key_facts updated: {', '.join(kf_updated[:5])}")
            notify("\n".join(msg_parts))

        print(f"[SMAP] sync({trigger}): inserted={len(inserted)} tombstoned={len(tombstoned)} kf_updated={len(kf_updated)}")
        return {
            "ok": True, "trigger": trigger,
            "total_components": len([r for r in rows if r.get("status") != "tombstone"]),
            "has_changes": has_changes,
            "drift": drift,
        }
    except Exception as e:
        print(f"[SMAP] sync error: {e}")
        return {"ok": False, "error": str(e)}


TOOLS["sync_system_map"] = {"fn": t_sync_system_map, "perm": "WRITE",
    "args": [
        {"name": "trigger", "type": "string",
         "description": "Why this sync is happening: manual|post_deploy|session_start|scheduled"},
        {"name": "notify_on_changes", "type": "string",
         "description": "Send Telegram notification if drift found (default: true)"},
    ],
    "desc": (
        "CORE self-model sync. Reconciles ALL 6 system layers against live state: "
        "executor/tools (TOOLS dict), brain/tables (Supabase), executor/files (GitHub .py), "
        "skeleton/docs (GitHub .md/.json), interface/services (MCP servers), "
        "local_pc/skill_versions. Updates key_facts volatile metrics (tool_count, row_count, "
        "line_count). Call after deploys, when drift detected, or manually. "
        "NEVER assume system_map is current -- always sync first when making architecture decisions."
    )}



def t_tool_health_scan(force: str = "false") -> dict:
    """PHASE-M/M.1: Scan all CORE tools and classify each as healthy/degraded/broken/untested.

    Pulls tool_stats for every tool in TOOLS dict (last 30 days).
    Classification:
      broken:    fail_rate >= 0.5 OR last_error contains hard exception keywords
      degraded:  0.2 <= fail_rate < 0.5
      untested:  0 calls in tool_stats
      healthy:   fail_rate < 0.2 and has been called

    Updates system_map status per tool. Auto-queues improvement proposals for broken tools
    into evolution_queue as change_type=code. Notifies owner with summary.
    force=true: re-scan even if last scan < 6h ago.
    """
    try:
        _force = str(force).lower() in ("true", "1", "yes")

        # Rate-limit: skip if last scan < 6h ago (unless forced)
        if not _force:
            try:
                last_rows = sb_get("sessions",
                    "select=summary&summary=like.*tool_health_scan_ts*&order=id.desc&limit=1",
                    svc=True) or []
                if last_rows:
                    import re as _re2
                    m = _re2.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", last_rows[0].get("summary", ""))
                    if m:
                        last_ts = datetime.fromisoformat(m.group())
                        if (datetime.utcnow() - last_ts).total_seconds() < 21600:
                            return {"ok": True, "skipped": True,
                                    "reason": "last scan < 6h ago -- pass force=true to override"}
            except Exception:
                pass

        # Pull all tool_stats rows
        stats_rows = sb_get("tool_stats",
            "select=tool_name,call_count,success_count,fail_count,fail_rate,last_error"
            "&order=fail_rate.desc",
            svc=True) or []
        stats_by_name = {r["tool_name"]: r for r in stats_rows}
        fleet_summary = _summarize_tool_stats_rows(stats_rows)["summary"]

        all_tool_names = sorted(TOOLS.keys())
        results = []
        broken_tools = []
        degraded_tools = []
        untested_tools = []
        healthy_tools = []
        proposals_queued = 0
        _HARD_ERR_KW = ["Exception", "Error", "Traceback", "AttributeError",
                        "KeyError", "TypeError", "ValueError", "NoneType", "not found"]

        for tool_name in all_tool_names:
            stat = stats_by_name.get(tool_name)
            if not stat or (stat.get("call_count") or 0) == 0:
                classification = "untested"
                untested_tools.append(tool_name)
            else:
                fr = float(stat.get("fail_rate") or 0)
                last_err = str(stat.get("last_error") or "")
                has_hard_error = any(kw in last_err for kw in _HARD_ERR_KW)
                if fr >= 0.5 or (fr >= 0.3 and has_hard_error):
                    classification = "broken"
                    broken_tools.append(tool_name)
                elif fr >= 0.2:
                    classification = "degraded"
                    degraded_tools.append(tool_name)
                else:
                    classification = "healthy"
                    healthy_tools.append(tool_name)

            results.append({
                "tool": tool_name,
                "status": classification,
                "fail_rate": float((stat or {}).get("fail_rate") or 0),
                "total_calls": int((stat or {}).get("call_count") or 0),
                "last_error": str((stat or {}).get("last_error") or "")[:120],
            })

        # Update system_map status per tool
        for r in results:
            try:
                sm_rows = sb_get("system_map",
                    f"select=id&name=eq.{r['tool']}&item_type=eq.tool&status=neq.tombstone",
                    svc=True) or []
                if sm_rows:
                    sb_patch("system_map", f"id=eq.{sm_rows[0]['id']}", {
                        "status": "active" if r["status"] in ("healthy", "untested") else r["status"],
                        "notes": f"health_scan:{r['status']} fail={r['fail_rate']:.2f} {r['last_error'][:60]}",
                        "last_updated": datetime.utcnow().isoformat(),
                        "updated_by": "tool_health_scan",
                    })
            except Exception:
                pass

        # Auto-queue improvement proposals for broken tools (skip if already pending)
        for tool_name in broken_tools:
            try:
                existing = sb_get("evolution_queue",
                    f"select=id&status=eq.pending&pattern_key=eq.broken_tool:{tool_name}",
                    svc=True) or []
                if not existing:
                    stat = stats_by_name.get(tool_name, {})
                    sb_post("evolution_queue", {
                        "change_type": "code",
                        "change_summary": (
                            f"[AUTO] {tool_name} broken: "
                            f"fail_rate={float(stat.get('fail_rate',0)):.2f}, "
                            f"last_error={str(stat.get('last_error',''))[:150]}"
                        ),
                        "pattern_key": f"broken_tool:{tool_name}",
                        "confidence": 0.9,
                        "status": "pending",
                        "source": "tool_health_scan",
                        "impact": "high",
                        "recommendation": f"Run tool_improve(tool_name='{tool_name}') to get code fix",
                        "created_at": datetime.utcnow().isoformat(),
                    })
                    proposals_queued += 1
            except Exception:
                pass

        # Persist scan timestamp for rate-limiting
        try:
            sb_post("sessions", {
                "summary": f"[state_update] tool_health_scan_ts: {datetime.utcnow().isoformat()[:16]}",
                "actions": [f"tool_health_scan: {len(broken_tools)}b {len(degraded_tools)}d {len(untested_tools)}u {len(healthy_tools)}h"],
                "interface": "mcp",
            })
        except Exception:
            pass

        # Notify owner if attention needed
        if broken_tools or degraded_tools:
            parts = ["[HEALTH SCAN] Tool Health Report"]
            parts.append(
                f"Fleet: calls {fleet_summary['fleet_calls']} | fail_rate {fleet_summary['fleet_fail_rate']:.2f} | "
                f"healthy {fleet_summary['healthy_tools']} | flagged {fleet_summary['flagged_tools']}"
            )
            if broken_tools:
                parts.append(f"BROKEN ({len(broken_tools)}): {', '.join(broken_tools[:8])}")
            if degraded_tools:
                parts.append(f"DEGRADED ({len(degraded_tools)}): {', '.join(degraded_tools[:8])}")
            parts.append(f"Healthy: {len(healthy_tools)} | Untested: {len(untested_tools)}")
            if proposals_queued:
                parts.append(f"Queued {proposals_queued} improvement proposals -> evolution_queue")
            notify("\n".join(parts))

        return {
            "ok": True,
            "scanned": len(all_tool_names),
            "broken": broken_tools,
            "degraded": degraded_tools,
            "untested_count": len(untested_tools),
            "healthy_count": len(healthy_tools),
            "proposals_queued": proposals_queued,
            "fleet_summary": fleet_summary,
            "summary": (
                f"{len(broken_tools)} broken, {len(degraded_tools)} degraded, "
                f"{len(untested_tools)} untested, {len(healthy_tools)} healthy "
                f"out of {len(all_tool_names)} total tools"
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


TOOLS["tool_health_scan"] = {
    "fn": t_tool_health_scan,
    "perm": "WRITE",
    "args": [
        {"name": "force", "type": "string",
         "description": "true=force scan even if last scan < 6h ago (default: false)"},
    ],
    "desc": (
        "PHASE-M: Daily tool health scan. Classifies all CORE tools as healthy/degraded/broken/untested "
        "using tool_stats fail_rate + last_error. broken=fail_rate>=0.5, degraded>=0.2, untested=0 calls. "
        "Updates system_map status. Auto-queues improvement proposals for broken tools. "
        "Notifies owner. Rate-limited to once per 6h (pass force=true to override). "
        "Use tool_improve(tool_name=X) to get root_cause + code fix for any broken tool."
    ),
}


def t_tool_improve(tool_name: str = "") -> dict:
    """PHASE-M/M.2: Root-cause analysis + code fix proposal for a broken/degraded tool.

    Fetches full context: function source + tool_stats + mistakes + KB + system_map.
    Calls Groq to generate: root_cause, code_fix (old_str/new_str), test_description.
    Writes proposal to evolution_queue as change_type=code.
    Owner approves via check_evolutions + approve_evolution. NEVER auto-applied.
    """
    if not tool_name:
        return {"ok": False, "error": "tool_name is required (e.g. 'debug_fn' or 't_debug_fn')"}

    clean_name = tool_name[2:] if tool_name.startswith("t_") else tool_name
    fn_name = f"t_{clean_name}"

    try:
        # 1. Read function source
        fn_source = ""
        source_file = ""
        for mod_file in ["core_tools.py", "core_train.py", "core_config.py",
                         "core_github.py", "core_main.py"]:
            result = t_core_py_fn(fn_name, file=mod_file)
            if result.get("ok"):
                fn_source = result.get("source", "")
                source_file = mod_file
                break
        if not fn_source:
            return {"ok": False, "error": f"{fn_name} not found in any CORE module",
                    "tool_name": clean_name}

        # 2. tool_stats
        stat_rows = sb_get("tool_stats",
            f"select=call_count,fail_count,fail_rate,last_error"
            f"&tool_name=eq.{clean_name}",
            svc=True) or []
        stat = stat_rows[0] if stat_rows else {}

        # 3. Mistakes mentioning this tool
        mistakes = sb_get("mistakes",
            f"select={_sel_force('mistakes', ['what_failed','root_cause','correct_approach','severity'])}"
            f"&or=(context.ilike.*{clean_name[:25]}*,what_failed.ilike.*{clean_name[:25]}*)"
            f"&order=created_at.desc&limit=5&id=gt.1",
            svc=True) or []

        # 4. KB entries
        kb = t_search_kb(query=clean_name, limit=4) or []

        # 5. System map notes
        sm = sb_get("system_map",
            f"select=role,status,notes&name=eq.{clean_name}&item_type=eq.tool",
            svc=True) or []
        sm_entry = sm[0] if sm else {}

        system = (
            "You are CORE's self-improvement engine. Analyze a broken Python function and produce a fix.\n"
            "Output ONLY valid JSON:\n"
            "{\n"
            '  "root_cause": "precise 1-2 sentence technical diagnosis",\n'
            '  "code_fix": {"description": "what changes", "old_str": "exact code to replace", "new_str": "replacement"} or null if no code fix needed,\n'
            '  "test_description": "how to verify the fix",\n'
            '  "confidence": 0.0-1.0,\n'
            '  "severity": "critical|high|medium|low"\n'
            "}\n"
            "For old_str: copy verbatim from the function source -- must be a unique substring."
        )

        mistake_text = "\n".join(
            f"- [{m.get('severity','?')}] {m.get('what_failed','')[:100]}"
            for m in mistakes
        ) or "none"
        kb_text = "\n".join(
            f"- {k.get('topic','')}: {str(k.get('content',''))[:120]}"
            for k in kb
        ) or "none"

        user = (
            f"TOOL: {fn_name} (in {source_file})\n"
            f"fail_rate={stat.get('fail_rate','?')} calls={stat.get('call_count',0)} "
            f"failures={stat.get('fail_count',0)}\n"
            f"last_error: {str(stat.get('last_error','none'))[:300]}\n\n"
            f"SOURCE:\n{fn_source[:3000]}\n\n"
            f"RELATED MISTAKES:\n{mistake_text}\n\n"
            f"KB:\n{kb_text}\n\n"
            f"SYSTEM MAP: status={sm_entry.get('status','?')} notes={str(sm_entry.get('notes',''))[:150]}\n\n"
            "Diagnose root_cause and produce code_fix."
        )

        raw = groq_chat(system, user, max_tokens=1500)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        import json as _j2
        proposal = _j2.loads(raw)

        diff_blob = _j2.dumps({
            "tool_name": clean_name,
            "fn_name": fn_name,
            "source_file": source_file,
            "code_fix": proposal.get("code_fix"),
            "test_description": proposal.get("test_description"),
            "generated_by": "tool_improve",
        })

        evo_ok = sb_post("evolution_queue", {
            "change_type": "code",
            "change_summary": f"[tool_improve] {fn_name}: {str(proposal.get('root_cause',''))[:200]}",
            "diff_content": diff_blob,
            "pattern_key": f"tool_improve:{clean_name}",
            "confidence": float(proposal.get("confidence", 0.7)),
            "status": "pending",
            "source": "tool_improve",
            "impact": proposal.get("severity", "medium"),
            "recommendation": (
                f"Apply code_fix to {fn_name} in {source_file}. "
                f"Test: {str(proposal.get('test_description',''))[:150]}"
            ),
            "created_at": datetime.utcnow().isoformat(),
        })

        evo_id = None
        if evo_ok:
            new_evo = sb_get("evolution_queue",
                f"select=id&pattern_key=eq.tool_improve:{clean_name}&order=id.desc&limit=1",
                svc=True) or []
            evo_id = new_evo[0]["id"] if new_evo else None

        notify(
            f"[TOOL IMPROVE] {fn_name}\n"
            f"Root: {str(proposal.get('root_cause',''))[:150]}\n"
            f"Confidence: {proposal.get('confidence','?')}\n"
            f"Evo #{evo_id} pending owner approval"
        )

        return {
            "ok": True,
            "tool_name": clean_name,
            "fn_name": fn_name,
            "source_file": source_file,
            "root_cause": proposal.get("root_cause"),
            "code_fix": proposal.get("code_fix"),
            "test_description": proposal.get("test_description"),
            "confidence": proposal.get("confidence"),
            "severity": proposal.get("severity"),
            "evolution_id": evo_id,
            "instruction": (
                f"Review evolution #{evo_id}. "
                "If code_fix.old_str/new_str looks correct, call approve_evolution. "
                "NEVER auto-apply code evolutions -- owner review required."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "tool_name": clean_name}


TOOLS["tool_improve"] = {
    "fn": t_tool_improve,
    "perm": "WRITE",
    "args": [
        {"name": "tool_name", "type": "string",
         "description": "Tool to analyze (e.g. 'debug_fn'). t_ prefix optional."},
    ],
    "desc": (
        "PHASE-M: Generate root_cause + code fix for any broken/degraded CORE tool. "
        "Reads source + fail history + mistakes + KB, calls Groq for diagnosis. "
        "Writes evolution_queue entry (change_type=code) for owner approval. "
        "NEVER auto-applied. Owner reviews via check_evolutions then approve_evolution. "
        "Returns: root_cause, code_fix (old_str/new_str), test_description, evolution_id."
    ),
}


def t_load_arch_context(domain: str = "general") -> dict:
    """PHASE-M/M.5: Load full architectural context before any code write or architecture decision.

    HARD RULE: Call this before creating new tools, making architecture changes,
    or starting any major patch session. CORE must never write code from memory.

    Loads in one call:
      1. system_map snapshot (layer counts, live tool count)
      2. _SB_SCHEMA table list (what tables exist, pk types, fat columns)
      3. TOOLS stats (total, perm distribution, deprecated tools)
      4. Active behavioral rules for the domain (top 20 by priority)
      5. Last 5 cold_reflection summaries
      6. Open tasks (pending + in_progress)
    """
    try:
        # 1. System map
        sm_rows = sb_get("system_map",
            "select=layer,component,item_type,name,status,key_facts"
            "&status=neq.tombstone&order=layer,component,name",
            svc=True) or []
        layer_summary: dict = {}
        live_tool_count = len(TOOLS)  # authoritative: TOOLS dict
        for row in sm_rows:
            key = f"{row.get('layer','?')}/{row.get('item_type','?')}"
            layer_summary[key] = layer_summary.get(key, 0) + 1
            kf = row.get("key_facts") or {}
            if row.get("name") == "core_tools.py" and kf.get("tool_count"):
                live_tool_count = kf["tool_count"]

        # 2. Schema tables from _SB_SCHEMA
        schema_summary = {}
        try:
            for tname, tdef in _SB_SCHEMA.get("tables", {}).items():
                schema_summary[tname] = {
                    "pk_type": tdef.get("pk_type", "?"),
                    "fat_columns": tdef.get("fat_columns", []),
                    "tombstone": tdef.get("tombstone", False),
                }
        except Exception as _se:
            schema_summary = {"error": str(_se)}

        # 3. TOOLS stats
        perm_dist: dict = {}
        deprecated = []
        for tname, tdef in TOOLS.items():
            perm = tdef.get("perm", "READ")
            perm_dist[perm] = perm_dist.get(perm, 0) + 1
            if "DEPRECATED" in str(tdef.get("desc", "")).upper():
                deprecated.append(tname)
        tools_context = {
            "total": len(TOOLS),
            "live": len(TOOLS) - len(deprecated),
            "deprecated": deprecated,
            "perm_distribution": perm_dist,
        }

        # 4. Behavioral rules
        rules = sb_get("behavioral_rules",
            f"select={_sel_force('behavioral_rules', ['trigger','pointer','full_rule','priority','confidence'])}"
            f"&active=eq.true"
            f"&or=(domain=eq.{domain},domain=eq.universal)"
            f"&order=priority.desc&limit=20&id=gt.1",
            svc=True) or []

        # 5. Recent cold_reflections
        cold = sb_get("cold_reflections",
            f"select={_sel_force('cold_reflections', ['created_at','patterns_found','evolutions_queued','summary_text'])}"
            "&order=id.desc&limit=5",
            svc=True) or []

        # 6. Open tasks
        tasks = sb_get("task_queue",
            "select=id,task,status,priority&status=in.(pending,in_progress)"
            "&order=priority.desc&limit=15",
            svc=True) or []
        task_list = []
        for t in tasks:
            raw = t.get("task", "")
            title = raw[:100] if isinstance(raw, str) else str(raw)[:100]
            task_list.append({
                "id": str(t.get("id", ""))[:8],
                "status": t.get("status"),
                "priority": t.get("priority"),
                "title": title,
            })

        return {
            "ok": True,
            "domain": domain,
            "system_map": {
                "total_active": len(sm_rows),
                "layer_breakdown": layer_summary,
                "live_tool_count": live_tool_count,
            },
            "schema_tables": schema_summary,
            "tools": tools_context,
            "behavioral_rules": [
                {
                    "trigger": r.get("trigger"),
                    "pointer": r.get("pointer"),
                    "rule_preview": str(r.get("full_rule", ""))[:200],
                    "priority": r.get("priority"),
                    "confidence": r.get("confidence"),
                }
                for r in rules
            ],
            "recent_cold_reflections": [
                {
                    "date": str(c.get("created_at", ""))[:10],
                    "patterns_found": c.get("patterns_found"),
                    "evolutions_queued": c.get("evolutions_queued"),
                    "summary": str(c.get("summary_text", ""))[:200],
                }
                for c in cold
            ],
            "open_tasks": task_list,
            "instruction": (
                "Full architectural context loaded. Before writing any code: "
                "(1) check schema_tables for table structure and fat columns, "
                "(2) check tools.deprecated before referencing any tool by name, "
                "(3) check behavioral_rules for constraints on your domain, "
                "(4) check open_tasks to avoid duplicating planned work. "
                "Context is live from Supabase -- not from memory."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


TOOLS["load_arch_context"] = {
    "fn": t_load_arch_context,
    "perm": "READ",
    "args": [
        {"name": "domain", "type": "string",
         "description": "Domain for behavioral rules filter (default: general)"},
    ],
    "desc": (
        "PHASE-M: Load full architectural context before any code write or architecture decision. "
        "Returns: system_map snapshot, schema_tables (_SB_SCHEMA), TOOLS stats (total/deprecated), "
        "active behavioral rules for domain, last 5 cold_reflections, open tasks. "
        "HARD RULE: call before any new tool creation, architecture change, or major patch session. "
        "CORE must never write code from memory -- always load live context first."
    ),
}



# get_table_schema is registered by core_web._register_web_tools — do not duplicate here



# =============================================================================
# SELF-EDITING TOOLKIT
# Tools for CORE to edit its own codebase from anywhere — Telegram, Claude Desktop
# t_replace_fn:    replace entire function by name (no old_str needed)
# t_smart_patch:   plain-English description → Gemini generates old_str/new_str
# t_register_tool: atomically add new function + TOOLS entry in one commit
# =============================================================================

def t_replace_fn(fn_name: str, new_code: str, file: str = "core_tools.py",
                 message: str = "", repo: str = "", dry_run: str = "false") -> dict:
    """Replace an entire function in a GitHub file by name.
    Finds function boundaries automatically — no need for exact old_str.
    Supports any size function. Syntax-checks before pushing.
    fn_name: function name without 'def' keyword (e.g. 't_add_knowledge').
    new_code: complete replacement function including def line and full body.
    dry_run: pass 'true' to preview diff without pushing.
    """
    import py_compile as _pc, tempfile as _tf, difflib as _dl
    try:
        repo = repo or GITHUB_REPO
        content = _gh_blob_read(file, repo)
        lines = content.splitlines(keepends=True)

        # Find function start (handles both module-level and class method)
        start_idx = None
        base_indent = 0
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if (stripped.startswith(f"def {fn_name}(") or
                stripped.startswith(f"async def {fn_name}(")):
                start_idx = i
                base_indent = len(line) - len(line.lstrip())
                break

        if start_idx is None:
            return {"ok": False, "error": f"Function '{fn_name}' not found in {file}",
                    "hint": f"Use search_in_file to confirm exact name"}

        # Find function end — next def/class/TOOLS at same or lower indent, or EOF
        end_idx = start_idx + 1
        while end_idx < len(lines):
            line = lines[end_idx]
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                end_idx += 1
                continue
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent <= base_indent and (
                stripped.startswith("def ") or
                stripped.startswith("async def ") or
                stripped.startswith("class ") or
                stripped.startswith("TOOLS[") or
                stripped.startswith("@")
            ):
                break
            end_idx += 1

        old_fn = "".join(lines[start_idx:end_idx])

        # Normalize new_code trailing newline
        nc = new_code.strip() + "\n"
        # Re-indent if inside class
        if base_indent > 0:
            indent_str = " " * base_indent
            nc = "\n".join(
                indent_str + l if l.strip() else l
                for l in nc.splitlines()
            ) + "\n"

        new_content = content.replace(old_fn, nc, 1)
        if new_content == content:
            return {"ok": False, "error": "Replace had no effect — function boundary issue or identical code"}

        # Syntax check
        if file.endswith(".py"):
            with _tf.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tf:
                tf.write(new_content); tmp = tf.name
            try:
                _pc.compile(tmp, doraise=True)
            except _pc.PyCompileError as ce:
                import os; os.unlink(tmp)
                return {"ok": False, "error": f"SYNTAX ERROR — not pushed: {ce}"}
            finally:
                import os
                if os.path.exists(tmp): os.unlink(tmp)

        diff = "".join(_dl.unified_diff(
            old_fn.splitlines(keepends=True), nc.splitlines(keepends=True),
            fromfile=f"{file}:{fn_name} (before)", tofile=f"{file}:{fn_name} (after)", n=2,
        ))[:2000]

        if str(dry_run).lower() == "true":
            return {"ok": True, "dry_run": True, "fn_name": fn_name, "file": file,
                    "old_lines": end_idx - start_idx,
                    "new_lines": len(nc.splitlines()), "diff": diff}

        commit_sha = _gh_blob_write(file, new_content,
            message or f"replace_fn: {fn_name} in {file}", repo)
        return {"ok": True, "dry_run": False, "fn_name": fn_name, "file": file,
                "start_line": start_idx + 1,
                "old_lines": end_idx - start_idx,
                "new_lines": len(nc.splitlines()),
                "commit": commit_sha[:12] if commit_sha else None,
                "diff": diff}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_smart_patch(file: str, instruction: str, repo: str = "",
                  dry_run: str = "false", context_lines: int = 80) -> dict:
    """High-level intelligent patch: describe what to change in plain English.
    CORE reads the relevant section, uses Gemini to generate old_str/new_str,
    then applies via _patch_find with full syntax check.
    instruction: e.g. 'Fix tags handling to accept list or string in t_add_knowledge'
                      'Change timeout from 15 to 30 in sb_get'
    dry_run: 'true' to preview diff without pushing.
    """
    import py_compile as _pc2, tempfile as _tf2, difflib as _dl2, re as _re2
    try:
        repo = repo or GITHUB_REPO
        content = _gh_blob_read(file, repo)
        lines = content.splitlines()
        total = len(lines)

        # Find anchor line via keyword matching
        stop = {"the","a","an","is","are","to","of","and","or","in","fix","add",
                "change","update","make","handle","for","with","from","that","this"}
        kws = [w for w in _re2.findall(r"[a-zA-Z_]{4,}", instruction.lower()) if w not in stop][:5]
        anchor = 0
        best = 0
        for i, line in enumerate(lines):
            score = sum(1 for kw in kws if kw in line.lower())
            if score > best:
                best = score
                anchor = i

        # Extract context window
        cs = max(0, anchor - context_lines // 2)
        ce = min(total, anchor + context_lines // 2)
        ctx = "\n".join(f"{cs+i+1:4d}  {l}" for i, l in enumerate(lines[cs:ce]))

        # Ask Gemini for patch
        system = (
            "You are a Python code patcher. Given file context and instruction, "
            "output ONLY a JSON object with keys: "
            "old_str (exact substring to replace, must exist verbatim), "
            "new_str (replacement code), "
            "explanation (one sentence). "
            "old_str must be unique in the file. new_str must be valid Python. "
            "Output ONLY valid JSON."
        )
        user = (
            f"FILE: {file}\n"
            f"INSTRUCTION: {instruction}\n\n"
            f"FILE CONTEXT (lines {cs+1}-{ce}):\n{ctx}\n\n"
            "Generate the patch JSON."
        )
        raw = gemini_chat(system, user, max_tokens=1500, json_mode=True)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        patch = json.loads(raw)

        old_str = patch.get("old_str", "")
        new_str = patch.get("new_str", "")
        explanation = patch.get("explanation", "")

        if not old_str:
            return {"ok": False, "error": "Gemini returned empty old_str", "raw": raw[:300]}

        # Apply with fuzzy matching
        pf = _patch_find(content, old_str)
        found, count, matched, hint = pf[0], pf[1], pf[2], pf[3]
        auto_context = pf[4] if len(pf) > 4 else None

        if not found:
            return {"ok": False, "error": "Generated old_str not found in file",
                    "hint": hint, "auto_context": auto_context,
                    "llm_old_str_preview": old_str[:200],
                    "suggestion": "Try more specific instruction or use gh_search_replace directly"}
        if count > 1:
            return {"ok": False, "error": f"Generated old_str ambiguous ({count} matches)",
                    "llm_old_str_preview": old_str[:200]}

        new_content = content.replace(matched, new_str, 1)

        # Syntax check
        if file.endswith(".py"):
            with _tf2.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tf:
                tf.write(new_content); tmp = tf.name
            try:
                _pc2.compile(tmp, doraise=True)
            except _pc2.PyCompileError as ce2:
                import os; os.unlink(tmp)
                return {"ok": False, "error": f"SYNTAX ERROR in patch: {ce2}"}
            finally:
                import os
                if os.path.exists(tmp): os.unlink(tmp)

        diff = "".join(_dl2.unified_diff(
            content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"{file} (before)", tofile=f"{file} (after)", n=2,
        ))[:2000]

        if str(dry_run).lower() == "true":
            return {"ok": True, "dry_run": True, "file": file,
                    "explanation": explanation, "diff": diff,
                    "match_note": hint or "exact_match"}

        commit_sha = _gh_blob_write(file, new_content,
            f"smart_patch: {instruction[:80]}", repo)
        return {"ok": True, "dry_run": False, "file": file,
                "explanation": explanation,
                "commit": commit_sha[:12] if commit_sha else None,
                "diff": diff, "match_note": hint or "exact_match"}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Gemini returned invalid JSON: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_register_tool(name: str, fn_code: str, desc: str,
                    args_list: str = "", perm: str = "READ",
                    message: str = "", dry_run: str = "false") -> dict:
    """Atomically add a new tool to core_tools.py: function + TOOLS entry in one commit.
    name: tool name without t_ prefix (e.g. 'my_tool' → creates t_my_tool).
    fn_code: complete function source starting with 'def t_{name}(...)'.
    desc: tool description for TOOLS registry.
    args_list: comma-separated arg names e.g. 'path,content,message'.
    perm: READ | WRITE | EXECUTE.
    dry_run: 'true' to preview without pushing.
    Guards: blocks if fn already defined or TOOLS key already registered.
    """
    import py_compile as _pc3, tempfile as _tf3
    try:
        repo = GITHUB_REPO
        content = _gh_blob_read("core_tools.py", repo)

        fn_full = f"t_{name}" if not name.startswith("t_") else name
        tool_key = fn_full[2:] if fn_full.startswith("t_") else name

        # Duplicate guards
        if f"def {fn_full}(" in content:
            return {"ok": False, "error": f"BLOCKED: {fn_full} already defined in core_tools.py"}
        if f'TOOLS["{tool_key}"]' in content:
            return {"ok": False, "error": f'BLOCKED: TOOLS["{tool_key}"] already registered'}

        # Build args dicts for TOOLS entry
        args = [a.strip() for a in args_list.split(",") if a.strip()]
        args_json = json.dumps([{"name": a, "type": "string"} for a in args])

        tools_entry = (
            f'\n\nTOOLS["{tool_key}"] = {{\n'
            f'    "fn": {fn_full},\n'
            f'    "perm": "{perm}",\n'
            f'    "args": {args_json},\n'
            f'    "desc": {json.dumps(desc)},\n'
            f'}}\n'
        )

        fn_block = "\n\n" + fn_code.strip() + "\n"
        insert_marker = "# -- core_web tools registration"
        if insert_marker in content:
            new_content = content.replace(insert_marker,
                fn_block + tools_entry + "\n" + insert_marker, 1)
        else:
            new_content = content + fn_block + tools_entry

        # Full syntax check
        with _tf3.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tf:
            tf.write(new_content); tmp = tf.name
        try:
            _pc3.compile(tmp, doraise=True)
        except _pc3.PyCompileError as ce3:
            import os; os.unlink(tmp)
            return {"ok": False, "error": f"SYNTAX ERROR — not pushed: {ce3}",
                    "hint": "Check fn_code for syntax errors"}
        finally:
            import os
            if os.path.exists(tmp): os.unlink(tmp)

        if str(dry_run).lower() == "true":
            return {"ok": True, "dry_run": True, "tool_name": tool_key,
                    "fn_name": fn_full, "fn_lines": len(fn_code.splitlines()),
                    "preview_fn": fn_code[:300], "preview_entry": tools_entry[:300]}

        commit_sha = _gh_blob_write("core_tools.py", new_content,
            message or f"register_tool: {tool_key} ({perm})", repo)
        notify(f"[SELF-EXTEND] New tool registered: {tool_key} ({perm})")
        return {"ok": True, "tool_name": tool_key, "fn_name": fn_full,
                "perm": perm, "args": args,
                "commit": commit_sha[:12] if commit_sha else None,
                "note": "Tool registered. Restart MCP session to see it (client caches tool list)."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


TOOLS["replace_fn"] = {
    "fn": t_replace_fn,
    "perm": "EXECUTE",
    "args": [
        {"name": "fn_name",  "type": "string", "description": "Function name to replace (e.g. 't_add_knowledge')"},
        {"name": "new_code", "type": "string", "description": "Complete replacement function source"},
        {"name": "file",     "type": "string", "description": "File path (default: core_tools.py)"},
        {"name": "message",  "type": "string", "description": "Commit message"},
        {"name": "repo",     "type": "string", "description": "GitHub repo (default: CORE repo)"},
        {"name": "dry_run",  "type": "string", "description": "true to preview without pushing"},
    ],
    "desc": (
        "Replace an entire function by name — no need for exact old_str. "
        "Finds function boundaries automatically. Syntax-checks before pushing. "
        "Supports dry_run=true. USE THIS instead of multi_patch for full function replacements. "
        "Works on any size function in any CORE source file."
    ),
}

TOOLS["smart_patch"] = {
    "fn": t_smart_patch,
    "perm": "EXECUTE",
    "args": [
        {"name": "file",          "type": "string", "description": "File path to patch"},
        {"name": "instruction",   "type": "string", "description": "Plain English description of the change"},
        {"name": "repo",          "type": "string", "description": "GitHub repo (default: CORE repo)"},
        {"name": "dry_run",       "type": "string", "description": "true to preview without pushing"},
        {"name": "context_lines", "type": "string", "description": "Lines of context to read around match (default: 80)"},
    ],
    "desc": (
        "Intelligent patch: describe what to change in plain English — Gemini generates old_str/new_str. "
        "Example: smart_patch(file='core_tools.py', instruction='Fix tags to accept list or string in t_add_knowledge'). "
        "Supports dry_run=true. Syntax-checked before push. "
        "Fallback: use gh_search_replace if smart_patch cannot find the right location."
    ),
}

TOOLS["register_tool"] = {
    "fn": t_register_tool,
    "perm": "EXECUTE",
    "args": [
        {"name": "name",      "type": "string", "description": "Tool name without t_ prefix"},
        {"name": "fn_code",   "type": "string", "description": "Complete function source starting with def t_{name}(...)"},
        {"name": "desc",      "type": "string", "description": "Tool description for TOOLS registry"},
        {"name": "args_list", "type": "string", "description": "Comma-separated arg names e.g. 'path,content,message'"},
        {"name": "perm",      "type": "string", "description": "READ | WRITE | EXECUTE"},
        {"name": "message",   "type": "string", "description": "Commit message"},
        {"name": "dry_run",   "type": "string", "description": "true to preview without pushing"},
    ],
    "desc": (
        "Self-extension: atomically add a new tool to CORE in one commit. "
        "Appends function + TOOLS entry to core_tools.py. "
        "Guards against duplicates. Full syntax check before push. Supports dry_run=true. "
        "NOTE: new tool visible after MCP session restart (client caches tool list). "
        "USE THIS when CORE needs to permanently add a new capability."
    ),
}

# -- P1-04: Register previously unregistered AGI tools -----------------------
TOOLS["circuit_breaker"] = {
    "fn":   t_circuit_breaker,
    "perm": "READ",
    "args": [
        {"name": "failed_step",      "type": "string", "description": "The step that failed"},
        {"name": "dependent_steps",  "type": "string", "description": "Comma-separated downstream steps that depend on failed_step"},
        {"name": "failure_reason",   "type": "string", "description": "Why the step failed"},
    ],
    "desc": (
        "AGI-14: Failure cascade prevention. Given a failed step and its dependents, "
        "returns cascade_risk per downstream step, safe_to_continue, steps_to_suspend. "
        "Auto-wired by orchestrator on 3+ consecutive TOOL_FAILEDs. "
        "Also call manually before abandoning a multi-step task mid-way."
    ),
}
TOOLS["assert_source"] = {
    "fn":   t_assert_source,
    "perm": "READ",
    "args": [
        {"name": "value",            "type": "string", "description": "The value being asserted"},
        {"name": "declared_source",  "type": "string", "description": "observed | inferred | assumed | skill_file"},
        {"name": "field_name",       "type": "string", "description": "What field/variable this value is for"},
    ],
    "desc": (
        "AGI-11: Assumption grounding. Tags a value with its certainty source. "
        "Returns flagged=True if source=assumed or skill_file — forces a live query instead. "
        "Use before any action that depends on a value you have not confirmed this session. "
        "EXAMPLE: assert_source(value='task_id', declared_source='memory', field_name='task_id') → flagged=True → go query task_queue."
    ),
}
TOOLS["scope_tracker"] = {
    "fn":   t_scope_tracker,
    "perm": "READ",
    "args": [
        {"name": "planned_scope",  "type": "string", "description": "Original task scope as described at start"},
        {"name": "actions_taken",  "type": "string", "description": "Comma-separated list of actions taken so far"},
        {"name": "task_id",        "type": "string", "description": "Optional task UUID for reference"},
    ],
    "desc": (
        "AGI-12: Scope creep detection. Compares planned_scope vs actions_taken. "
        "Returns scope_exceeded (bool), drift_level (none|minor|moderate|severe), "
        "unplanned_items, overlap_percent, recommendation (continue|flag_owner|stop). "
        "Call after every 5-10 tool calls on long tasks to catch goal drift early."
    ),
}


# -- P2-03: Cross-session goal tracker ----------------------------------------

def t_set_goal(goal: str = "", domain: str = "general") -> dict:
    """P2-03: Register an active goal for cross-session tracking.
    goal: description of the goal (e.g. 'Complete CORE Phase 2 by end of month').
    domain: project domain (e.g. project:lsei, core_agi, general).
    Deduplicates: if an active goal with the same text exists, returns existing.
    Returns: {ok, goal_id, goal, domain, action: created|exists}
    """
    if not goal or not goal.strip():
        return {"ok": False, "error": "goal text required"}
    try:
        existing = sb_get(
            "session_goals",
            "select=id,goal,domain&status=eq.active&limit=20",
            svc=True,
        ) or []
        for ex in existing:
            if ex.get("goal", "").strip().lower() == goal.strip().lower():
                return {
                    "ok": True, "action": "exists",
                    "goal_id": ex["id"], "goal": ex["goal"], "domain": ex.get("domain", ""),
                    "message": "Goal already active — use update_goal_progress to add notes.",
                }
        ok = sb_post("session_goals", {
            "goal":       goal.strip(),
            "domain":     domain.strip() or "general",
            "progress":   "",
            "status":     "active",
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        })
        if not ok:
            return {"ok": False, "error": "Failed to insert goal"}
        new_row = sb_get(
            "session_goals",
            "select=id,goal,domain&status=eq.active&order=id.desc&limit=1",
            svc=True,
        ) or []
        goal_id = new_row[0]["id"] if new_row else None
        return {"ok": True, "action": "created", "goal_id": goal_id, "goal": goal, "domain": domain}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_update_goal_progress(goal_id: str = "", progress_note: str = "",
                            status: str = "") -> dict:
    """P2-03: Append a timestamped progress note to an active goal. Optionally update status.
    goal_id: ID from set_goal or get_active_goals.
    progress_note: what happened this session toward this goal.
    status: optional — active | completed | paused. Leave empty to keep current.
    Returns: {ok, goal_id, appended, status}
    """
    if not goal_id or not progress_note.strip():
        return {"ok": False, "error": "goal_id and progress_note required"}
    try:
        gid = int(goal_id)
        rows = sb_get("session_goals", f"select=progress,status&id=eq.{gid}&limit=1", svc=True) or []
        if not rows:
            return {"ok": False, "error": f"Goal {gid} not found"}
        existing_progress = rows[0].get("progress") or ""
        ts = datetime.utcnow().strftime("%Y-%m-%d")
        new_progress = (existing_progress + f"\n[{ts}] {progress_note.strip()}").strip()
        updates = {"progress": new_progress, "updated_at": datetime.utcnow().isoformat()}
        if status and status in ("active", "completed", "paused"):
            updates["status"] = status
        ok = sb_patch("session_goals", f"id=eq.{gid}", updates)
        return {
            "ok": ok, "goal_id": gid,
            "appended": progress_note.strip()[:100],
            "status": updates.get("status", rows[0].get("status")),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_get_active_goals(domain: str = "") -> dict:
    """P2-03: Return all active goals with progress history.
    Automatically called by session_start — result is in the active_goals field.
    domain: optional filter. Empty = all active goals.
    Returns: {ok, goals: [{id, goal, domain, progress, status}], count}
    """
    try:
        filters = "status=eq.active&order=created_at.asc&limit=20"
        if domain and domain.strip():
            filters = f"status=eq.active&domain=eq.{domain.strip()}&order=created_at.asc&limit=20"
        rows = sb_get(
            "session_goals",
            f"select=id,goal,domain,progress,status,created_at,updated_at&{filters}",
            svc=True,
        ) or []
        return {"ok": True, "goals": rows, "count": len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


TOOLS["set_goal"] = {
    "fn":   t_set_goal,
    "perm": "WRITE",
    "args": [
        {"name": "goal",   "type": "string", "description": "Goal description"},
        {"name": "domain", "type": "string", "description": "Domain/project context (e.g. core_agi, project:lsei, general)"},
    ],
    "desc": (
        "P2-03: Register a cross-session goal. Persists across all sessions, injected into "
        "session_start as active_goals. Deduplicates by goal text. "
        "EXAMPLE: set_goal(goal='Complete Phase 2 by month end', domain='core_agi')"
    ),
}

TOOLS["update_goal_progress"] = {
    "fn":   t_update_goal_progress,
    "perm": "WRITE",
    "args": [
        {"name": "goal_id",       "type": "string", "description": "Goal ID from set_goal or get_active_goals"},
        {"name": "progress_note", "type": "string", "description": "What happened this session toward this goal"},
        {"name": "status",        "type": "string", "description": "Optional: active | completed | paused"},
    ],
    "desc": (
        "P2-03: Append a timestamped progress note to an active goal. Call at session_end "
        "when work was done toward a tracked goal. Optionally mark completed or paused. "
        "EXAMPLE: update_goal_progress(goal_id='3', progress_note='P2-02 done, behavioral rules auto-evolving')"
    ),
}

TOOLS["get_active_goals"] = {
    "fn":   t_get_active_goals,
    "perm": "READ",
    "args": [
        {"name": "domain", "type": "string", "description": "Optional domain filter"},
    ],
    "desc": (
        "P2-03: Return all active cross-session goals with progress history. "
        "Auto-called by session_start (active_goals field). Call manually mid-session to check status. "
        "EXAMPLE: get_active_goals() or get_active_goals(domain='core_agi')"
    ),
}

# -- P3-01 / P3-04: Semantic KB + Episode memory tools -----------------------
try:
    from core_embeddings import (
        t_embed_kb_entry, t_semantic_kb_search,
        t_backfill_kb_embeddings, t_semantic_episode_search,
    )
    TOOLS["embed_kb_entry"] = {
        "fn":   t_embed_kb_entry,
        "perm": "WRITE",
        "args": [{"name": "kb_id", "type": "string", "description": "knowledge_base row id to embed"}],
        "desc": (
            "P3-01: Embed a single KB entry using Gemini text-embedding-004. "
            "Stores 768-dim vector in knowledge_base.embedding column. "
            "REQUIRES: ALTER TABLE knowledge_base ADD COLUMN embedding vector(768). "
            "Use backfill_kb_embeddings for bulk operation."
        ),
    }
    TOOLS["semantic_kb_search"] = {
        "fn":   t_semantic_kb_search,
        "perm": "READ",
        "args": [
            {"name": "query",     "type": "string"},
            {"name": "domain",    "type": "string"},
            {"name": "limit",     "type": "string"},
            {"name": "threshold", "type": "string", "description": "Cosine similarity threshold 0.0-1.0 (default 0.70)"},
        ],
        "desc": (
            "P3-01: Semantic KB search via pgvector cosine similarity. "
            "Finds conceptually related entries even when keywords don't match. "
            "Falls back to ilike if embedding column not yet available. "
            "EXAMPLE: semantic_kb_search(query='Railway build hanging on startup', threshold='0.65')"
        ),
    }
    TOOLS["backfill_kb_embeddings"] = {
        "fn":   t_backfill_kb_embeddings,
        "perm": "WRITE",
        "args": [
            {"name": "batch_size", "type": "string", "description": "Entries per batch (default 20, max 50)"},
            {"name": "domain",     "type": "string", "description": "Optional domain filter"},
        ],
        "desc": (
            "P3-01: Batch embed all knowledge_base entries missing embeddings. "
            "Run once after adding vector column, then periodically for new entries. "
            "Safe to re-run — skips already-embedded entries. "
            "Call multiple times until has_more=false."
        ),
    }
    TOOLS["semantic_episode_search"] = {
        "fn":   t_semantic_episode_search,
        "perm": "READ",
        "args": [
            {"name": "chat_id", "type": "string"},
            {"name": "query",   "type": "string"},
            {"name": "limit",   "type": "string"},
        ],
        "desc": (
            "P3-04: Search past conversation episodes by semantic similarity. "
            "Returns summaries of relevant past conversations. "
            "REQUIRES: conversation_episodes table + embedding column. "
            "EXAMPLE: semantic_episode_search(chat_id='838737537', query='LSEI RMU commissioning')"
        ),
    }
    print("[CORE] P3-01/P3-04 embedding tools registered")
except ImportError as _emb_e:
    print(f"[CORE] core_embeddings not found — P3-01/P3-04 tools skipped: {_emb_e}")

# -- core_web tools registration ----------------------------------------------
from core_web import _register_web_tools
_register_web_tools(TOOLS)


# =============================================================================
# P3-02: OWNER PROFILE TOOLS
# Structured model of Vux's working style, preferences, and patterns.
# Populated by cold processor. Injected into session_start system prompt.
# =============================================================================

def t_get_owner_profile(dimension: str = "", limit: str = "10") -> dict:
    """P3-02: Load active owner profile entries, optionally filtered by dimension.
    dimension: communication_style | decision_pattern | recurring_concern |
               working_habit | preference | trigger | frustration | (empty = all)
    Returns entries sorted by confidence DESC, times_observed DESC.
    Injected into session_start as OWNER PROFILE section.
    """
    try:
        lim = min(int(limit) if limit else 10, 50)
        if dimension and dimension.strip():
            qs = (
                f"select=id,dimension,value,confidence,domain,times_observed,last_seen"
                f"&active=eq.true&dimension=eq.{dimension.strip()}"
                f"&order=confidence.desc,times_observed.desc&limit={lim}"
            )
        else:
            qs = (
                f"select=id,dimension,value,confidence,domain,times_observed,last_seen"
                f"&active=eq.true"
                f"&order=confidence.desc,times_observed.desc&limit={lim}"
            )
        rows = sb_get("owner_profile", qs, svc=True) or []
        return {
            "ok":       True,
            "count":    len(rows),
            "dimension": dimension or "all",
            "profile":  rows,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_add_owner_observation(
    dimension: str  = "",
    value: str      = "",
    confidence: str = "0.7",
    evidence: str   = "",
    domain: str     = "universal",
    source: str     = "session_observation",
) -> dict:
    """P3-02: Add or reinforce an owner profile observation.
    dimension: communication_style | decision_pattern | recurring_concern |
               working_habit | preference | trigger | frustration
    value: the observation text (min 10 chars).
    If entry with same dimension+value already exists: increments times_observed,
    updates confidence if higher, updates last_seen.
    Use for real-time session observations and cold processor synthesis.
    EXAMPLE: add_owner_observation(dimension='working_habit',
               value='Prefers concise bullet-point summaries over prose', confidence='0.8')
    """
    VALID_DIMS = {
        "communication_style", "decision_pattern", "recurring_concern",
        "working_habit", "preference", "trigger", "frustration",
    }
    if not dimension or not value:
        return {"ok": False, "error": "dimension and value are required"}
    if dimension not in VALID_DIMS:
        return {"ok": False, "error": f"dimension must be one of: {sorted(VALID_DIMS)}"}
    try:
        conf = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        conf = 0.7

    try:
        val_slug = value.strip()[:50].replace("'", "").replace(" ", "%20")
        existing = sb_get(
            "owner_profile",
            f"select=id,confidence,times_observed"
            f"&dimension=eq.{dimension}&value=ilike.*{val_slug[:30]}*&limit=1",
            svc=True,
        ) or []

        if existing:
            row = existing[0]
            new_conf  = max(float(row.get("confidence") or 0), conf)
            new_count = int(row.get("times_observed") or 1) + 1
            ok = sb_patch("owner_profile", f"id=eq.{row['id']}", {
                "confidence":     round(new_conf, 3),
                "times_observed": new_count,
                "last_seen":      datetime.utcnow().isoformat(),
                "updated_at":     datetime.utcnow().isoformat(),
            })
            return {
                "ok":            ok,
                "action":        "reinforced",
                "id":            row["id"],
                "times_observed": new_count,
                "confidence":    round(new_conf, 3),
            }
        else:
            ok = sb_post("owner_profile", {
                "dimension":      dimension,
                "value":          value.strip()[:1000],
                "confidence":     round(conf, 3),
                "evidence":       (evidence or "")[:500],
                "domain":         domain.strip() or "universal",
                "source":         source.strip() or "session_observation",
                "active":         True,
                "times_observed": 1,
                "last_seen":      datetime.utcnow().isoformat(),
                "created_at":     datetime.utcnow().isoformat(),
                "updated_at":     datetime.utcnow().isoformat(),
            })
            return {"ok": ok, "action": "inserted", "dimension": dimension, "value": value[:80]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# =============================================================================
# P3-07: CAPABILITY MODEL TOOLS
# CORE's calibrated self-assessment of its own reliability per domain.
# Updated weekly by _run_capability_calibration() in core_train.py.
# =============================================================================

def t_get_capability_model(domain: str = "") -> dict:
    """P3-07: Load CORE's self-calibrated capability model.
    domain: optional filter (e.g. 'code', 'deploy', 'training').
    Empty = all domains, sorted by reliability ASC (weakest first).
    Injected into session_start. Used by orchestrator to flag weak domains.
    Returns weak_domains list (reliability < 0.60) for system prompt injection.
    EXAMPLE: get_capability_model() or get_capability_model(domain='deploy')
    """
    try:
        if domain and domain.strip():
            qs = (
                f"select=domain,capability,reliability,tool_count,avg_fail_rate,"
                f"strong_tools,weak_tools,last_calibrated,notes"
                f"&domain=eq.{domain.strip()}&order=reliability.asc&limit=50"
            )
        else:
            qs = (
                f"select=domain,capability,reliability,tool_count,avg_fail_rate,"
                f"strong_tools,weak_tools,last_calibrated,notes"
                f"&order=reliability.asc&limit=50"
            )
        rows = sb_get("capability_model", qs, svc=True) or []
        weak_domains = [r["domain"] for r in rows if float(r.get("reliability") or 1.0) < 0.60]
        return {
            "ok":           True,
            "count":        len(rows),
            "domains":      rows,
            "weak_domains": weak_domains,
            "calibration_note": (
                f"{len(weak_domains)} domain(s) below 0.60: "
                + ", ".join(weak_domains)
                if weak_domains else "All domains above 0.60 reliability."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_trigger_capability_calibration() -> dict:
    """P3-07: Manually trigger the weekly capability calibration.
    Reads tool_stats for last 30 days, computes per-domain reliability,
    updates capability_model table. Notifies owner via Telegram.
    Returns: {ok, updated (domain count), weak_domains}
    """
    try:
        from core_train import _run_capability_calibration
        return _run_capability_calibration()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_core_gap_audit(force: str = "false", notify_owner: str = "true") -> dict:
    """Run the CORE-wide manual work audit.

    Returns a structured packet describing gaps CORE cannot self-resolve.
    If notify_owner is true, also sends the report to Telegram.
    """
    try:
        from core_gap_audit import build_core_gap_audit, notify_core_gap_audit
        want_force = str(force).strip().lower() in {"1", "true", "yes", "on"}
        want_notify = str(notify_owner).strip().lower() in {"1", "true", "yes", "on"}
        if want_notify:
            return notify_core_gap_audit(force=want_force)
        return build_core_gap_audit(force=want_force)
    except Exception as e:
        return {"ok": False, "error": str(e)}


# =============================================================================
# TOOLS REGISTRATION — P3-02 + P3-07
# Append these registrations to the TOOLS dict in core_tools.py.
# =============================================================================

TOOLS["get_owner_profile"] = {
    "fn":   t_get_owner_profile,
    "perm": "READ",
    "args": [
        {"name": "dimension", "type": "string",
         "description": "communication_style | decision_pattern | recurring_concern | working_habit | preference | trigger | frustration | (empty=all)"},
        {"name": "limit",     "type": "string", "description": "Max entries to return (default 10, max 50)"},
    ],
    "desc": (
        "P3-02: Load active owner profile entries. Returns structured observations about "
        "Vux's working style, preferences, and behavioral patterns. Sorted by confidence. "
        "Auto-injected into session_start. Use directly when you need specific behavioral "
        "context. EXAMPLE: get_owner_profile(dimension='preference')"
    ),
}

TOOLS["add_owner_observation"] = {
    "fn":   t_add_owner_observation,
    "perm": "WRITE",
    "args": [
        {"name": "dimension",   "type": "string", "description": "communication_style | decision_pattern | recurring_concern | working_habit | preference | trigger | frustration"},
        {"name": "value",       "type": "string", "description": "The observed behavioral pattern"},
        {"name": "confidence",  "type": "string", "description": "Confidence 0.0-1.0 (default 0.7)"},
        {"name": "evidence",    "type": "string", "description": "What session/event led to this observation"},
        {"name": "domain",      "type": "string", "description": "Domain this applies to (default: universal)"},
        {"name": "source",      "type": "string", "description": "session_observation | owner_stated | cold_processor"},
    ],
    "desc": (
        "P3-02: Record a new behavioral observation about Vux. If same observation exists, "
        "reinforces it (increments times_observed, updates confidence). Auto-deduplicates. "
        "Call when you notice a pattern in how Vux works or communicates. "
        "EXAMPLE: add_owner_observation(dimension='preference', "
        "value='Prefers deploy then test sequence not test-in-advance', confidence='0.8')"
    ),
}

TOOLS["get_capability_model"] = {
    "fn":   t_get_capability_model,
    "perm": "READ",
    "args": [
        {"name": "domain", "type": "string",
         "description": "Filter by domain: deploy | code | training | knowledge | task | telegram | system | project | crypto | web | document | railway | agentic"},
    ],
    "desc": (
        "P3-07: Load CORE's self-calibrated capability model. Returns reliability scores "
        "per domain derived from tool_stats (0.0=always fails, 1.0=never fails). "
        "Returns weak_domains list (reliability < 0.60). "
        "Auto-injected into session_start. Use before starting work in a domain to "
        "understand current reliability. Calibrated weekly by _run_capability_calibration(). "
        "EXAMPLE: get_capability_model(domain='deploy') to check deploy reliability before patching."
    ),
}

TOOLS["trigger_capability_calibration"] = {
    "fn":   t_trigger_capability_calibration,
    "perm": "WRITE",
    "args": [],
    "desc": (
        "P3-07: Manually trigger weekly capability calibration. "
        "Reads tool_stats (last 30 days), computes per-domain reliability, "
        "updates capability_model table, notifies owner of weak domains. "
        "Normally runs automatically every Thursday. Call manually after a major "
        "tool fix session to get updated reliability scores."
    ),
}

TOOLS["core_gap_audit"] = {
    "fn":   t_core_gap_audit,
    "perm": "WRITE",
    "args": [
        {"name": "force", "type": "string", "description": "true to notify even if the signature is unchanged"},
        {"name": "notify_owner", "type": "string", "description": "true to send the report to Telegram (default true)"},
    ],
    "desc": (
        "CORE-wide manual work audit. Scans tool taxonomy, repo-map health, capability model, "
        "quality alerts, state continuity, and backlog pressure. Returns a gap packet showing what "
        "CORE cannot self-resolve yet. Use when you need the system to tell the owner what human "
        "work is required next. EXAMPLE: core_gap_audit()"
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
# VM TOOLS — Direct execution on Oracle VM
# No routing, no queuing — CORE runs ON the VM so these execute immediately.
# ══════════════════════════════════════════════════════════════════════════════

import subprocess as _subprocess
from pathlib import Path as _Path

_VM_WORK_DIR = "/home/ubuntu/core-agi"


def t_shell(command: str = "", timeout: str = "60", sudo: str = "false") -> dict:
    """Run any bash command on the VM. Full shell access."""
    if not command:
        return {"ok": False, "error": "command required"}
    try:
        use_sudo = sudo.lower() == "true"
        cmd = f"sudo {command}" if use_sudo and not command.strip().startswith("sudo") else command
        result = _subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=int(timeout), cwd=_VM_WORK_DIR
        )
        output = (result.stdout + result.stderr).strip()[:4000]
        return {"ok": result.returncode == 0, "output": output, "returncode": result.returncode}
    except _subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_run_script(script: str = "", lang: str = "bash", timeout: str = "60") -> dict:
    """Write a bash or python script to a temp file and execute it on the VM."""
    if not script:
        return {"ok": False, "error": "script required"}
    try:
        import time as _time
        ext = ".sh" if lang == "bash" else ".py"
        tmp = _Path(f"/tmp/core_script_{int(_time.time())}{ext}")
        tmp.write_text(script)
        tmp.chmod(0o755)
        import sys as _sys
        cmd = ["bash", str(tmp)] if lang == "bash" else [_sys.executable, str(tmp)]
        result = _subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=int(timeout), cwd=_VM_WORK_DIR
        )
        tmp.unlink(missing_ok=True)
        output = (result.stdout + result.stderr).strip()[:4000]
        return {"ok": result.returncode == 0, "output": output}
    except _subprocess.TimeoutExpired:
        return {"ok": False, "error": f"script timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_file_read(path: str = "", lines: str = "0") -> dict:
    """Read a file from the VM filesystem."""
    if not path:
        return {"ok": False, "error": "path required"}
    try:
        p = _Path(path)
        if not p.exists():
            return {"ok": False, "error": f"not found: {path}"}
        content = p.read_text(errors="replace")
        n = int(lines)
        if n > 0:
            content = "\n".join(content.splitlines()[:n])
        return {"ok": True, "path": path, "content": content[:10000], "size": p.stat().st_size}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_file_write(path: str = "", content: str = "", mode: str = "write") -> dict:
    """Write or append content to a file on the VM."""
    if not path:
        return {"ok": False, "error": "path required"}
    try:
        p = _Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with open(p, "a") as f:
                f.write(content)
        else:
            p.write_text(content)
        return {"ok": True, "path": path, "size": p.stat().st_size, "mode": mode}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_file_list(path: str = "", pattern: str = "*") -> dict:
    """List files in a directory on the VM."""
    try:
        p = _Path(path or _VM_WORK_DIR)
        if not p.exists():
            return {"ok": False, "error": f"path not found: {path}"}
        import os as _os
        files = []
        for f in sorted(p.glob(pattern))[:200]:
            try:
                st = f.stat()
                files.append({
                    "name": f.name, "path": str(f),
                    "type": "file" if f.is_file() else "dir",
                    "size": st.st_size if f.is_file() else 0,
                })
            except Exception:
                pass
        return {"ok": True, "path": str(p), "count": len(files), "files": files}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_git(repo_path: str = "", operation: str = "status", message: str = "CORE auto-commit") -> dict:
    """Git operations on any repo on the VM: pull, push, status, log, commit, diff."""
    repo = repo_path or _VM_WORK_DIR
    ops = {
        "pull":   ["git", "pull"],
        "status": ["git", "status", "--short"],
        "log":    ["git", "log", "--oneline", "-10"],
        "push":   ["git", "push"],
        "diff":   ["git", "diff", "--stat"],
        "commit": ["git", "commit", "-am", message],
    }
    if operation not in ops:
        return {"ok": False, "error": f"unknown operation: {operation}. Use: {list(ops.keys())}"}
    try:
        result = _subprocess.run(
            ops[operation], capture_output=True, text=True, timeout=60, cwd=repo
        )
        return {"ok": result.returncode == 0, "operation": operation,
                "output": (result.stdout + result.stderr).strip()[:2000]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_service(service: str = "core-agi", operation: str = "status") -> dict:
    """Manage systemd services on the VM: start, stop, restart, status, reload."""
    allowed = ["start", "stop", "restart", "status", "reload", "enable", "disable"]
    if operation not in allowed:
        return {"ok": False, "error": f"operation must be one of {allowed}"}
    try:
        result = _subprocess.run(
            ["sudo", "systemctl", operation, service],
            capture_output=True, text=True, timeout=30
        )
        return {"ok": result.returncode == 0, "service": service, "operation": operation,
                "output": (result.stdout + result.stderr).strip()[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_vm_info() -> dict:
    """Get VM system info: disk, memory, CPU load, uptime, running services."""
    try:
        result = _subprocess.run(
            "echo '=DISK=' && df -h / && "
            "echo '=MEMORY=' && free -h && "
            "echo '=UPTIME=' && uptime && "
            "echo '=SERVICES=' && systemctl list-units --type=service --state=running --no-pager | head -15",
            shell=True, capture_output=True, text=True, timeout=15
        )
        return {"ok": True, "info": result.stdout.strip()[:3000]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_install_package(package: str = "", manager: str = "pip") -> dict:
    """Install a package via apt or pip on the VM."""
    if not package:
        return {"ok": False, "error": "package required"}
    try:
        import sys as _sys
        if manager == "apt":
            cmd = ["sudo", "apt", "install", "-y", package]
        else:
            cmd = [_sys.executable, "-m", "pip", "install", package, "--break-system-packages"]
        result = _subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = (result.stdout + result.stderr).strip()[-1000:]
        return {"ok": result.returncode == 0, "package": package, "output": output}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Register VM tools ─────────────────────────────────────────────────────────

TOOLS["shell"] = {
    "fn":   t_shell,
    "perm": "EXECUTE",
    "args": [
        {"name": "command", "type": "string", "description": "Bash command to run on VM"},
        {"name": "timeout", "type": "string", "description": "Timeout in seconds (default 60)"},
        {"name": "sudo",    "type": "string", "description": "Run with sudo: true|false (default false)"},
    ],
    "desc": (
        "Run any bash command directly on the Oracle VM. Full shell access. "
        "CORE runs on this VM so output is immediate — no queuing. "
        "EXAMPLE: shell(command='df -h') to check disk space. "
        "EXAMPLE: shell(command='cat /var/log/syslog | tail -50') to read logs. "
        "EXAMPLE: shell(command='apt list --installed', sudo='true') for installed packages."
    ),
}

TOOLS["run_script"] = {
    "fn":   t_run_script,
    "perm": "EXECUTE",
    "args": [
        {"name": "script",  "type": "string", "description": "Script content to execute"},
        {"name": "lang",    "type": "string", "description": "bash or python (default bash)"},
        {"name": "timeout", "type": "string", "description": "Timeout in seconds (default 60)"},
    ],
    "desc": (
        "Write and execute a bash or python script on the VM. "
        "Writes to /tmp, executes, returns output. "
        "EXAMPLE: run_script(script='#!/bin/bash\\necho hello\\nls -la', lang='bash'). "
        "EXAMPLE: run_script(script='import os\\nprint(os.listdir(\".\")), lang='python')"
    ),
}

TOOLS["file_read"] = {
    "fn":   t_file_read,
    "perm": "READ",
    "args": [
        {"name": "path",  "type": "string", "description": "Absolute path to file on VM"},
        {"name": "lines", "type": "string", "description": "Number of lines to read (0=full file)"},
    ],
    "desc": (
        "Read any file from the VM filesystem. "
        "EXAMPLE: file_read(path='/home/ubuntu/core-agi/core_config.py', lines='50') "
        "to read first 50 lines. file_read(path='/var/log/nginx/error.log') for full log."
    ),
}

TOOLS["file_write"] = {
    "fn":   t_file_write,
    "perm": "WRITE",
    "args": [
        {"name": "path",    "type": "string", "description": "Absolute path to write to"},
        {"name": "content", "type": "string", "description": "Content to write"},
        {"name": "mode",    "type": "string", "description": "write (overwrite) or append (default: write)"},
    ],
    "desc": (
        "Write or append content to any file on the VM. Creates parent dirs if needed. "
        "EXAMPLE: file_write(path='/home/ubuntu/core-agi/test.py', content='print(1)') "
        "EXAMPLE: file_write(path='/home/ubuntu/notes.txt', content='new line\\n', mode='append')"
    ),
}

TOOLS["file_list"] = {
    "fn":   t_file_list,
    "perm": "READ",
    "args": [
        {"name": "path",    "type": "string", "description": "Directory path (default: core-agi folder)"},
        {"name": "pattern", "type": "string", "description": "Glob pattern (default: *)"},
    ],
    "desc": (
        "List files in any directory on the VM. "
        "EXAMPLE: file_list(path='/home/ubuntu/core-agi', pattern='*.py') "
        "to list all Python files."
    ),
}

TOOLS["git"] = {
    "fn":   t_git,
    "perm": "EXECUTE",
    "args": [
        {"name": "repo_path",  "type": "string", "description": "Path to git repo (default: core-agi)"},
        {"name": "operation",  "type": "string", "description": "pull | push | status | log | commit | diff"},
        {"name": "message",    "type": "string", "description": "Commit message (for commit operation)"},
    ],
    "desc": (
        "Git operations on any repo on the VM. "
        "EXAMPLE: git(operation='pull') to pull latest from GitHub. "
        "EXAMPLE: git(operation='status') to see changed files. "
        "EXAMPLE: git(operation='commit', message='fix: update config') to commit all changes."
    ),
}

TOOLS["service"] = {
    "fn":   t_service,
    "perm": "EXECUTE",
    "args": [
        {"name": "service",   "type": "string", "description": "systemd service name (default: core-agi)"},
        {"name": "operation", "type": "string", "description": "start | stop | restart | status | reload"},
    ],
    "desc": (
        "Manage systemd services on the VM. "
        "EXAMPLE: service(service='core-agi', operation='restart') to restart CORE. "
        "EXAMPLE: service(service='nginx', operation='status') to check nginx. "
        "EXAMPLE: service(service='core-agi', operation='stop') to stop CORE."
    ),
}

TOOLS["vm_info"] = {
    "fn":   t_vm_info,
    "perm": "READ",
    "args": [],
    "desc": (
        "Get VM system status: disk usage, memory, CPU load, uptime, running services. "
        "Call this to check VM health or before major operations. "
        "EXAMPLE: vm_info()"
    ),
}

TOOLS["install_package"] = {
    "fn":   t_install_package,
    "perm": "EXECUTE",
    "args": [
        {"name": "package", "type": "string", "description": "Package name to install"},
        {"name": "manager", "type": "string", "description": "pip or apt (default: pip)"},
    ],
    "desc": (
        "Install a Python or system package on the VM. "
        "EXAMPLE: install_package(package='httpx') for pip. "
        "EXAMPLE: install_package(package='htop', manager='apt') for system package."
    ),
}
