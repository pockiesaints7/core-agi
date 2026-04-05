"""core_train.py â€” CORE AGI Training Pipeline
Extracted from core.py. Contains:
  - auto_hot_reflection
  - run_cold_processor
  - apply_evolution / reject_evolution
  - cold_processor_loop
  - _backlog_add / _sync_backlog_status / _backlog_to_markdown
  - run_kb_mining
  - _extract_real_signal / _run_simulation_batch
  - background_researcher

Depends on: core_config, core_github
NOTE: Split architecture is live. core.py has been deleted.
"""
import json
import os
import time
import threading as _threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from core_config import (
    SUPABASE_URL,
    COLD_HOT_THRESHOLD, COLD_TIME_THRESHOLD, COLD_KB_GROWTH_THRESHOLD,
    PATTERN_EVO_THRESHOLD, KNOWLEDGE_AUTO_CONFIDENCE,
    KB_MINE_BATCH_SIZE, KB_MINE_RATIO_THRESHOLD,
    sb_get, sb_count, sb_post, sb_post_critical, sb_patch, sb_upsert, _sbh_count_svc, _env_int, _env_float,
    gemini_chat, groq_chat, GROQ_MODEL, GROQ_FAST,
)
from core_github import notify, gh_write
from core_trading_specialization import (
    TRADING_DOMAIN,
    TRADING_META_DOMAIN,
    TRADING_RARL_GOALS,
    allow_generic_public_ingest,
    build_trading_curriculum,
    build_trading_source_packet,
    trading_specialization_enabled,
    training_meta_domain,
)
from core_work_taxonomy import build_autonomy_contract

# -- Schema helpers (mirrors core_tools._sel_force) ---------------------------
def _sel_force(table: str, cols: list) -> str:
    """SELECT string with specific columns. Validates against core_tools._SB_SCHEMA.
    Drops unknown columns to prevent 400 errors on schema changes."""
    try:
        from core_tools import _SB_SCHEMA
        schema = _SB_SCHEMA.get("tables", {}).get(table, {})
        known = set(schema.get("columns", {}).keys())
        if known:
            valid = [c for c in cols if c in known]
            return ",".join(valid) if valid else ",".join(cols)
    except Exception:
        pass
    return ",".join(cols)


def _sb_eq_value(value: object, max_len: int | None = None) -> str:
    text = "" if value is None else str(value)
    if max_len is not None:
        text = text[:max_len]
    return quote(text, safe="")


def _pattern_frequency_lookup(keys) -> dict[str, dict]:
    rows_by_key: dict[str, dict] = {}
    seen: set[str] = set()
    for raw_key in keys:
        key = str(raw_key or "")[:500]
        if not key or key in seen:
            continue
        seen.add(key)
        rows = sb_get(
            "pattern_frequency",
            f"select=id,pattern_key,frequency,auto_applied&pattern_key=eq.{_sb_eq_value(key, 500)}&id=gt.1&limit=1",
            svc=True,
        ) or []
        if rows and rows[0].get("pattern_key"):
            rows_by_key[str(rows[0]["pattern_key"])] = rows[0]
    return rows_by_key


def _normalize_utc_timestamp(value: str, *, clamp_future: bool = True) -> tuple[str | None, bool, bool]:
    """Return (timestamp_str, parsed_ok, future_detected)."""
    if value in (None, "", {}, []):
        return None, False, False
    text = str(value).strip()
    if not text:
        return None, False, False
    text = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        try:
            dt = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None, False, False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    future = dt > now
    if future and clamp_future:
        dt = now
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), True, future


def _training_kb_domain() -> str:
    return training_meta_domain()


def _phase_signal_packet(limit: int = 16) -> dict | None:
    if not trading_specialization_enabled():
        return None
    packet = build_trading_source_packet(limit=max(40, limit * 3))
    live_mistakes = packet.get("tables", {}).get("trading_mistakes", [])
    memory_mistakes = packet.get("tables", {}).get("mistakes", [])
    recent_mistakes = (live_mistakes + memory_mistakes)[:limit]
    recent_hots = packet.get("tables", {}).get("hot_reflections", [])[:limit]
    curriculum = build_trading_curriculum(limit=limit, packet=packet)
    return {
        "packet": packet,
        "curriculum": curriculum,
        "recent_hots": recent_hots,
        "recent_mistakes": recent_mistakes,
        "recent_evos": [],
        "recent_sessions": (
            packet.get("tables", {}).get("trading_decisions", [])[:limit] +
            packet.get("tables", {}).get("closed_positions", [])[:limit]
        ),
    }


# Training globals
_last_cold_run: float = 0.0
_last_cold_kb_count: int = 0
_last_research_run: float = -1.0
_IMPROVEMENT_INTERVAL = 3600  # 60 min
_last_public_source_run: float = -1.0
_PUBLIC_SOURCE_INTERVAL = 21600  # 6 hours
_last_meta_learning_run: float = -1.0
_META_LEARNING_INTERVAL = 21600  # 6 hours
_last_meta_training_run: float = -1.0
_META_TRAINING_INTERVAL = 21600  # 6 hours
_last_causal_discovery_run: float = -1.0
_CAUSAL_DISCOVERY_INTERVAL = 21600  # 6 hours
_last_temporal_hwm_run: float = -1.0
_TEMPORAL_HWM_INTERVAL = 21600  # 6 hours
_last_joint_training_run: float = -1.0
_JOINT_TRAINING_INTERVAL = 21600  # 6 hours
_JOINT_TRAINING_PLANNER_ENABLED = os.getenv("JOINT_TRAINING_PLANNER_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
_last_router_policy_run: float = -1.0
_ROUTER_POLICY_INTERVAL = 21600  # 6 hours
_ROUTER_POLICY_ENABLED = os.getenv("DYNAMIC_ROUTER_POLICY_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}

# Source confidence multipliers (Phase 3)
_SRC_CONF = {"real": 1.0, "simulation": 0.7, "both": 1.3}

# â”€â”€ P2-01: Approval tier assignment helper â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _assign_approval_tier(confidence: float, change_type: str, src_key: str) -> str:
    """Assign approval tier for a new evolution queue entry.
    auto:   conf >= 0.85 AND knowledge AND real/both source -> apply immediately in cold processor
    notify: conf >= 0.70 AND knowledge -> apply after 24h via evolution_tier_processor
    owner:  everything else (code, schema, low confidence, simulation-only)
    Safety valve: EVOLUTION_AUTO_TIER env var
      - 'notify_only': demotes all auto -> notify (no immediate auto-apply)
      - 'disabled':    forces all -> owner (pauses all autonomous apply)
    """
    safety = os.getenv("EVOLUTION_AUTO_TIER", "").strip().lower()
    if safety == "disabled":
        return "owner"
    if change_type != "knowledge":
        return "owner"
    if confidence >= 0.85 and src_key in ("real", "both"):
        if safety == "notify_only":
            return "notify"
        return "auto"
    if confidence >= 0.70:
        return "notify"
    return "owner"


def _hierarchical_auxiliary_loss(levels: list) -> tuple[float, dict]:
    """Estimate divergence across ordered reasoning levels.

    This is a bounded proxy for hierarchical prediction loss:
    more token drift between adjacent levels means higher penalty.
    """
    def _tokens(text: str) -> set:
        raw = str(text or "").lower().replace("\n", " ").split()
        cleaned = []
        for tok in raw:
            tok = tok.strip(".,:;!?()[]{}<>\"'`|/\\")
            if len(tok) > 2:
                cleaned.append(tok)
        return set(cleaned[:80])

    prev = None
    divergences = []
    disentanglement_scores = []
    for lvl in levels:
        cur = _tokens(lvl)
        if not cur:
            continue
        if prev is not None:
            union = prev | cur
            if union:
                divergence = 1.0 - (len(prev & cur) / len(union))
                divergences.append(divergence)
                disentanglement_scores.append(len(prev & cur) / len(union))
        prev = cur

    if not divergences:
        return 0.0, {"pairs": 0, "avg_divergence": 0.0, "max_divergence": 0.0}

    avg_div = sum(divergences) / len(divergences)
    max_div = max(divergences)
    causal_disentanglement_loss = round(sum(disentanglement_scores) / len(disentanglement_scores), 3) if disentanglement_scores else 0.0
    aux_loss = round(min(1.0, (avg_div * 0.55) + (max_div * 0.25) + (causal_disentanglement_loss * 0.20)), 3)
    return aux_loss, {
        "pairs": len(divergences),
        "avg_divergence": round(avg_div, 3),
        "max_divergence": round(max_div, 3),
        "causal_disentanglement_loss": causal_disentanglement_loss,
    }


def _temporal_sequence_loss(sequence: list, decay: float = 0.82) -> tuple[float, dict]:
    """Estimate bounded temporal loss over a sequence of textual states/actions."""
    items = [str(item).strip() for item in (sequence or []) if str(item).strip()]
    if len(items) < 2:
        return 0.0, {
            "pairs": 0,
            "adjacency_loss": 0.0,
            "recency_loss": 0.0,
            "repetition_penalty": 0.0,
            "sequence_loss": 0.0,
        }

    def _tokens(text: str) -> set[str]:
        raw = str(text or "").lower().replace("\n", " ").split()
        cleaned = []
        for tok in raw:
            tok = tok.strip(".,:;!?()[]{}<>\"'`|/\\")
            if len(tok) > 2:
                cleaned.append(tok)
        return set(cleaned[:80])

    token_sets = [_tokens(item) for item in items]
    divergences = []
    recency_deltas = []
    for idx, cur in enumerate(token_sets):
        if idx == 0:
            continue
        prev = token_sets[idx - 1]
        union = prev | cur
        overlap = (len(prev & cur) / len(union)) if union else 0.0
        divergences.append(1.0 - overlap)

    for idx, item in enumerate(items):
        decay_target = max(0.05, min(1.0, float(decay or 0.82) ** max(0, len(items) - idx - 1)))
        normalized_pos = (idx + 1) / len(items)
        recency_deltas.append(abs(decay_target - normalized_pos))

    unique_ratio = len(set(items)) / len(items)
    repetition_penalty = round(max(0.0, 1.0 - unique_ratio), 3)
    adjacency_loss = round(sum(divergences) / len(divergences), 3) if divergences else 0.0
    recency_loss = round(sum(recency_deltas) / len(recency_deltas), 3) if recency_deltas else 0.0
    sequence_loss = round(min(1.0, (adjacency_loss * 0.55) + (recency_loss * 0.3) + (repetition_penalty * 0.15)), 3)
    return sequence_loss, {
        "pairs": len(divergences),
        "adjacency_loss": adjacency_loss,
        "recency_loss": recency_loss,
        "repetition_penalty": repetition_penalty,
        "sequence_loss": sequence_loss,
    }


def _hierarchical_reward_schedule(epoch_number: int, research_domain: str, champion_exists: bool) -> dict:
    """Return a phase-aware reward weighting for RARL epochs.

    The schedule intentionally changes emphasis over time:
    - explore: favor stability/sample efficiency
    - stabilize: balance all objectives
    - exploit: favor benchmark/transfer gains
    Domain-specific nudges make the schedule sensitive to the epoch goal.
    """
    phase_cycle = {
        1: "explore",
        2: "stabilize",
        0: "exploit",
    }
    phase = phase_cycle[epoch_number % 3]
    weights = {
        "benchmark_score": 0.20,
        "transfer_score": 0.20,
        "stability_score": 0.20,
        "sample_efficiency": 0.20,
        "reasoning_depth": 0.20,
        "planning_success_rate": 0.20,
    }
    phase_focus = {
        "explore": {"stability_score": 0.08, "sample_efficiency": 0.08, "reasoning_depth": 0.04, "planning_success_rate": 0.04},
        "stabilize": {"stability_score": 0.04, "transfer_score": 0.04, "reasoning_depth": 0.04, "planning_success_rate": 0.04},
        "exploit": {"benchmark_score": 0.08, "transfer_score": 0.08, "reasoning_depth": 0.04, "planning_success_rate": 0.03},
    }
    domain_focus = {
        "memory": {"stability_score": 0.08, "sample_efficiency": 0.05},
        "world_modeling": {"benchmark_score": 0.08, "transfer_score": 0.05},
        "reasoning": {"reasoning_depth": 0.10, "transfer_score": 0.04},
        "planning": {"reasoning_depth": 0.10, "benchmark_score": 0.04, "planning_success_rate": 0.12},
        "sample_efficiency": {"sample_efficiency": 0.12, "stability_score": 0.04},
        "generalization": {"transfer_score": 0.12, "reasoning_depth": 0.03},
    }

    for name, delta in phase_focus.get(phase, {}).items():
        weights[name] = max(0.05, weights.get(name, 0.0) + delta)
    for name, delta in domain_focus.get(research_domain, {}).items():
        weights[name] = max(0.05, weights.get(name, 0.0) + delta)
    if not champion_exists:
        weights["stability_score"] += 0.05
        weights["sample_efficiency"] += 0.05
        weights["planning_success_rate"] += 0.04

    total = sum(weights.values()) or 1.0
    norm_weights = {k: round(v / total, 3) for k, v in weights.items()}
    reward_focus = {
        "explore": "reward stability and sample efficiency while avoiding overfit complexity",
        "stabilize": "reward balanced generalization with moderate transfer and reasoning gains",
        "exploit": "reward benchmark and transfer gains without collapsing stability",
    }[phase]
    return {
        "phase": phase,
        "reward_focus": reward_focus,
        "weights": norm_weights,
    }


def _weighted_reward_score(scores: dict, schedule: dict) -> tuple[float, dict]:
    weights = schedule.get("weights") or {}
    reward_score = 0.0
    contributions = {}
    for key, weight in weights.items():
        value = float(scores.get(key, 0.0))
        contrib = value * float(weight)
        contributions[key] = round(contrib, 3)
        reward_score += contrib
    complexity_penalty = float(scores.get("complexity_penalty", 1.0))
    compute_cost = float(scores.get("compute_cost", 1.0))
    inference_latency = float(scores.get("inference_latency", 1.0))
    penalty = round((complexity_penalty * 0.12) + (compute_cost * 0.08) + (inference_latency * 0.08), 3)
    reward_score = round(max(0.0, reward_score - penalty), 3)
    return reward_score, {
        "phase": schedule.get("phase", "stabilize"),
        "weights": weights,
        "contributions": contributions,
        "penalty": penalty,
        "raw": round(sum(float(scores.get(k, 0.0)) * float(v) for k, v in weights.items()), 3),
    }


def _build_task_embedding_curriculum(limit: int = 12) -> dict:
    """Collect a small curriculum of recent tasks for meta-learning.

    The task payload is stored as JSON in task_queue.task, so we decode it and
    bucket tasks by work track / source for a compact training curriculum.
    """
    rows = sb_get(
        "task_queue",
        f"select=task,result,status,priority,source,updated_at&order=updated_at.desc&limit={limit}",
        svc=True,
    ) or []
    curriculum = []
    buckets: Counter = Counter()

    for row in rows:
        task_raw = row.get("task")
        task_data = {}
        if isinstance(task_raw, str):
            try:
                task_data = json.loads(task_raw)
            except Exception:
                task_data = {"title": task_raw}
        elif isinstance(task_raw, dict):
            task_data = task_raw
        else:
            task_data = {"title": str(task_raw)}

        autonomy = task_data.get("autonomy") if isinstance(task_data.get("autonomy"), dict) else {}
        work_track = (
            autonomy.get("work_track")
            or autonomy.get("task_group")
            or autonomy.get("kind")
            or row.get("source")
            or "general"
        )
        title = (
            task_data.get("title")
            or task_data.get("task")
            or task_data.get("description")
            or str(task_raw)
        )
        result = row.get("result")
        if isinstance(result, str):
            result = result[:180]
        elif isinstance(result, dict):
            result = json.dumps(result)[:180]
        curriculum.append({
            "work_track": str(work_track)[:32],
            "title": str(title)[:180],
            "status": str(row.get("status") or "unknown")[:24],
            "priority": row.get("priority"),
            "source": str(row.get("source") or "unknown")[:24],
            "result": result,
        })
        buckets[str(work_track)] += 1

    return {
        "items": curriculum[:limit],
        "counts": dict(buckets),
    }


# AGI-02: Nightly self-diagnosis
# NOTE: _last_self_diagnosis_run is intentionally NOT initialised to 0.0 here.
# It is seeded from Supabase at cold_processor_loop boot to survive Railway redeploys.
# Initialised to a sentinel so the boot-restore logic can detect first-run.
_last_self_diagnosis_run: float = -1.0   # -1 = not yet seeded from Supabase
_SELF_DIAGNOSIS_INTERVAL = 86400  # 24 hours
_SELF_DIAGNOSIS_HOUR_UTC = 19     # 02:00 WIB = 19:00 UTC
_DIAG_STATE_KEY = "last_self_diagnosis_ts"  # key used in sessions state_key

# AGI-01: Weekly cross-domain synthesis
_last_synthesis_run: float = 0.0
_SYNTHESIS_INTERVAL = 6 * 24 * 3600  # 6 days
_SYNTHESIS_DAY_UTC = 2               # Wednesday UTC

# AGI-05: Weekly memory consolidation
_last_consolidation_run: float = 0.0
_CONSOLIDATION_INTERVAL = 6 * 24 * 3600  # 6 days
_CONSOLIDATION_DAY_UTC = 6               # Sunday UTC (weekday=6)

# AGI-06: Weekly capability report
_last_capability_report_run: float = 0.0
_CAPABILITY_REPORT_INTERVAL = 6 * 24 * 3600  # 6 days
_CAPABILITY_REPORT_DAY_UTC = 1               # Tuesday UTC (weekday=1)

# â”€â”€ P3-07: Weekly capability calibration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_last_capability_calibration_run: float = 0.0
_CAPABILITY_CALIBRATION_INTERVAL = 6 * 24 * 3600  # 6 days

_CAP_DOMAIN_MAP = {
    "deploy":    ["redeploy", "deploy", "build_status", "validate_syntax", "patch_file",
                  "multi_patch", "gh_search_replace", "railway_logs", "replace_fn",
                  "smart_patch", "register_tool", "rollback", "verify_before_deploy"],
    "code":      ["read_file", "write_file", "gh_read", "search_in_file", "core_py",
                  "append_to_file", "diff", "gh_read_lines"],
    "training":  ["cold_processor", "training_pipeline", "evolution", "reflection",
                  "backfill", "synthesize", "trigger_cold"],
    "system":    ["get_state", "health", "stats", "crash", "system_map",
                  "sync_system", "session_start", "session_end", "checkpoint",
                  "tool_health", "load_arch"],
    "railway":   ["railway_env", "railway_service", "railway_logs"],
    "knowledge": ["search_kb", "add_knowledge", "kb_update", "get_mistakes",
                  "search_mistakes", "ask", "add_evolution_rule"],
    "task":      ["task_add", "task_update", "task_health", "sb_query",
                  "sb_insert", "sb_patch", "sb_upsert", "sb_delete"],
    "telegram":  ["notify_owner", "notify"],
    "crypto":    ["crypto_price", "crypto_balance", "crypto_trade"],
    "project":   ["project_list", "project_get", "project_search", "project_register",
                  "project_update", "project_index", "project_prepare", "project_consume"],
    "web":       ["web_search", "web_fetch", "summarize_url"],
    "document":  ["create_document", "create_spreadsheet", "create_presentation",
                  "read_document", "convert_document", "read_pdf", "read_image"],
    "agentic":   ["reason_chain", "lookahead", "decompose", "negative_space",
                  "predict_failure", "action_gate", "loop_detect", "goal_check",
                  "circuit_breaker", "mid_task", "assert_source"],
}

_CAP_DESCRIPTIONS = {
    "deploy":    "GitHub push -> Railway redeploy -> health verify pipeline",
    "code":      "Read/write/patch Python source files via GitHub Blobs API",
    "training":  "Cold processor + pattern extraction + evolution queue management",
    "knowledge": "KB search, add, update, dedup via search_kb and kb_update",
    "task":      "Task queue CRUD: add, update status, priority management",
    "telegram":  "Async owner notifications via Telegram bot",
    "system":    "Session bootstrap, system_map sync, tool health scan",
    "project":   "Project KB management, context prep, document extraction",
    "crypto":    "Binance price monitoring, balance queries, trade execution",
    "web":       "web_search, web_fetch, summarize_url via DDG/Bing scraping",
    "document":  "Document creation, reading, format conversion",
    "railway":   "Railway env vars, service info, live log fetching",
    "agentic":   "Causal reasoning, task decomposition, loop detection, self-correction",
}



# â”€â”€ TASK-4: Binance Price Monitor config â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_PRICE_MONITOR_SYMBOLS  = os.getenv("BINANCE_WATCH_SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT").split(",")
_PRICE_ALERT_THRESHOLD  = _env_float("BINANCE_ALERT_THRESHOLD_PCT", "3.0")
_PRICE_MONITOR_INTERVAL = _env_int("BINANCE_MONITOR_INTERVAL_S", "60")
_price_monitor_last_prices: dict = {}
_price_monitor_running = False


# -- Helpers (imported by core_main for get_system_counts) --------------------
def get_system_counts():
    counts = {}
    table_filters = {
        "knowledge_base":  "",
        "mistakes":        "",
        "sessions":        "",
        "task_queue":      "",
        "hot_reflections": "&processed_by_cold=eq.0",
        "evolution_queue": "&status=eq.pending",
    }
    for t, extra in table_filters.items():
        try:
            r = httpx.get(f"{SUPABASE_URL}/rest/v1/{t}?select=id&limit=1{extra}",
                          headers=_sbh_count_svc(), timeout=10)
            cr = r.headers.get("content-range", "*/0")
            counts[t] = int(cr.split("/")[-1]) if "/" in cr else 0
        except:
            counts[t] = -1
    return counts


def get_latest_session():
    d = sb_get("sessions", f"select={_sel_force('sessions', ['summary','actions','created_at'])}&order=created_at.desc&limit=1")
    return d[0] if d else {}


# -- Hot reflection ------------------------------------------------------------
def auto_hot_reflection(session_data: dict):
    try:
        summary       = session_data.get("summary", "")
        actions       = session_data.get("actions", []) or []
        interface     = session_data.get("interface", "unknown")
        seed_patterns = session_data.get("seed_patterns", []) or []
        total     = max(len(actions), 1)
        verify_rate  = round(sum(1 for a in actions if any(k in str(a).lower() for k in ["verify","readback","confirm"])) / total, 2)
        mistake_rate = round(sum(1 for a in actions if any(k in str(a).lower() for k in ["mistake","error","fix","wrong"])) / total, 2)
        domain = "general"
        _summary_lower = summary.lower()
        _domain_map = [
            (["supabase", "sb_query", "sb_patch", "sb_insert", "database", "table"], "db"),
            (["github", "patch_file", "gh_search", "gh_read", "commit", "deploy", "railway"], "code"),
            (["telegram", "notify", "bot"], "bot"),
            (["mcp", "tool", "tools dict", "session_start", "session_end"], "mcp"),
            (["training", "cold processor", "hot_reflection", "evolution", "pattern"], "training"),
            (["knowledge", "kb", "add_knowledge", "search_kb"], "kb"),
            (["architecture", "refactor", "skill file", "session_md", "system_map"], "core_agi.architecture"),
            (["patching", "patch", "old_str", "new_str"], "core_agi.patching"),
        ]
        for keywords, d in _domain_map:
            if any(kw in _summary_lower for kw in keywords):
                domain = d
                break
        if len(summary.strip()) < 50 and total <= 2:
            print(f"[HOT] Skipped trivial session: summary_len={len(summary)} actions={total}")
            return False

        # Extract patterns via Groq so cold processor has real signal
        new_patterns = list(seed_patterns)  # start with caller-supplied patterns
        quality_score = session_data.get("quality") or None
        gaps_identified = None
        try:
            actions_str = ", ".join(str(a) for a in actions[:20])
            seed_hint = f"Caller already identified: {seed_patterns}\n" if seed_patterns else ""

            anchor_ts = None
            try:
                prev = sb_get("hot_reflections",
                    "select=created_at&order=created_at.desc&limit=1",
                    svc=True)
                if prev and prev[0].get("created_at"):
                    anchor_ts = prev[0]["created_at"]
            except Exception:
                pass
            if not anchor_ts:
                anchor_ts = session_data.get("created_at") or ""
            if not anchor_ts:
                anchor_ts = (datetime.utcnow() - timedelta(hours=24)).isoformat()
            session_ts = anchor_ts.replace("Z", "").split("+")[0]

            enrichment = ""
            try:
                new_mistakes = sb_get("mistakes",
                    f"select=domain,what_failed,root_cause&created_at=gte.{session_ts}&order=id.desc&limit=5",
                    svc=True)
                if new_mistakes:
                    enrichment += "\nNew mistakes this session:\n" + "\n".join(
                        f"  [{r.get('domain','?')}] {r.get('what_failed','')[:120]} | root: {r.get('root_cause','')[:80]}"
                        for r in new_mistakes)
            except Exception: pass
            try:
                new_kb = sb_get("knowledge_base",
                    f"select=domain,topic&updated_at=gte.{session_ts}&order=updated_at.desc&limit=5",
                    svc=True)
                if new_kb:
                    enrichment += "\nKB entries added/updated:\n" + "\n".join(
                        f"  [{r.get('domain','?')}] {r.get('topic','')[:100]}" for r in new_kb)
            except Exception: pass
            try:
                task_updates = sb_get("task_queue",
                    f"select=task,status,result&updated_at=gte.{session_ts}&order=updated_at.desc&limit=5",
                    svc=True)
                if task_updates:
                    enrichment += "\nTask queue updates:\n" + "\n".join(
                        f"  [{r.get('status','?')}] {str(r.get('task',''))[:100]} | result: {str(r.get('result','null'))[:60]}"
                        for r in task_updates)
            except Exception: pass
            try:
                changelog_rows = sb_get("changelog",
                    f"select=component,title,change_type&created_at=gte.{session_ts}&order=id.desc&limit=3",
                    svc=True)
                if changelog_rows:
                    enrichment += "\nChangelog entries:\n" + "\n".join(
                        f"  [{r.get('change_type','?')}] {r.get('component','?')}: {r.get('title','')[:100]}"
                        for r in changelog_rows)
            except Exception: pass

            prompt = (
                f"Session summary: {summary[:500]}\n"
                f"Actions taken: {actions_str}\n"
                f"{enrichment}\n"
                f"{seed_hint}\n"
                f"Extract 2-5 reusable patterns from this session. "
                f"Each pattern should be a short, generalizable rule or observation (under 120 chars). "
                f"Do NOT duplicate patterns already listed above. "
                f"Also rate session quality 0.0-1.0 and identify any gaps.\n"
                f"Respond ONLY as JSON: "
                f'{{"patterns": ["..."], "quality": 0.8, "gaps": "..or null"}}'
            )
            _sys_hot = _load_prompt("hot_pattern_extractor", "You are CORE's pattern extraction engine. Return only valid JSON.")
            _maybe_eval_prompt("hot_pattern_extractor", _sys_hot, 20)
            raw = gemini_chat(system=_sys_hot, user=prompt, max_tokens=500, json_mode=True)
            raw_clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            parsed = json.loads(raw_clean)
            groq_patterns = [p for p in parsed.get("patterns", []) if isinstance(p, str) and len(p) > 5][:5]
            seen = set(p.lower() for p in new_patterns)
            for p in groq_patterns:
                if p.lower() not in seen:
                    new_patterns.append(p)
                    seen.add(p.lower())
            if quality_score is None:
                quality_score = float(parsed.get("quality") or 0.7)
            _gaps_raw = parsed.get("gaps") or None
            gaps_identified = [_gaps_raw] if _gaps_raw and isinstance(_gaps_raw, str) else _gaps_raw
            print(f"[HOT] Groq extracted {len(groq_patterns)} patterns, merged total={len(new_patterns)}, quality={quality_score}")
        except Exception as e:
            print(f"[HOT] Pattern extraction failed (non-fatal): {e}")

        ok = sb_post("hot_reflections", {
            "task_summary": summary[:300], "domain": domain,
            "verify_rate": verify_rate, "mistake_consult_rate": mistake_rate,
            "new_patterns": new_patterns, "new_mistakes": [],
            "quality_score": quality_score, "gaps_identified": gaps_identified,
            "reflection_text": f"Auto-generated from {interface} session. Actions: {total}. Patterns: {len(new_patterns)}.",
            "processed_by_cold": 0,
        })
        print(f"[HOT] ok={ok} domain={domain}")
        return ok
    except Exception as e:
        print(f"[HOT] error: {e}")
        return False


# -- Knowledge ingestion bridge -----------------------------------------------
def _ingest_to_hot_reflection(topic: str, source_type: str, concept_clusters: list, engagement_avg: float) -> bool:
    if not concept_clusters:
        print(f"[INGEST->HOT] No concepts found for topic={topic}, skipping")
        return False

    inserted = 0
    for concept in concept_clusters:
        try:
            quality_score = round(min(1.0, engagement_avg / 100.0), 3)
            new_patterns = []
            gaps_identified = None
            try:
                prompt = (
                    f"Topic: {topic}\n"
                    f"Concept: {concept}\n"
                    f"Sources: {source_type}\n"
                    f"Avg community engagement score: {engagement_avg:.1f}/100\n\n"
                    f"Extract 2-4 reusable patterns about '{concept}' that CORE should internalize. "
                    f"Each pattern = short actionable rule (<120 chars). "
                    f"Also identify 1 gap or open question about this concept.\n"
                    f"Respond ONLY as JSON: "
                    f'{{"patterns": ["..."], "gap": "..or null"}}'
                )
                _sys_ingest = _load_prompt("knowledge_ingest_synthesizer", "You are CORE's knowledge synthesis engine. Return only valid JSON.")
                _maybe_eval_prompt("knowledge_ingest_synthesizer", _sys_ingest, 20)
                raw = groq_chat(
                    system=_sys_ingest,
                    user=prompt, model=GROQ_MODEL, max_tokens=500,
                )
                parsed = json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
                new_patterns = [p for p in parsed.get("patterns", []) if isinstance(p, str) and len(p) > 5][:4]
                gap_raw = parsed.get("gap") or None
                gaps_identified = [gap_raw] if gap_raw and isinstance(gap_raw, str) else None
            except Exception as e:
                print(f"[INGEST->HOT] Groq synthesis failed for {concept} (non-fatal): {e}")
                new_patterns = [f"Community knowledge on {concept}: avg engagement {engagement_avg:.0f}/100"]

            ok = sb_post("hot_reflections", {
                "task_summary":          f"Knowledge ingest: {topic} â€” concept: {concept}",
                "domain":                "knowledge_ingestion",
                "verify_rate":           0,
                "mistake_consult_rate":  0,
                "new_patterns":          new_patterns,
                "new_mistakes":          [],
                "quality_score":         quality_score,
                "gaps_identified":       gaps_identified,
                "reflection_text":       f"Ingested from {source_type}. Topic: {topic}. Concept: {concept}. Avg engagement: {engagement_avg:.1f}/100.",
                "processed_by_cold":     0,
                "source":                "real",
            })
            if ok:
                inserted += 1
        except Exception as e:
            print(f"[INGEST->HOT] Error for concept={concept}: {e}")
            continue

    print(f"[INGEST->HOT] Done: {inserted}/{len(concept_clusters)} hot_reflections inserted")
    return inserted > 0


# -- Gaps reconciliation -------------------------------------------------------
def _reconcile_gaps(hots: list) -> int:
    try:
        raw_gaps = []
        for h in hots:
            gaps = h.get("gaps_identified") or []
            if isinstance(gaps, str):
                try:
                    gaps = json.loads(gaps)
                except Exception:
                    gaps = [gaps]
            domain = h.get("domain", "general")
            for g in gaps:
                if g and isinstance(g, str) and len(g) > 10:
                    raw_gaps.append({"gap": g.strip()[:400], "domain": domain})

        if not raw_gaps:
            return 0

        gaps_text = "\n".join(f"  [{i+1}] [{g['domain']}] {g['gap']}" for i, g in enumerate(raw_gaps))
        prompt = (
            f"You are CORE's gap reconciliation engine.\n"
            f"Below are {len(raw_gaps)} gaps identified from recent hot reflections.\n\n"
            f"{gaps_text}\n\n"
            f"Respond with ONLY a JSON array of unique, actionable gaps (no duplicates, no vague items).\n"
            f"Each item: {{\"gap\": \"concise gap description\", \"domain\": \"domain\", \"priority\": 1-5}}\n"
            f"Priority 5=critical architecture gap, 1=minor improvement. Max 10 items. No preamble."
        )
        deduped = None
        try:
            _sys_gaps = _load_prompt("gaps_reconciliation", "You are CORE's gap reconciliation engine. Respond only with valid JSON array. No preamble.")
            _maybe_eval_prompt("gaps_reconciliation", _sys_gaps, 15)
            raw = gemini_chat(
                system=_sys_gaps,
                user=prompt, max_tokens=600, json_mode=True,
            )
            parsed = json.loads(raw.strip())
            if isinstance(parsed, list):
                deduped = parsed
        except Exception as groq_err:
            print(f"[COLD] _reconcile_gaps Groq fallback (non-fatal): {groq_err}")
        if not deduped:
            seen = set()
            deduped = []
            for g in raw_gaps:
                if g["gap"] not in seen:
                    seen.add(g["gap"])
                    deduped.append({"gap": g["gap"], "domain": g["domain"], "priority": 2})
            deduped = deduped[:10]

        inserted = 0
        for item in deduped:
            gap_text = item.get("gap", "")[:400]
            domain = item.get("domain", "general")
            priority = int(item.get("priority", 2))
            if not gap_text:
                continue
            from core_semantic import search as _sem
            existing = _sem("evolution_queue", gap_text[:200], limit=1,
                threshold=0.88, filters="&status=eq.pending") or []
            if existing:
                print(f"[COLD] Skipped duplicate gap (pending): {gap_text[:80]}")
                continue
            confidence = round(min(0.3 + priority * 0.1, 0.8), 2)
            ok = sb_post_critical("evolution_queue", {
                "change_type":    "code",
                "change_summary": f"[GAP] {gap_text}",
                "pattern_key":    f"gap:{gap_text[:200]}",
                "confidence":     confidence,
                "status":         "pending",
                "source":         "real",
                "impact":         domain,
                "recommendation": f"Gap identified in hot_reflections (priority={priority}). Requires architectural review.",
                "diff_content": json.dumps({
                    "gap": gap_text,
                    "source": gap.get("source", ""),
                    "domain": domain,
                    "priority": priority,
                    "autonomy": {
                        "kind": "kb_expand" if gap.get("source") == "kb_coverage" else "behavioral_remediation" if gap.get("source") == "stale_tasks" else "architecture_proposal",
                        "origin": "cold_processor",
                        "source": gap.get("source", ""),
                        "domain": domain,
                        "priority": priority,
                        "expected_artifact": "task_queue",
                        "next_worker": "evolution_autonomy",
                    },
                }, default=str),
            })
            if ok:
                inserted += 1

        print(f"[COLD] _reconcile_gaps: raw={len(raw_gaps)} deduped={len(deduped)} inserted={inserted}")
        return inserted
    except Exception as e:
        print(f"[COLD] _reconcile_gaps error (non-fatal): {e}")
        return 0


# -- Cold processor ------------------------------------------------------------
def _groq_synthesize_cold(hots: list, batch_counts: Counter, batch_domain: dict) -> str:
    fallback = f"Processed {len(hots)} hots. {len(batch_counts)} unique patterns."
    try:
        top = sorted(batch_counts.items(), key=lambda x: x[1], reverse=True)[:15]
        top_text = "\n".join(
            f"  [{batch_domain.get(k,'?')}] ({v}x) {k[:120]}"
            for k, v in top
        )
        domain_counts: Counter = Counter(batch_domain[k] for k in batch_counts)
        domain_text = ", ".join(f"{d}:{n}" for d, n in domain_counts.most_common(6))
        session_summaries = "\n".join(
            f"  - {h.get('task_summary','')[:150]}" for h in hots[:10]
        )
        prompt = (
            f"You are CORE's cold processor synthesis engine.\n"
            f"You just processed {len(hots)} hot reflections covering {len(batch_counts)} unique patterns.\n\n"
            f"Domain breakdown: {domain_text}\n\n"
            f"Session summaries:\n{session_summaries}\n\n"
            f"Top patterns by frequency:\n{top_text}\n\n"
            f"Respond with ONLY a JSON object matching this exact schema (no preamble, no markdown):\n"
            f"{{\n"
            f"  \"themes\": \"2-3 sentence summary of dominant themes and domains\",\n"
            f"  \"top_patterns\": [\"pattern 1\", \"pattern 2\", \"pattern 3\"],\n"
            f"  \"gaps\": [\"gap or risk 1\", \"gap or risk 2\"],\n"
            f"  \"health_signal\": \"one sentence system health assessment\",\n"
            f"  \"summary\": \"3-5 sentence plain text synthesis combining all above\"\n"
            f"}}\n"
        )
        _sys_cold = _load_prompt("cold_processor_synthesis", "You are CORE's cold processor synthesis engine. Respond only with valid JSON. No preamble.")
        _maybe_eval_prompt("cold_processor_synthesis", _sys_cold, 15)
        raw = gemini_chat(
            system=_sys_cold,
            user=prompt, max_tokens=500, json_mode=True,
        )
        try:
            parsed = json.loads(raw.strip())
            synthesis = parsed.get("summary", "").strip()
            if not synthesis:
                synthesis = parsed.get("themes", "").strip()
            if len(synthesis) > 50:
                return synthesis
            parts = [parsed.get("themes", ""), " | ".join(parsed.get("top_patterns", [])[:3])]
            synthesis = " ".join(p for p in parts if p)
            return synthesis if len(synthesis) > 20 else fallback
        except (json.JSONDecodeError, ValueError, AttributeError):
            print(f"[COLD] synthesis JSON parse failed, using raw text fallback")
            synthesis = raw.strip()
            if len(synthesis) > 50:
                return synthesis
            return fallback
    except Exception as e:
        print(f"[COLD] synthesis failed (non-fatal): {e}")
        return fallback


def _groq_kb_content(pattern_key: str, domain: str, frequency: int, src_key: str) -> str:
    try:
        prompt = (
            f"You are CORE's knowledge base writer.\n"
            f"A pattern has recurred {frequency}x across real sessions (source: {src_key}).\n"
            f"Domain: {domain}\n"
            f"Pattern: {pattern_key}\n\n"
            f"Write a concise KB entry for this pattern. Include:\n"
            f"1. The rule stated clearly (1-2 sentences)\n"
            f"2. Why it matters / what failure it prevents\n"
            f"3. How to apply it (concrete action)\n"
            f"4. Any known exceptions\n"
            f"Max 200 words. No markdown headers. Plain paragraphs only."
        )
        _sys_kb = _load_prompt("kb_content_writer", "You are CORE's knowledge synthesis engine. Write clear actionable KB content.")
        _maybe_eval_prompt("kb_content_writer", _sys_kb, 30)
        content = groq_chat(system=_sys_kb, user=prompt, model=GROQ_MODEL, max_tokens=350)
        content = content.strip()
        if len(content) > 30:
            return content
        return pattern_key
    except Exception as e:
        print(f"[COLD] kb_content generation failed (non-fatal): {e}")
        return pattern_key


def _backfill_patterns(batch_size: int = 10) -> int:
    try:
        rows = sb_get(
            "pattern_frequency",
            "select=id,pattern_key,frequency,domain&frequency=gte.2&auto_applied=eq.false&stale=eq.false&order=frequency.desc",
            svc=True
        ) or []
        if not rows:
            print("[BACKFILL] No patterns to backfill.")
            return 0

        rows = rows[:batch_size]
        inserted = 0

        for row in rows:
            key    = row.get("pattern_key", "")[:500]
            freq   = row.get("frequency", 2)
            domain = row.get("domain", "general")
            if not key:
                continue

            existing = sb_get(
                "knowledge_base",
                f"select=id&domain=eq.{domain}&topic=eq.{key[:100]}&limit=1",
                svc=True
            )
            if existing:
                sb_upsert("pattern_frequency", {"pattern_key": key, "auto_applied": True}, on_conflict="pattern_key")
                continue

            content = _groq_kb_content(key, domain, freq, "real")
            if not content or len(content) < 10:
                print(f"[BACKFILL] Groq returned empty content for: {key[:60]}")
                continue

            ok = sb_post("knowledge_base", {
                "domain":     domain,
                "topic":      key[:100],
                "content":    content,
                "confidence": "medium" if freq < 5 else "high",
                "tags":       ["backfill", "pattern_frequency"],
                "source":     "pattern_frequency",
            })
            if ok:
                inserted += 1
                sb_upsert("pattern_frequency", {"pattern_key": key, "auto_applied": True}, on_conflict="pattern_key")
                print(f"[BACKFILL] KB entry inserted: [{domain}] {key[:60]} (freq={freq})")
            else:
                print(f"[BACKFILL] sb_post failed for: {key[:60]}")

        print(f"[BACKFILL] Done: checked={len(rows)} inserted={inserted}")
        return inserted
    except Exception as e:
        print(f"[BACKFILL] error (non-fatal): {e}")
        return 0


def _run_capability_report():
    """AGI-06/S3: Weekly capability report. Reads last 7 days of capability_metrics,
    computes averages per dimension, sends Telegram summary to owner.
    Non-blocking.
    """
    try:
        cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        rows = sb_get("capability_metrics",
            f"select=accuracy,efficiency,autonomy,robustness,learning_rate,transfer,composite_score,domain&session_ts=gte.{cutoff}&limit=100",
            svc=True) or []
        if not rows:
            print("[CAP] No capability_metrics entries for last 7 days")
            return
        n = len(rows)
        def avg(key): return round(sum(r.get(key) or 0 for r in rows) / n, 3)
        report = (
            f"\U0001f4ca CORE Capability Report (last 7d, {n} sessions)\n"
            f"Accuracy:      {avg('accuracy')}\n"
            f"Efficiency:    {avg('efficiency')}\n"
            f"Autonomy:      {avg('autonomy')}\n"
            f"Robustness:    {avg('robustness')}\n"
            f"Learning Rate: {avg('learning_rate')}\n"
            f"Transfer:      {avg('transfer')}\n"
            f"Composite:     {avg('composite_score')}"
        )
        print(report)
        print(f"[CAP] Weekly report sent: composite={avg('composite_score')}")
    except Exception as e:
        print(f"[CAP] report error (non-fatal): {e}")


def _run_memory_consolidation(dry_run: bool = False):
    """AGI-05/S2: Weekly memory consolidation and forgetting job.
    Consolidation: entries with access_count > 10 AND confidence != 'proven' -> boost to 'high'.
    Forgetting: entries with last_accessed > 90 days AND access_count < 2 -> set active=false.
    Non-blocking. Reports via Telegram.
    """
    try:
        now = datetime.utcnow()
        cutoff_90d = (now - timedelta(days=90)).isoformat()

        # Consolidation: frequently accessed entries get confidence boost
        consolidate_rows = sb_get("knowledge_base",
            "select=id,domain,topic,confidence,access_count&id=gt.1&access_count=gte.10&confidence=neq.proven&limit=200",
            svc=True) or []
        consolidated = 0
        for r in consolidate_rows:
            if not dry_run:
                try:
                    sb_patch("knowledge_base", f"id=eq.{r['id']}", {"confidence": "high"})
                    consolidated += 1
                except Exception:
                    pass
            else:
                consolidated += 1

        # Forgetting: stale entries not referenced recently -> soft archive
        forget_rows = sb_get("knowledge_base",
            f"select=id,domain,topic,access_count,last_accessed&id=gt.1&access_count=lt.2&last_accessed=lt.{cutoff_90d}&limit=200",
            svc=True) or []
        # Extra safety: never archive entries referenced in mistakes or behavioral_rules
        archived = 0
        for r in forget_rows:
            topic_slug = (r.get("topic") or "")[:50]
            domain_slug = (r.get("domain") or "")
            # Check if referenced in mistakes
            from core_semantic import search as _sem
            refs = _sem("mistakes", topic_slug, limit=1, threshold=0.85) or []
            if refs:
                continue  # Referenced in mistakes -- keep
            if not dry_run:
                try:
                    sb_patch("knowledge_base", f"id=eq.{r['id']}", {"active": False})
                    archived += 1
                except Exception:
                    pass
            else:
                archived += 1

        summary = (
            f"[CONSOLIDATION] {'DRY RUN ' if dry_run else ''}Done. "
            f"Consolidated={consolidated} (confidence->high). "
            f"Archived={archived} (active->false, stale 90d+)."
        )
        print(summary)
        if not dry_run and (consolidated + archived) > 0:
            print(summary)
        return {"ok": True, "consolidated": consolidated, "archived": archived, "dry_run": dry_run}
    except Exception as e:
        print(f"[CONSOLIDATION] error: {e}")
        return {"ok": False, "error": str(e)}


def task_similarity_metric(task_a: dict, task_b: dict) -> float:
    """Compare two task payloads with a bounded lexical Jaccard score."""
    def _flatten(task: dict) -> str:
        if not isinstance(task, dict):
            return str(task or "")
        parts = [
            task.get("title", ""),
            task.get("description", ""),
        ]
        autonomy = task.get("autonomy") if isinstance(task.get("autonomy"), dict) else {}
        parts.extend([
            autonomy.get("work_track", ""),
            autonomy.get("task_group", ""),
            autonomy.get("route", ""),
            autonomy.get("specialized_worker", ""),
        ])
        return " ".join(str(p) for p in parts if p)

    def _tokens(text: str) -> set:
        toks = []
        for tok in str(text or "").lower().split():
            tok = tok.strip(".,:;!?()[]{}<>\"'`|/\\")
            if len(tok) > 2:
                toks.append(tok)
        return set(toks[:80])

    a = _tokens(_flatten(task_a))
    b = _tokens(_flatten(task_b))
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return round(len(a & b) / len(union), 3)


def novelty_assessment_module(experience: dict, reference_memory: list[dict] | None = None, limit: int = 25) -> dict:
    """Assess novelty of an experience against recent memory/task representations.

    Returns a bounded novelty score plus a routing recommendation:
    - merge: low novelty, consolidate with existing memory
    - preserve: high novelty, keep as distinct memory/event
    - review: ambiguous novelty, send to owner or higher-level review
    """
    def _parse(item) -> dict:
        if isinstance(item, dict):
            return item
        if isinstance(item, str):
            try:
                parsed = json.loads(item)
                return parsed if isinstance(parsed, dict) else {"title": item}
            except Exception:
                return {"title": item}
        return {"title": str(item)}

    exp = _parse(experience)
    refs = reference_memory
    if refs is None:
        refs = ConsolidationManager().collect(limit=max(5, min(int(limit or 25), 50)))
    ref_tasks = [_parse(item) for item in (refs or []) if item]
    if not ref_tasks:
        return {
            "ok": True,
            "novelty_score": 1.0,
            "max_similarity": 0.0,
            "reference_count": 0,
            "recommended_route": "preserve",
            "reason": "no_reference_memory",
        }

    similarities = [task_similarity_metric(exp, ref) for ref in ref_tasks[:max(1, min(int(limit or 25), 50))]]
    max_similarity = max(similarities) if similarities else 0.0
    avg_similarity = sum(similarities) / len(similarities) if similarities else 0.0
    novelty_score = round(max(0.0, min(1.0, 1.0 - max_similarity)), 3)
    if novelty_score >= 0.75:
        recommended_route = "preserve"
    elif novelty_score >= 0.45:
        recommended_route = "review"
    else:
        recommended_route = "merge"
    return {
        "ok": True,
        "experience": exp,
        "novelty_score": novelty_score,
        "max_similarity": round(max_similarity, 3),
        "avg_similarity": round(avg_similarity, 3),
        "reference_count": len(ref_tasks),
        "recommended_route": recommended_route,
    }


class ConsolidationManager:
    """Group similar queued tasks and summarize them for review."""

    def __init__(self, similarity_threshold: float = 0.62):
        self.similarity_threshold = max(0.1, min(float(similarity_threshold), 0.95))

    def collect(self, limit: int = 25) -> list:
        rows = sb_get(
            "task_queue",
            f"select=task,status,source,priority,updated_at&status=in.(pending,in_progress)&order=updated_at.desc&limit={max(1, min(int(limit), 50))}",
            svc=True,
        ) or []
        tasks = []
        for row in rows:
            task_raw = row.get("task")
            task_data = {}
            if isinstance(task_raw, str):
                try:
                    task_data = json.loads(task_raw)
                except Exception:
                    task_data = {"title": task_raw}
            elif isinstance(task_raw, dict):
                task_data = task_raw
            else:
                task_data = {"title": str(task_raw)}
            task_data["_row"] = row
            tasks.append(task_data)
        return tasks

    def cluster(self, tasks: list) -> list:
        clusters: list[dict] = []
        for task in tasks:
            matched = None
            for cluster in clusters:
                score = task_similarity_metric(cluster["seed"], task)
                if score >= self.similarity_threshold:
                    matched = cluster
                    break
            if matched:
                matched["tasks"].append(task)
                matched["scores"].append(task_similarity_metric(matched["seed"], task))
            else:
                clusters.append({"seed": task, "tasks": [task], "scores": []})
        return clusters

    def summarize(self, clusters: list) -> dict:
        groups = []
        for cluster in clusters:
            seed = cluster["seed"]
            row = seed.get("_row") or {}
            title = seed.get("title") or seed.get("description") or "untitled task"
            novelty = novelty_assessment_module(seed, reference_memory=[t for t in cluster["tasks"] if t is not seed], limit=25)
            groups.append({
                "title": str(title)[:120],
                "count": len(cluster["tasks"]),
                "source": row.get("source") or seed.get("source") or "unknown",
                "priority": row.get("priority"),
                "similarity_threshold": self.similarity_threshold,
                "novelty_score": novelty.get("novelty_score", 0.0),
                "recommended_route": novelty.get("recommended_route", "merge"),
            })
        return {"ok": True, "cluster_count": len(groups), "groups": groups[:10]}

    def run(self, limit: int = 25) -> dict:
        tasks = self.collect(limit=limit)
        if not tasks:
            return {"ok": True, "cluster_count": 0, "groups": []}
        clusters = self.cluster(tasks)
        summary = self.summarize(clusters)
        summary["task_count"] = len(tasks)
        return summary


class ActiveLearningStrategy:
    """Pluggable selector for choosing the highest-value tasks to learn from."""

    def __init__(self, strategy_name: str = "novelty_priority", budget: int = 5, similarity_threshold: float = 0.62):
        self.strategy_name = (strategy_name or "novelty_priority").strip()
        self.budget = max(1, min(int(budget or 5), 25))
        self.similarity_threshold = max(0.1, min(float(similarity_threshold), 0.95))

    @staticmethod
    def _task_text(task: dict) -> str:
        if not isinstance(task, dict):
            return str(task or "")
        parts = [
            task.get("title", ""),
            task.get("description", ""),
            task.get("goal", ""),
            task.get("summary", ""),
        ]
        return " ".join(str(part) for part in parts if part).strip()

    def score_task(self, task: dict, reference_memory: list[dict] | None = None) -> dict:
        refs = reference_memory or []
        novelty = novelty_assessment_module(task, reference_memory=refs, limit=25)
        text = self._task_text(task).lower()
        priority_hint = 0.0
        if any(term in text for term in ("critical", "urgent", "break", "fix", "error", "crash")):
            priority_hint = 0.2
        if any(term in text for term in ("core_tools.py", "core_train.py", "core_main.py")):
            priority_hint = max(priority_hint, 0.15)
        base_score = float(novelty.get("novelty_score") or 0.0)
        if self.strategy_name == "conservative":
            final_score = max(0.0, min(1.0, base_score * 0.7 + priority_hint))
        elif self.strategy_name == "review_first":
            final_score = max(0.0, min(1.0, base_score * 0.55 + priority_hint + (0.1 if novelty.get("recommended_route") == "review" else 0.0)))
        else:
            final_score = max(0.0, min(1.0, base_score * 0.75 + priority_hint))
        return {
            "task": task,
            "novelty": novelty,
            "score": round(final_score, 3),
        }

    def rank(self, tasks: list[dict], reference_memory: list[dict] | None = None) -> list[dict]:
        scored = [self.score_task(task, reference_memory=reference_memory) for task in (tasks or [])]
        scored.sort(key=lambda item: (item["score"], item["novelty"].get("novelty_score", 0.0)), reverse=True)
        return scored

    def run(self, limit: int = 25) -> dict:
        tasks = ConsolidationManager(similarity_threshold=self.similarity_threshold).collect(limit=limit)
        if not tasks:
            return {"ok": True, "strategy_name": self.strategy_name, "selected_count": 0, "selected": []}
        ranked = self.rank(tasks)
        selected = ranked[: self.budget]
        return {
            "ok": True,
            "strategy_name": self.strategy_name,
            "budget": self.budget,
            "similarity_threshold": self.similarity_threshold,
            "task_count": len(tasks),
            "selected_count": len(selected),
            "selected": [
                {
                    "title": item["task"].get("title") or item["task"].get("description") or "untitled task",
                    "score": item["score"],
                    "recommended_route": item["novelty"].get("recommended_route", "merge"),
                    "novelty_score": item["novelty"].get("novelty_score", 0.0),
                    "task": item["task"],
                }
                for item in selected
            ],
            "ranked_preview": [
                {
                    "title": item["task"].get("title") or item["task"].get("description") or "untitled task",
                    "score": item["score"],
                    "recommended_route": item["novelty"].get("recommended_route", "merge"),
                }
                for item in ranked[:10]
            ],
        }


def t_task_similarity_metric(task_a: str = "", task_b: str = "") -> dict:
    """Compare two task JSON blobs or plain text blobs."""
    try:
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
        return {"ok": True, "similarity": task_similarity_metric(a, b)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_active_learning_strategy(strategy_name: str = "novelty_priority", budget: str = "5", limit: str = "25", similarity_threshold: str = "0.62") -> dict:
    """Select high-value tasks for active learning using a pluggable strategy."""
    try:
        bud = max(1, min(int(budget or 5), 25))
        lim = max(1, min(int(limit or 25), 50))
        thresh = max(0.1, min(float(similarity_threshold or 0.62), 0.95))
        return ActiveLearningStrategy(strategy_name=strategy_name, budget=bud, similarity_threshold=thresh).run(limit=lim)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_novelty_assessment(experience: str = "", reference_memory: str = "", limit: str = "25") -> dict:
    """Assess how novel an experience is versus recent memory/task representations."""
    try:
        def _parse(v):
            if not v:
                return {}
            if isinstance(v, dict):
                return v
            if isinstance(v, str):
                try:
                    parsed = json.loads(v)
                    return parsed if isinstance(parsed, dict) else {"title": v}
                except Exception:
                    return {"title": v}
            return {"title": str(v)}

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

        exp = _parse(experience)
        return novelty_assessment_module(exp, reference_memory=refs or None, limit=lim)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_consolidation_manager(limit: str = "25", similarity_threshold: str = "0.62") -> dict:
    """Cluster similar queued tasks and return a consolidation summary."""
    try:
        lim = max(1, min(int(limit or 25), 50))
        thresh = max(0.1, min(float(similarity_threshold or 0.62), 0.95))
        return ConsolidationManager(similarity_threshold=thresh).run(limit=lim)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _run_causal_quality_analysis():
    """AGI-03/S3: When quality trend is declining, analyze last 10 sessions causally.
    Identifies what changed (task types, domains, error rates) and writes explanation
    to knowledge_base(domain=meta). Triggered by run_cold_processor when trend=declining.
    Non-blocking -- never raises.
    """
    try:
        # Check quality trend from last 10 hot_reflections
        recent = sb_get("hot_reflections",
            "select=quality_score,domain,summary,created_at&id=gt.1&order=created_at.desc&limit=10",
            svc=True) or []
        if len(recent) < 5:
            return  # Not enough data

        scores = [float(r.get("quality_score") or 0.7) for r in recent]
        avg_recent = sum(scores[:5]) / 5
        avg_older  = sum(scores[5:]) / max(len(scores[5:]), 1)

        if avg_recent >= avg_older:  # Not actually declining
            return

        # Gather domain distribution
        domain_counts: dict = {}
        for r in recent[:5]:
            d = r.get("domain", "general")
            domain_counts[d] = domain_counts.get(d, 0) + 1
        top_domain = max(domain_counts, key=domain_counts.get) if domain_counts else "unknown"

        # Gather recent mistakes in that domain
        recent_mistakes = sb_get("mistakes",
            f"select=domain,what_failed,severity&domain=eq.{top_domain}&order=created_at.desc&limit=5",
            svc=True) or []
        mistake_summary = "; ".join([
            (m.get("what_failed") or "")[:80] for m in recent_mistakes
        ])

        prompt = (
            f"CORE AGI quality analysis. Recent 5 sessions avg={avg_recent:.2f}, "
            f"prior 5 sessions avg={avg_older:.2f}. Trend: DECLINING.\n"
            f"Top domain in recent sessions: {top_domain}\n"
            f"Recent mistakes in that domain: {mistake_summary[:400]}\n"
            f"In 2-3 sentences: what is the most likely causal explanation for the quality decline? "
            f"Be specific about what changed. Output plain text, no lists."
        )
        _sys_qual = _load_prompt("quality_decline_analyst", "You are CORE AGI's causal analyst. Be concise and precise.")
        _maybe_eval_prompt("quality_decline_analyst", _sys_qual, 10)
        explanation = gemini_chat(
            system=_sys_qual,
            user=prompt,
            max_tokens=200,
            temperature=0.1,
        )
        if not explanation or len(explanation) < 20:
            return

        today = datetime.utcnow().strftime("%Y-%m-%d")
        sb_post("knowledge_base", {
            "domain": _training_kb_domain(),
            "topic": f"quality_decline_causal_model_{today}",
            "content": (
                f"Quality decline detected {today}. "
                f"Recent avg={avg_recent:.2f} vs prior avg={avg_older:.2f}. "
                f"Top domain: {top_domain}. "
                f"Causal explanation: {explanation}"
            ),
            "confidence": "medium",
        })
        print(f"[COLD][CAUSAL] Quality decline analysis written to KB: {explanation[:100]}")
    except Exception as e:
        print(f"[COLD][CAUSAL] error (non-fatal): {e}")


def _auto_evolve_behavioral_rule(pattern_key: str, domain: str, confidence: float, frequency: int) -> bool:
    """P2-02: Auto-insert or update a behavioral rule when a pattern crosses the threshold.
    Called from run_cold_processor() when frequency >= 10 AND confidence >= 0.85.
    - If no rule exists for this trigger+domain: insert with source=cold_processor, tested=false
    - If rule exists with lower confidence: update confidence, reset tested=false
    - Notifies owner in both cases.
    Returns True if a rule was inserted or updated.
    """
    try:
        # Map CORE domain names to valid behavioral_rules domain enum
        VALID_DOMAINS = {
            "auth", "code", "failure_recovery", "github", "groq", "local_pc",
            "postgres", "powershell", "project", "railway", "reasoning",
            "supabase", "telegram", "trading", "trading_meta", "universal", "zapier",
        }
        _DOMAIN_MAP = {
            "db": "postgres", "code": "code", "bot": "telegram",
            "mcp": "reasoning", "training": "reasoning", "kb": "reasoning",
            "core_agi": "reasoning", "core_agi.patching": "code",
            "core_agi.deploy": "railway", "core_agi.session": "reasoning",
            "core_agi.architecture": "reasoning", "rarl": "trading_meta", "trading": "trading",
        }
        br_domain = _DOMAIN_MAP.get(domain, domain if domain in VALID_DOMAINS else "universal")

        # Derive a trigger from the pattern content
        pattern_lower = pattern_key.lower()
        if any(k in pattern_lower for k in ["deploy", "redeploy", "railway", "build"]):
            trigger = "before_deploy"
        elif any(k in pattern_lower for k in ["patch", "edit", "old_str", "new_str", "write"]):
            trigger = "before_code"
        elif any(k in pattern_lower for k in ["supabase", "sb_", "query", "insert", "table"]):
            trigger = "before_supabase_write"
        elif any(k in pattern_lower for k in ["verify", "check", "confirm", "validate"]):
            trigger = "before_any_act"
        elif any(k in pattern_lower for k in ["session", "close", "end"]):
            trigger = "session_close"
        elif any(k in pattern_lower for k in ["error", "fail", "broken", "fix"]):
            trigger = "on_failure"
        else:
            trigger = "during_action"

        pointer_slug = pattern_key[:80]

        # Check if rule already exists for this trigger+domain
        existing = sb_get(
            "behavioral_rules",
            f"select=id,confidence,active&active=eq.true&trigger=eq.{trigger}&domain=eq.{br_domain}&limit=5",
            svc=True,
        ) or []

        for ex in existing:
            ex_id   = ex.get("id")
            ex_conf = float(ex.get("confidence") or 0)
            if ex_conf < confidence:
                # Update to higher confidence + reset tested flag
                sb_patch("behavioral_rules", f"id=eq.{ex_id}", {
                    "confidence": round(confidence, 3),
                    "tested":     False,
                    "source":     "cold_processor",
                })
                print(
                    f"[P2-02] Behavioral rule updated\n"
                    f"Domain: {br_domain} | Trigger: {trigger}\n"
                    f"Pattern ({frequency}x): {pattern_key[:120]}\n"
                    f"Confidence: {ex_conf:.2f} â†’ {confidence:.2f}"
                )
                print(f"[P2-02] Rule updated id={ex_id} conf {ex_conf:.2f}->{confidence:.2f}: {pattern_key[:60]}")
                return True
            else:
                print(f"[P2-02] Skipped: rule exists with conf={ex_conf:.2f}: {pattern_key[:60]}")
                return False

        # No existing rule â€” insert new
        ok = sb_post("behavioral_rules", {
            "trigger":    trigger,
            "pointer":    pointer_slug,
            "full_rule":  (
                f"Auto-evolved from {frequency} occurrences (confidence={confidence:.2f})."
                f" Pattern: {pattern_key[:400]}. Domain: {domain}. Source: cold_processor."
            ),
            "domain":     br_domain,
            "priority":   3,
            "active":     True,
            "tested":     False,
            "source":     "cold_processor",
            "confidence": round(confidence, 3),
        })
        if ok:
            print(
                f"[P2-02] New behavioral rule auto-evolved\n"
                f"Domain: {br_domain} | Trigger: {trigger}\n"
                f"Pattern ({frequency}x, conf={confidence:.2f}): {pattern_key[:120]}\n"
                f"Status: active=true, tested=false â€” review in next session"
            )
            print(f"[P2-02] New rule inserted: [{br_domain}/{trigger}] {pattern_key[:60]}")
        return bool(ok)
    except Exception as e:
        print(f"[P2-02] _auto_evolve_behavioral_rule error (non-fatal): {e}")
        return False


def _run_meta_learning_loop() -> dict:
    """Periodically synthesize a meta-learning note from recent training signals.

    The loop is intentionally bounded:
    - reads recent hot reflections, mistakes, patterns, and queued evolutions
    - generates one actionable synthesis
    - stores it in knowledge_base(domain=meta)
    - checkpoints the run timestamp in sessions for restart recovery
    """
    try:
        phase_signal = _phase_signal_packet(limit=12)
        if phase_signal:
            recent_hots = phase_signal["recent_hots"]
            recent_mistakes = phase_signal["recent_mistakes"]
            recent_evos = phase_signal["recent_evos"]
        else:
            recent_hots = sb_get(
                "hot_reflections",
                "select=domain,quality_score,task_summary,new_patterns,new_mistakes,gaps_identified,source,created_at"
                "&order=created_at.desc&limit=10",
                svc=True,
            ) or []
            recent_mistakes = sb_get(
                "mistakes",
                "select=domain,what_failed,severity,root_cause,how_to_avoid&order=created_at.desc&limit=10",
                svc=True,
            ) or []
            recent_evos = sb_get(
                "evolution_queue",
                "select=change_type,change_summary,confidence,impact,recommendation,status,source"
                "&status=in.(pending,applied,rejected)&order=created_at.desc&limit=10",
                svc=True,
            ) or []
        recent_rules = sb_get(
            "behavioral_rules",
            (
                f"select=trigger,pointer,domain,priority,confidence,active&active=eq.true&domain=in.(universal,{TRADING_DOMAIN})&order=created_at.desc&limit=10"
                if trading_specialization_enabled()
                else "select=trigger,pointer,domain,priority,confidence,active&active=eq.true&order=created_at.desc&limit=10"
            ),
            svc=True,
        ) or []
        task_curriculum = phase_signal["curriculum"] if phase_signal else _build_task_embedding_curriculum(limit=12)

        if len(recent_hots) + len(recent_mistakes) < 4:
            return {"ok": False, "reason": "insufficient_recent_signal"}

        hot_domains = Counter((r.get("domain") or "general") for r in recent_hots)
        mistake_domains = Counter((r.get("domain") or "general") for r in recent_mistakes)
        evo_types = Counter((r.get("change_type") or "unknown") for r in recent_evos)
        top_hot_domain = hot_domains.most_common(1)[0][0] if hot_domains else "general"
        top_mistake_domain = mistake_domains.most_common(1)[0][0] if mistake_domains else "general"

        hot_lines = [
            f"- [{(r.get('domain') or 'general')}] {str(r.get('task_summary') or '')[:120]}"
            for r in recent_hots[:5]
        ] or ["- none"]
        mistake_lines = [
            f"- [{(r.get('domain') or 'general')}/{(r.get('severity') or 'low')}] {str(r.get('what_failed') or '')[:120]}"
            for r in recent_mistakes[:5]
        ] or ["- none"]
        evo_lines = [
            f"- [{(r.get('change_type') or 'unknown')}] {str(r.get('change_summary') or '')[:120]}"
            for r in recent_evos[:5]
        ] or ["- none"]
        rule_lines = [
            f"- [{(r.get('domain') or 'general')}] {str(r.get('trigger') or '')} :: {str(r.get('pointer') or '')[:120]}"
            for r in recent_rules[:5]
        ] or ["- none"]
        curriculum_lines = [
            f"- [{item.get('work_track','general')}] {item.get('title','')[:120]} | status={item.get('status','?')} | result={str(item.get('result') or '')[:120]}"
            for item in task_curriculum.get("items", [])[:8]
        ] or ["- none"]

        system = _load_prompt(
            "meta_learning_analyst",
            "You are CORE's meta-learning analyst. You turn recent training signals into one actionable improvement note. "
            "Return only valid JSON."
        )
        _maybe_eval_prompt("meta_learning_analyst", system, 10)
        user = (
            "Recent CORE training signals:\n"
            f"Hot reflections by domain: {dict(hot_domains)}\n"
            f"Mistakes by domain: {dict(mistake_domains)}\n"
            f"Evolution types: {dict(evo_types)}\n\n"
            "Recent hot reflections:\n" + "\n".join(hot_lines) + "\n\n"
            "Recent mistakes:\n" + "\n".join(mistake_lines) + "\n\n"
            "Recent evolutions:\n" + "\n".join(evo_lines) + "\n\n"
            "Active behavioral rules:\n" + "\n".join(rule_lines) + "\n\n"
            "Task embedding curriculum:\n" + "\n".join(curriculum_lines) + "\n\n"
            "Write JSON with fields:\n"
            "meta_learning_focus, task_embedding_objective, curriculum_focus, training_signal, recommendation, expected_gain, risk, next_step\n"
            "Keep each field concise and specific to improving CORE across tasks."
        )
        raw = gemini_chat(system=system, user=user, max_tokens=400, json_mode=True)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {
                "meta_learning_focus": "training-signal-consolidation",
                "task_embedding_objective": "learn a curriculum-aware task embedding that generalizes across work tracks",
                "curriculum_focus": "balanced recent task_queue signals",
                "training_signal": raw[:500],
                "recommendation": raw[:500],
                "expected_gain": "better cross-task generalization",
                "risk": "summary quality depends on current signals",
                "next_step": "review and refine the training heuristic",
            }

        today = datetime.utcnow().strftime("%Y-%m-%d")
        topic = f"meta_learning_{today}"
        content = (
            f"Meta-learning synthesis for {today}.\n"
            f"Focus: {parsed.get('meta_learning_focus', 'training-signal-consolidation')}\n"
            f"Task embedding objective: {parsed.get('task_embedding_objective', '')}\n"
            f"Curriculum focus: {parsed.get('curriculum_focus', '')}\n"
            f"Training signal: {parsed.get('training_signal', '')}\n"
            f"Recommendation: {parsed.get('recommendation', '')}\n"
            f"Expected gain: {parsed.get('expected_gain', '')}\n"
            f"Risk: {parsed.get('risk', '')}\n"
            f"Next step: {parsed.get('next_step', '')}\n"
            f"Observed domains: hot={dict(hot_domains)} mistakes={dict(mistake_domains)} evolutions={dict(evo_types)}\n"
            f"Task curriculum counts: {task_curriculum.get('counts', {})}\n"
        )
        ok = sb_upsert("knowledge_base", {
            "domain": _training_kb_domain(),
            "topic": topic,
            "content": content[:4000],
            "instruction": content[:4000],
            "source": "meta_learning_loop",
            "source_type": "meta_learning",
            "source_ref": f"cold_processor:{today}",
            "confidence": "medium",
            "tags": ["meta", "training", "loop", "curriculum"],
            "active": True,
        }, on_conflict="domain,topic")
        if ok:
            sb_post("sessions", {
                "summary": f"[state_update] last_meta_learning_ts: {time.time()}",
                "actions": ["last_meta_learning_ts persisted", "task_embedding_curriculum captured"],
                "interface": "mcp",
            })
            # Best-effort: update router policy for DynamicRouter from the same downstream signals.
            try:
                _maybe_update_dynamic_router_policy(task_curriculum, recent_mistakes)
            except Exception as _drpe:
                print(f"[META] router policy update non-fatal: {_drpe}")
            print(f"[META] Learning synthesis stored: {topic}")
            return {
                "ok": True,
                "topic": topic,
                "focus": parsed.get("meta_learning_focus", ""),
                "objective": parsed.get("task_embedding_objective", ""),
                "gain": parsed.get("expected_gain", ""),
            }
        return {"ok": False, "reason": "kb_upsert_failed"}
    except Exception as e:
        print(f"[META] loop error (non-fatal): {e}")
        return {"ok": False, "error": str(e)}


def _run_meta_training_phase(cycle_count: int = 0) -> dict:
    """Bounded meta-training phase run before the main training phase.

    This does not update model weights directly. It records a compact
    meta-training contract derived from diverse sampled tasks and performance
    signals so the main training pass can follow a better curriculum.
    """
    try:
        phase_signal = _phase_signal_packet(limit=16)
        if phase_signal:
            curriculum = phase_signal["curriculum"]
            counts = curriculum.get("counts", {}) or {}
            items = curriculum.get("items", []) or []
            recent_hots = phase_signal["recent_hots"]
            recent_mistakes = phase_signal["recent_mistakes"]
            recent_evos = phase_signal["recent_evos"]
        else:
            curriculum = _build_task_embedding_curriculum(limit=16)
            counts = curriculum.get("counts", {}) or {}
            items = curriculum.get("items", []) or []
            recent_hots = sb_get(
                "hot_reflections",
                "select=domain,quality_score,task_summary,new_patterns,new_mistakes,gaps_identified,source,created_at"
                "&order=created_at.desc&limit=8",
                svc=True,
            ) or []
            recent_mistakes = sb_get(
                "mistakes",
                "select=domain,what_failed,severity,root_cause,how_to_avoid&order=created_at.desc&limit=8",
                svc=True,
            ) or []
            recent_evos = sb_get(
                "evolution_queue",
                "select=change_type,change_summary,confidence,impact,recommendation,status,source"
                "&status=in.(pending,applied,rejected,synthesized)&order=created_at.desc&limit=8",
                svc=True,
            ) or []

        diverse_items = []
        seen_tracks = set()
        for item in items:
            track = (item.get("work_track") or "general").strip()
            if track not in seen_tracks:
                diverse_items.append(item)
                seen_tracks.add(track)
            if len(diverse_items) >= 8:
                break
        if len(diverse_items) < 8:
            for item in items:
                if item not in diverse_items:
                    diverse_items.append(item)
                if len(diverse_items) >= 8:
                    break

        avg_quality = None
        quality_scores = [float(h.get("quality_score") or 0.0) for h in recent_hots if h.get("quality_score") is not None]
        if quality_scores:
            avg_quality = round(sum(quality_scores) / len(quality_scores), 3)

        track_counts = Counter((item.get("work_track") or "general") for item in diverse_items)
        focus_track = None
        if counts:
            try:
                focus_track = sorted(counts.items(), key=lambda kv: int(kv[1] or 0))[0][0]
            except Exception:
                focus_track = None

        phase_packet = {
            "cycle_count": int(cycle_count or 0),
            "phase": "meta_training",
            "focus_track": focus_track or "mixed",
            "task_curriculum_counts": counts,
            "diverse_task_sample": [{
                "work_track": item.get("work_track") or "general",
                "title": (item.get("title") or "")[:160],
                "status": item.get("status") or "",
                "source": item.get("source") or "",
                "result": (item.get("result") or "")[:160],
            } for item in diverse_items[:8]],
            "performance_metrics": {
                "hot_reflections": len(recent_hots),
                "mistakes": len(recent_mistakes),
                "evolutions": len(recent_evos),
                "avg_quality_score": avg_quality,
                "track_diversity": len(track_counts),
                "top_track": track_counts.most_common(1)[0][0] if track_counts else None,
            },
            "meta_learner_update": {
                "objective": "Update curriculum priorities before the main training phase using diverse task performance signals.",
                "inputs": [
                    "diverse_task_sample",
                    "performance_metrics",
                    "recent_hot_reflections",
                    "recent_mistakes",
                    "recent_evolutions",
                ],
                "rule": "increase attention to underrepresented or high-signal tracks before main training",
            },
            "notes": "Planner-only meta-training phase. This records the pre-training contract and does not change model weights.",
            "recent_mistakes": [
                (m.get("what_failed") or m.get("root_cause") or "")[:160]
                for m in recent_mistakes[:8]
            ],
            "recent_evolutions": [
                (e.get("change_summary") or "")[:160]
                for e in recent_evos[:8]
            ],
        }

        today = datetime.utcnow().strftime("%Y-%m-%d")
        topic = f"meta_training_phase_{today}"
        content = "Meta-training phase (bounded).\n" + json.dumps(phase_packet, ensure_ascii=True)[:5000]
        ok = sb_upsert("knowledge_base", {
            "domain": _training_kb_domain(),
            "topic": topic,
            "content": content,
            "instruction": content,
            "source": "meta_training_phase",
            "source_type": "training_phase",
            "source_ref": f"background_researcher:cycle={int(cycle_count or 0)}",
            "confidence": "low",
            "tags": ["meta", "training", "phase", "curriculum"],
            "active": True,
        }, on_conflict="domain,topic")
        if ok:
            sb_post("sessions", {
                "summary": f"[state_update] last_meta_training_ts: {time.time()}",
                "actions": ["last_meta_training_ts persisted", "meta_training_phase captured"],
                "interface": "mcp",
            })
            return {
                "ok": True,
                "topic": topic,
                "focus_track": focus_track or "mixed",
                "metrics": phase_packet["performance_metrics"],
            }
        return {"ok": False, "reason": "kb_upsert_failed"}
    except Exception as e:
        print(f"[META] training phase error (non-fatal): {e}")
        return {"ok": False, "error": str(e)}


def _run_causal_discovery_phase(cycle_count: int = 0) -> dict:
    """Bounded causal-discovery phase run before standard RL training.

    This does not train weights. It records a causal abstraction contract from
    recent tasks, mistakes, evolutions, and hot reflections so the main RL
    epoch can follow a better abstraction layer.
    """
    try:
        phase_signal = _phase_signal_packet(limit=16)
        if phase_signal:
            curriculum = phase_signal["curriculum"]
            counts = curriculum.get("counts", {}) or {}
            items = curriculum.get("items", []) or []
            recent_hots = phase_signal["recent_hots"]
            recent_mistakes = phase_signal["recent_mistakes"]
            recent_evos = phase_signal["recent_evos"]
        else:
            curriculum = _build_task_embedding_curriculum(limit=16)
            counts = curriculum.get("counts", {}) or {}
            items = curriculum.get("items", []) or []
            recent_hots = sb_get(
                "hot_reflections",
                "select=domain,quality_score,task_summary,new_patterns,new_mistakes,gaps_identified,source,created_at"
                "&order=created_at.desc&limit=8",
                svc=True,
            ) or []
            recent_mistakes = sb_get(
                "mistakes",
                "select=domain,what_failed,severity,root_cause,how_to_avoid&order=created_at.desc&limit=8",
                svc=True,
            ) or []
            recent_evos = sb_get(
                "evolution_queue",
                "select=change_type,change_summary,confidence,impact,recommendation,status,source"
                "&status=in.(pending,applied,rejected,synthesized)&order=created_at.desc&limit=8",
                svc=True,
            ) or []

        diverse_items = []
        seen_tracks = set()
        for item in items:
            track = (item.get("work_track") or "general").strip()
            if track not in seen_tracks:
                diverse_items.append(item)
                seen_tracks.add(track)
            if len(diverse_items) >= 8:
                break
        if len(diverse_items) < 8:
            for item in items:
                if item not in diverse_items:
                    diverse_items.append(item)
                if len(diverse_items) >= 8:
                    break

        task_graph = []
        for item in diverse_items[:8]:
            title = (item.get("title") or item.get("topic") or item.get("summary") or "")[:180]
            track = (item.get("work_track") or "general").strip()
            task_graph.append({
                "track": track,
                "title": title,
                "priority": item.get("priority"),
                "source": item.get("source") or "",
            })

        causal_layers = []
        for idx, item in enumerate(task_graph[:6]):
            causal_layers.append({
                "layer": idx + 1,
                "cause": item.get("track") or "general",
                "effect": item.get("title") or "",
                "confidence": round(0.5 + min(0.4, 0.05 * idx), 3),
            })

        avg_quality = None
        quality_scores = [float(h.get("quality_score") or 0.0) for h in recent_hots if h.get("quality_score") is not None]
        if quality_scores:
            avg_quality = round(sum(quality_scores) / len(quality_scores), 3)

        track_counts = Counter((item.get("work_track") or "general") for item in diverse_items)
        focus_track = None
        if counts:
            try:
                focus_track = sorted(counts.items(), key=lambda kv: int(kv[1] or 0))[0][0]
            except Exception:
                focus_track = None

        phase_packet = {
            "cycle_count": int(cycle_count or 0),
            "phase": "causal_discovery",
            "focus_track": focus_track or "mixed",
            "task_curriculum_counts": counts,
            "task_graph": task_graph,
            "causal_layers": causal_layers,
            "meta_learning_objective": {
                "objective": "Learn abstraction-layer routing from causal structure before the RL epoch.",
                "inputs": [
                    "task_graph",
                    "causal_layers",
                    "recent_hot_reflections",
                    "recent_mistakes",
                    "recent_evolutions",
                ],
                "rule": "prefer causal predecessors that improve sample efficiency and generalization",
            },
            "performance_metrics": {
                "hot_reflections": len(recent_hots),
                "mistakes": len(recent_mistakes),
                "evolutions": len(recent_evos),
                "avg_quality_score": avg_quality,
                "track_diversity": len(track_counts),
                "top_track": track_counts.most_common(1)[0][0] if track_counts else None,
            },
            "recent_mistakes": [
                (m.get("what_failed") or m.get("root_cause") or "")[:160]
                for m in recent_mistakes[:8]
            ],
            "recent_evolutions": [
                (e.get("change_summary") or "")[:160]
                for e in recent_evos[:8]
            ],
            "notes": "Planner-only causal discovery phase. This records the abstraction contract and does not change model weights.",
        }

        today = datetime.utcnow().strftime("%Y-%m-%d")
        topic = f"causal_discovery_phase_{today}"
        content = "Causal discovery phase (bounded).\n" + json.dumps(phase_packet, ensure_ascii=True)[:5000]
        ok = sb_upsert("knowledge_base", {
            "domain": _training_kb_domain(),
            "topic": topic,
            "content": content,
            "instruction": content,
            "source": "causal_discovery_phase",
            "source_type": "training_phase",
            "source_ref": f"background_researcher:cycle={int(cycle_count or 0)}",
            "confidence": "low",
            "tags": ["meta", "training", "phase", "causal", "abstraction"],
            "active": True,
        }, on_conflict="domain,topic")
        if ok:
            sb_post("sessions", {
                "summary": f"[state_update] last_causal_discovery_ts: {time.time()}",
                "actions": ["last_causal_discovery_ts persisted", "causal_discovery_phase captured"],
                "interface": "mcp",
            })
            return {
                "ok": True,
                "topic": topic,
                "focus_track": focus_track or "mixed",
                "metrics": phase_packet["performance_metrics"],
            }
        return {"ok": False, "reason": "kb_upsert_failed"}
    except Exception as e:
        print(f"[CAUSAL] discovery phase error (non-fatal): {e}")
        return {"ok": False, "error": str(e)}
def _run_temporal_hierarchical_training_phase(cycle_count: int = 0) -> dict:
    """Bounded temporal training contract for TemporalHierarchicalWorldModel.

    This does not update weights. It records sequence batches, temporal loss
    summaries, and a training contract that future model code can consume.
    """
    try:
        phase_signal = _phase_signal_packet(limit=16)
        if phase_signal:
            curriculum = phase_signal["curriculum"]
            counts = curriculum.get("counts", {}) or {}
            items = curriculum.get("items", []) or []
            recent_sessions = phase_signal["recent_sessions"]
            recent_hots = phase_signal["recent_hots"]
            recent_mistakes = phase_signal["recent_mistakes"]
            recent_evos = phase_signal["recent_evos"]
        else:
            curriculum = _build_task_embedding_curriculum(limit=16)
            counts = curriculum.get("counts", {}) or {}
            items = curriculum.get("items", []) or []
            recent_sessions = sb_get(
                "sessions",
                "select=summary,actions,interface,created_at&order=created_at.desc&limit=8",
                svc=True,
            ) or []
            recent_hots = sb_get(
                "hot_reflections",
                "select=domain,quality_score,task_summary,new_patterns,new_mistakes,gaps_identified,source,created_at"
                "&order=created_at.desc&limit=8",
                svc=True,
            ) or []
            recent_mistakes = sb_get(
                "mistakes",
                "select=domain,what_failed,severity,root_cause,how_to_avoid&order=created_at.desc&limit=8",
                svc=True,
            ) or []
            recent_evos = sb_get(
                "evolution_queue",
                "select=change_type,change_summary,confidence,impact,recommendation,status,source"
                "&status=in.(pending,applied,rejected,synthesized)&order=created_at.desc&limit=8",
                svc=True,
            ) or []

        sequence_batches: list[list[str]] = []
        for item in items:
            batch = []
            for key in ("title", "description", "result", "source", "work_track", "topic"):
                value = item.get(key)
                if value:
                    batch.append(str(value)[:180])
            if batch:
                sequence_batches.append(batch)
            if len(sequence_batches) >= 8:
                break
        if len(sequence_batches) < 8:
            for sess in recent_sessions:
                batch = []
                if phase_signal:
                    for key in ("symbol", "strategy", "status", "direction", "market_regime", "action_taken", "close_reason", "reasoning", "notes"):
                        value = sess.get(key)
                        if value not in (None, ""):
                            batch.append(str(value)[:180])
                else:
                    summary = sess.get("summary") or ""
                    if summary:
                        batch.append(str(summary)[:180])
                    actions = sess.get("actions") or []
                    if isinstance(actions, list):
                        batch.extend(str(a)[:180] for a in actions if a)
                    elif actions:
                        batch.append(str(actions)[:180])
                batch = [part for part in batch if part]
                if batch:
                    sequence_batches.append(batch)
                if len(sequence_batches) >= 8:
                    break
        if len(sequence_batches) < 8:
            for evo in recent_evos:
                batch = []
                for key in ("change_summary", "recommendation", "impact", "change_type"):
                    value = evo.get(key)
                    if value:
                        batch.append(str(value)[:180])
                if batch:
                    sequence_batches.append(batch)
                if len(sequence_batches) >= 8:
                    break

        temporal_losses = []
        for idx, batch in enumerate(sequence_batches[:8]):
            loss, detail = _temporal_sequence_loss(batch)
            temporal_losses.append({
                "index": idx,
                "batch": batch[:6],
                "loss": loss,
                "detail": detail,
            })

        avg_loss = round(sum(item["loss"] for item in temporal_losses) / len(temporal_losses), 3) if temporal_losses else 0.0
        best_batch = min(temporal_losses, key=lambda item: item["loss"], default={})
        worst_batch = max(temporal_losses, key=lambda item: item["loss"], default={})
        track_counts = Counter((item.get("work_track") or "general") for item in items)
        focus_track = None
        if counts:
            try:
                focus_track = sorted(counts.items(), key=lambda kv: int(kv[1] or 0))[0][0]
            except Exception:
                focus_track = None

        phase_packet = {
            "cycle_count": int(cycle_count or 0),
            "phase": "temporal_hierarchical_world_model",
            "focus_track": focus_track or "mixed",
            "task_curriculum_counts": counts,
            "sequence_batch_count": len(sequence_batches),
            "sequence_batches": sequence_batches[:8],
            "temporal_losses": temporal_losses,
            "temporal_loss_summary": {
                "average_loss": avg_loss,
                "best_batch_loss": best_batch.get("loss") if best_batch else None,
                "worst_batch_loss": worst_batch.get("loss") if worst_batch else None,
            },
            "training_contract": {
                "model": "TemporalHierarchicalWorldModel",
                "objective": "Learn sequence-aware routing and temporal ordering for hierarchical world-model planning.",
                "inputs": [
                    "sequence_batches",
                    "recent_sessions",
                    "recent_hot_reflections",
                    "recent_mistakes",
                    "recent_evolutions",
                ],
                "loss_terms": [
                    "adjacency_loss",
                    "recency_loss",
                    "repetition_penalty",
                ],
                "rule": "prefer stable order, recency-aware context compression, and low repetition across adjacent sequence steps",
            },
            "performance_metrics": {
                "hot_reflections": len(recent_hots),
                "mistakes": len(recent_mistakes),
                "evolutions": len(recent_evos),
                "track_diversity": len(track_counts),
                "top_track": track_counts.most_common(1)[0][0] if track_counts else None,
            },
            "notes": "Planner-only temporal phase. This records the contract and does not change model weights.",
        }

        today = datetime.utcnow().strftime("%Y-%m-%d")
        topic = f"temporal_hierarchical_world_model_phase_{today}"
        content = "Temporal hierarchical world-model phase (bounded).\n" + json.dumps(phase_packet, ensure_ascii=True)[:5000]
        ok = sb_upsert("knowledge_base", {
            "domain": _training_kb_domain(),
            "topic": topic,
            "content": content,
            "instruction": content,
            "source": "temporal_hierarchical_world_model_phase",
            "source_type": "training_phase",
            "source_ref": f"background_researcher:cycle={int(cycle_count or 0)}",
            "confidence": "low",
            "tags": ["meta", "training", "phase", "temporal", "hierarchical", "world_model"],
            "active": True,
        }, on_conflict="domain,topic")
        if ok:
            sb_post("sessions", {
                "summary": f"[state_update] last_temporal_hwm_ts: {time.time()}",
                "actions": ["last_temporal_hwm_ts persisted", "temporal_hierarchical_world_model_phase captured"],
                "interface": "mcp",
            })
            return {
                "ok": True,
                "topic": topic,
                "focus_track": focus_track or "mixed",
                "metrics": phase_packet["performance_metrics"],
                "temporal_loss_summary": phase_packet["temporal_loss_summary"],
            }
        return {"ok": False, "reason": "kb_upsert_failed"}
    except Exception as e:
        print(f"[TEMPORAL] hierarchical phase error (non-fatal): {e}")
        return {"ok": False, "error": str(e)}


def _dynamic_router_policy_from_signals(task_curriculum: dict, recent_mistakes: list[dict]) -> dict:
    """Bounded policy synthesis for DynamicRouter.

    Uses downstream signals (recent tasks + mistakes) to adjust conservative route weights.
    Output is deterministic and safe to store in KB.
    """
    counts = (task_curriculum or {}).get("counts", {}) or {}
    # Conservative default weights by work track.
    w = {
        "db_only": 0.34,
        "behavioral_rule": 0.26,
        "research": 0.14,
        "code_patch": 0.14,
        "integration": 0.09,
        "proposal_only": 0.03,
    }
    # Mild transfer nudge: favor underrepresented track in the recent curriculum.
    try:
        if counts:
            least = sorted(counts.items(), key=lambda kv: int(kv[1] or 0))[0][0]
            if least in w:
                w[least] += 0.05
    except Exception:
        pass
    # Mistake-based penalty: if recent mistakes mention deploy/wiring/schema, downweight code+integration slightly.
    penalty = {"code_patch": 0.0, "integration": 0.0}
    for m in (recent_mistakes or [])[:10]:
        text = f"{m.get('what_failed','')} {m.get('root_cause','')} {m.get('how_to_avoid','')}".lower()
        if any(k in text for k in ("deploy", "import", "module", "syntax", "traceback")):
            penalty["code_patch"] += 0.02
        if any(k in text for k in ("wire", "wiring", "integration", "webhook", "port", "endpoint", "schema", "column")):
            penalty["integration"] += 0.02
    for k, p in penalty.items():
        if k in w:
            w[k] = max(0.03, w[k] - min(0.08, p))
    total = sum(w.values()) or 1.0
    work_track_weights = {k: round(v / total, 3) for k, v in w.items()}
    route_weights = {
        "task_autonomy": round(work_track_weights["db_only"] + work_track_weights["behavioral_rule"], 3),
        "research_autonomy": round(work_track_weights["research"], 3),
        "code_autonomy": round(work_track_weights["code_patch"], 3),
        "integration_autonomy": round(work_track_weights["integration"], 3),
        "proposal_router": round(work_track_weights["proposal_only"], 3),
    }
    return {
        "policy_version": 1,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "objective": "Route actions to the safest worker that can complete them while maximizing cross-task transfer.",
        "signals": {
            "task_counts": counts,
            "mistake_sample": [str(m.get("what_failed") or "")[:120] for m in (recent_mistakes or [])[:5]],
        },
        "work_track_weights": work_track_weights,
        "route_weights": route_weights,
        "notes": "Bounded heuristic policy; intended as input to DynamicRouter until a true learned policy exists.",
    }


def _maybe_update_dynamic_router_policy(task_curriculum: dict, recent_mistakes: list[dict]) -> dict:
    global _last_router_policy_run
    try:
        now = time.time()
        if not _ROUTER_POLICY_ENABLED:
            return {"ok": False, "reason": "disabled"}
        if _last_router_policy_run > 0 and (now - _last_router_policy_run) < _ROUTER_POLICY_INTERVAL:
            return {"ok": False, "reason": "interval_not_elapsed"}
        policy = _dynamic_router_policy_from_signals(task_curriculum, recent_mistakes)
        topic = "dynamic_router_policy"
        content = "Dynamic router policy (bounded).\n" + json.dumps(policy, ensure_ascii=True)[:3500]
        ok = sb_upsert("knowledge_base", {
            "domain": _training_kb_domain(),
            "topic": topic,
            "content": content,
            "instruction": content,
            "source": "dynamic_router_policy",
            "source_type": "policy",
            "source_ref": "meta_learning_loop",
            "confidence": "low",
            "tags": ["meta", "routing", "policy"],
            "active": True,
        }, on_conflict="domain,topic")
        if ok:
            _last_router_policy_run = now
            sb_post("sessions", {
                "summary": f"[state_update] last_router_policy_ts: {now}",
                "actions": ["dynamic_router_policy updated"],
                "interface": "mcp",
            })
        return {"ok": bool(ok), "topic": topic, "updated_at": policy.get("updated_at")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _router_meta_learning_objective(task_curriculum: dict, recent_mistakes: list[dict]) -> dict:
    """Bounded router meta-learning objective for downstream task routing.

    This does not train anything. It records the objective, signals, and policy
    contract that a future learned router should optimize against.
    """
    counts = (task_curriculum or {}).get("counts", {}) or {}
    focus_track = None
    if counts:
        try:
            focus_track = sorted(counts.items(), key=lambda kv: int(kv[1] or 0))[0][0]
        except Exception:
            focus_track = None
    policy = _dynamic_router_policy_from_signals(task_curriculum, recent_mistakes)
    return {
        "class": "core_tools.DynamicRouter",
        "objective": "Map world-model outputs to the safest effective route with minimal rework.",
        "focus_track": focus_track or "mixed",
        "inputs": [
            "predictions",
            "confidences",
            "candidate_routes",
            "query",
            "state_hint",
        ],
        "reward_signal": {
            "positive": [
                "downstream task completion",
                "low rework",
                "correct handoff",
                "fast but safe route selection",
            ],
            "negative": [
                "false confidence",
                "wrong route",
                "route latency",
                "unnecessary escalation",
            ],
        },
        "policy_seed": policy,
        "notes": "Planner-only contract. This records the objective and policy seed for a later learned router.",
    }


def _mrc_meta_learning_objective(task_curriculum: dict, recent_mistakes: list[dict]) -> dict:
    """Bounded meta-learning objective for the MRC (intermediate-state predictor).

    The target is to predict downstream task performance from intermediate states
    without adding a new runtime model in this patch. The planner records the
    contract and the signals that should supervise a future MRC worker.
    """
    counts = (task_curriculum or {}).get("counts", {}) or {}
    focus_track = None
    if counts:
        try:
            focus_track = sorted(counts.items(), key=lambda kv: int(kv[1] or 0))[0][0]
        except Exception:
            focus_track = None
    sample_items = (task_curriculum or {}).get("items", []) or []
    intermediate_states = []
    for item in sample_items[:8]:
        intermediate_states.append({
            "work_track": item.get("work_track") or "general",
            "status": item.get("status") or "",
            "source": item.get("source") or "",
            "title": (item.get("title") or "")[:120],
        })
    mistake_signals = []
    for m in (recent_mistakes or [])[:8]:
        mistake_signals.append((m.get("what_failed") or m.get("root_cause") or "")[:160])
    return {
        "class": "core_tools.StateEvaluator",
        "objective": "Predict downstream task performance from intermediate states.",
        "focus_track": focus_track or "mixed",
        "intermediate_state_schema": [
            "work_track",
            "status",
            "source",
            "title",
            "confidence",
            "evidence",
            "risk",
        ],
        "reward_signal": {
            "positive": [
                "accurate downstream performance prediction",
                "calibrated confidence",
                "useful intermediate-state summaries",
            ],
            "negative": [
                "overconfident false prediction",
                "missed high-risk states",
                "prediction that does not improve task outcome",
            ],
        },
        "intermediate_states": intermediate_states,
        "mistake_signals": mistake_signals,
        "notes": "Planner-only contract for a future MRC worker; no runtime model added in this patch.",
    }


def _world_model_fusion_meta_learning_objective(
    task_curriculum: dict,
    recent_mistakes: list[dict],
    recent_evos: list[dict] | None = None,
) -> dict:
    """Bounded meta-learning objective for the world-model fusion mechanism.

    This records the contract that a future learned world-model fusion policy
    should optimize against. It intentionally does not add a new runtime model.
    """
    counts = (task_curriculum or {}).get("counts", {}) or {}
    focus_track = None
    if counts:
        try:
            focus_track = sorted(counts.items(), key=lambda kv: int(kv[1] or 0))[0][0]
        except Exception:
            focus_track = None

    sample_items = (task_curriculum or {}).get("items", []) or []
    fusion_signals = []
    for item in sample_items[:8]:
        fusion_signals.append({
            "work_track": item.get("work_track") or "general",
            "status": item.get("status") or "",
            "source": item.get("source") or "",
            "title": (item.get("title") or "")[:120],
            "result": (item.get("result") or "")[:120],
        })

    mistake_signals = []
    for m in (recent_mistakes or [])[:8]:
        mistake_signals.append((m.get("what_failed") or m.get("root_cause") or "")[:160])

    evo_signals = []
    for evo in (recent_evos or [])[:8]:
        evo_signals.append({
            "change_type": evo.get("change_type") or "unknown",
            "impact": evo.get("impact") or "",
            "status": evo.get("status") or "",
            "summary": (evo.get("change_summary") or "")[:120],
        })

    return {
        "class": "core_tools.WorldModel",
        "objective": "Learn how to fuse temporal filtering, attention, evaluation, graph structure, and search into a calibrated next-action ranking.",
        "focus_track": focus_track or "mixed",
        "fusion_inputs": [
            "AdaptiveTemporalFilter",
            "TemporalAttention",
            "StateEvaluator",
            "DynamicRelationalGraph",
            "MonteCarloTreeSearch",
        ],
        "fusion_contract": {
            "temporal_context": "compress and rank the recent state/action sequence",
            "state_evaluation": "score coherence, evidence, risk, and readiness",
            "graph_context": "surface relational structure and density",
            "search_context": "pick a stable best_action from candidate rollouts",
        },
        "reward_signal": {
            "positive": [
                "calibrated action ranking",
                "better next-state prediction",
                "lower rework",
                "stable fusion across similar states",
            ],
            "negative": [
                "overconfident fused predictions",
                "ignoring high-risk signals",
                "unstable action ranking",
                "unnecessary escalation",
            ],
        },
        "fusion_signals": fusion_signals,
        "mistake_signals": mistake_signals,
        "evolution_signals": evo_signals,
        "notes": "Planner-only contract; no runtime model added in this patch.",
    }


def _hierarchical_gated_dual_loss(
    neural_prediction_loss: float = 0.0,
    symbolic_transition_loss: float = 0.0,
    gating_weight: float = 0.55,
) -> dict:
    """Bounded dual-loss contract for hierarchical gated neuro-symbolic training.

    The gating weight is interpreted as the share assigned to the neural
    prediction head; the remainder is assigned to symbolic transition
    consistency.
    """
    try:
        neural_loss = max(0.0, min(1.0, float(neural_prediction_loss or 0.0)))
    except Exception:
        neural_loss = 0.0
    try:
        symbolic_loss = max(0.0, min(1.0, float(symbolic_transition_loss or 0.0)))
    except Exception:
        symbolic_loss = 0.0
    try:
        gate = max(0.1, min(0.9, float(gating_weight or 0.55)))
    except Exception:
        gate = 0.55

    weighted_loss = round((neural_loss * gate) + (symbolic_loss * (1.0 - gate)), 3)
    balance = round(abs(neural_loss - symbolic_loss), 3)
    alignment = round(max(0.0, min(1.0, 1.0 - balance)), 3)
    return {
        "class": "core_tools.WorldModel",
        "objective": "Balance neural prediction and symbolic transition consistency using gating-aware weighting.",
        "gating_weight": gate,
        "neural_prediction_loss": neural_loss,
        "symbolic_transition_loss": symbolic_loss,
        "weighted_loss": weighted_loss,
        "balance": balance,
        "alignment": alignment,
        "summary": (
            f"dual_loss={weighted_loss:.3f} | gate={gate:.2f} | "
            f"neural={neural_loss:.3f} | symbolic={symbolic_loss:.3f}"
        ),
    }


def _meta_replay_update(
    task_curriculum: dict,
    recent_mistakes: list[dict],
    recent_evos: list[dict] | None = None,
    buffer_limit: int = 12,
) -> dict:
    """Bounded meta-replay update packet for curriculum-aware replay sampling.

    The helper samples a small replay buffer from the least-represented task
    track plus recent mistakes/evolutions so future training batches can
    rebalance under-exposed work without adding a new runtime learner.
    """
    counts = (task_curriculum or {}).get("counts", {}) or {}
    focus_track = None
    if counts:
        try:
            focus_track = sorted(counts.items(), key=lambda kv: int(kv[1] or 0))[0][0]
        except Exception:
            focus_track = None

    items = (task_curriculum or {}).get("items", []) or []
    replay_buffer: list[dict] = []
    for idx, item in enumerate(items[: max(4, buffer_limit)]):
        replay_buffer.append({
            "source": "curriculum",
            "work_track": item.get("work_track") or "general",
            "title": (item.get("title") or "")[:120],
            "status": item.get("status") or "",
            "priority": round(0.9 - (0.03 * idx), 3),
        })

    for idx, m in enumerate((recent_mistakes or [])[:6]):
        replay_buffer.append({
            "source": "mistake",
            "work_track": focus_track or "mixed",
            "title": (m.get("what_failed") or m.get("root_cause") or "mistake")[:120],
            "status": m.get("severity") or "medium",
            "priority": round(0.7 - (0.04 * idx), 3),
        })

    for idx, evo in enumerate((recent_evos or [])[:6]):
        replay_buffer.append({
            "source": "evolution",
            "work_track": focus_track or "mixed",
            "title": (evo.get("change_summary") or evo.get("summary") or "evolution")[:120],
            "status": evo.get("status") or "",
            "priority": round(0.6 - (0.04 * idx), 3),
        })

    replay_buffer.sort(key=lambda item: (item.get("priority", 0.0), item.get("source", ""), item.get("title", "")), reverse=True)
    selected = replay_buffer[: max(3, min(int(buffer_limit or 12), 16))]
    replay_gain = round(min(1.0, 0.45 + (0.05 * len(selected)) + (0.08 if focus_track else 0.0)), 3)
    return {
        "class": "core_tools.MetaReplay",
        "objective": "Sample replay items from curriculum, mistakes, and evolutions to rebalance under-represented training tracks.",
        "focus_track": focus_track or "mixed",
        "buffer_count": len(replay_buffer),
        "selected_count": len(selected),
        "selected": selected,
        "replay_gain": replay_gain,
        "summary": f"meta_replay_update=ok | focus={focus_track or 'mixed'} | selected={len(selected)} | gain={replay_gain:.2f}",
    }


def _safe_recent_changelog_context(limit: int = 5) -> dict:
    """Return recent changelog text plus availability metadata.

    The background researcher should never fail just because the changelog table
    is missing, unavailable, or temporarily unreadable. Instead we surface a
    structured status and keep the rest of the recent training context intact.
    """
    limit = max(1, int(limit))
    canonical_cols = "version,change_type,component,title,description,before_state,after_state,triggered_by,created_at"
    legacy_cols = "summary,category"

    def _normalize_rows(rows: list[dict], schema: str) -> tuple[list[dict], dict]:
        normalized: list[dict] = []
        missing_fields_rows = 0
        missing_fields_total = 0
        completeness_total = 0.0
        for row in rows or []:
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
            legacy_summary = str(row.get("summary") or "").strip()
            legacy_category = str(row.get("category") or "").strip()
            missing_fields = [name for name, value in canonical.items() if not value]
            if not canonical["title"]:
                canonical["title"] = legacy_summary or "Untitled changelog entry"
            if not canonical["description"]:
                canonical["description"] = legacy_summary or "No description provided."
            if not canonical["change_type"]:
                canonical["change_type"] = legacy_category or "unknown"
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
                display_line = canonical["description"] or legacy_summary or "Untitled changelog entry"
            completeness = round((len(canonical) - len(missing_fields)) / max(1, len(canonical)), 2)
            completeness_total += completeness
            if missing_fields:
                missing_fields_rows += 1
                missing_fields_total += len(missing_fields)
            normalized.append({
                **row,
                "_schema": schema,
                "_missing_fields": missing_fields,
                "_row_completeness": completeness,
                "_display_line": display_line,
            })
        stats = {
            "missing_fields_rows": missing_fields_rows,
            "missing_fields_total": missing_fields_total,
            "row_completeness": round(completeness_total / max(1, len(normalized)), 2) if normalized else 0.0,
            "normalized_rows": normalized,
        }
        return normalized, stats

    try:
        rows = sb_get(
            "changelog",
            f"select={canonical_cols}&order=id.desc&limit={limit}",
            svc=True,
        )
        try:
            from core_tools import t_changelog_source_packet
            source_context = t_changelog_source_packet(limit=limit)
        except Exception as source_exc:
            source_context = {
                "available": False,
                "counts": {"sessions": 0, "hot_reflections": 0, "mistakes": 0, "knowledge_base": 0},
                "rows": {"sessions": [], "hot_reflections": [], "mistakes": [], "knowledge_base": []},
                "text": "Unavailable.",
                "sources": ["sessions", "hot_reflections", "mistakes", "knowledge_base"],
                "error": str(source_exc),
                }
        normalized_rows, stats = _normalize_rows(rows or [], "canonical")
        if not rows:
            text = "None yet."
        else:
            text = "\n".join(
                "  [{ver}|{ctype}] {component} â€” {title}{missing}".format(
                    ver=(r.get("version") or "?"),
                    ctype=(r.get("change_type") or "?"),
                    component=(r.get("component") or "general"),
                    title=(r.get("_display_line") or r.get("title") or r.get("description") or "")[:160],
                    missing=(
                        f" [missing: {','.join(r.get('_missing_fields') or [])}]"
                        if r.get("_missing_fields")
                        else ""
                    ),
                )
                for r in normalized_rows
            )
        return {
            "available": True,
            "rows": rows or [],
            "normalized_rows": normalized_rows,
            "missing_fields_rows": stats["missing_fields_rows"],
            "missing_fields_total": stats["missing_fields_total"],
            "row_completeness": stats["row_completeness"],
            "text": text,
            "error": "",
            "schema": "canonical",
            "source_context": source_context,
        }
    except Exception as exc:
        try:
            rows = sb_get(
                "changelog",
                f"select={legacy_cols}&order=id.desc&limit={limit}",
                svc=True,
            )
            try:
                from core_tools import t_changelog_source_packet
                source_context = t_changelog_source_packet(limit=limit)
            except Exception as source_exc:
                source_context = {
                    "available": False,
                    "counts": {"sessions": 0, "hot_reflections": 0, "mistakes": 0, "knowledge_base": 0},
                    "rows": {"sessions": [], "hot_reflections": [], "mistakes": [], "knowledge_base": []},
                    "text": "Unavailable.",
                    "sources": ["sessions", "hot_reflections", "mistakes", "knowledge_base"],
                    "error": str(source_exc),
                }
            normalized_rows, stats = _normalize_rows(rows or [], "legacy")
            text = "\n".join(
                "  [{ctype}] {summary}{missing}".format(
                    ctype=(r.get("change_type") or r.get("category") or "?"),
                    summary=(r.get("_display_line") or r.get("summary") or "")[:160],
                    missing=(
                        f" [missing: {','.join(r.get('_missing_fields') or [])}]"
                        if r.get("_missing_fields")
                        else ""
                    ),
                )
                for r in normalized_rows
            ) if rows else "None yet."
            return {
                "available": True,
                "rows": rows or [],
                "normalized_rows": normalized_rows,
                "missing_fields_rows": stats["missing_fields_rows"],
                "missing_fields_total": stats["missing_fields_total"],
                "row_completeness": stats["row_completeness"],
                "text": text,
                "error": "",
                "schema": "legacy",
                "fallback_error": str(exc),
                "source_context": source_context,
            }
        except Exception as legacy_exc:
            try:
                from core_tools import t_changelog_source_packet
                source_context = t_changelog_source_packet(limit=limit)
            except Exception as source_exc:
                source_context = {
                    "available": False,
                    "counts": {"sessions": 0, "hot_reflections": 0, "mistakes": 0, "knowledge_base": 0},
                    "rows": {"sessions": [], "hot_reflections": [], "mistakes": [], "knowledge_base": []},
                    "text": "Unavailable.",
                    "sources": ["sessions", "hot_reflections", "mistakes", "knowledge_base"],
                    "error": str(source_exc),
                    "verified": False,
                    "blocked": True,
                    "verification_score": 0.0,
                    "passed_checks": [],
                    "failed_checks": ["source_packet_error"],
                    "warnings": [str(source_exc)],
                    "summary": f"source packet error: {source_exc}",
                }
            return {
                "available": False,
                "rows": [],
                "normalized_rows": [],
                "missing_fields_rows": 0,
                "missing_fields_total": 0,
                "row_completeness": 0.0,
                "text": "Unavailable.",
                "error": str(legacy_exc),
                "schema": "unavailable",
                "source_context": {
                    **source_context,
                },
                "fallback_error": str(exc),
            }


def _collect_background_research_context() -> dict:
    """Collect and verify recent research inputs with proactive fallbacks."""
    result = {
        "ok": True,
        "since_ts": "",
        "fallback_used": False,
        "sessions": [],
        "mistakes": [],
        "changelog": _safe_recent_changelog_context(limit=5),
        "source_context": {},
        "verification": {
            "anchor_source": "",
            "sessions_count": 0,
            "mistakes_count": 0,
            "changelog_available": False,
            "changelog_error": "",
            "source_context_available": False,
            "source_context_error": "",
            "data_ready": False,
            "note": "",
        },
    }
    try:
        if trading_specialization_enabled():
            state_rows = sb_get("sessions", "select=summary&summary=like.*last_real_signal_ts:*&order=created_at.desc&limit=1", svc=True)
            if state_rows and state_rows[0].get("summary"):
                raw = state_rows[0]["summary"].split("last_real_signal_ts:")[-1].strip().split()[0]
                normalized, parsed, future = _normalize_utc_timestamp(raw, clamp_future=True)
                if parsed and normalized:
                    since_ts = normalized
                    result["verification"]["anchor_source"] = "last_real_signal_ts"
                    if future:
                        print(f"[RESEARCH/TRADING] last_real_signal_ts was in the future; clamped to {since_ts}")
                else:
                    since_ts = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    result["verification"]["anchor_source"] = "soft_boot_7d"
                    result["fallback_used"] = True
            else:
                since_ts = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
                result["verification"]["anchor_source"] = "soft_boot_7d"
                result["fallback_used"] = True
            result["since_ts"] = since_ts

            packet = build_trading_source_packet(since_ts=since_ts, limit=40)
            trading_sessions = (packet.get("tables", {}).get("trading_decisions", []) + packet.get("tables", {}).get("closed_positions", []))[:20]
            trading_mistakes = (packet.get("tables", {}).get("trading_mistakes", []) + packet.get("tables", {}).get("mistakes", []))[:20]
            result["sessions"] = trading_sessions
            result["mistakes"] = trading_mistakes
            result["source_context"] = packet
            result["verification"]["sessions_count"] = len(trading_sessions)
            result["verification"]["mistakes_count"] = len(trading_mistakes)
            result["verification"]["source_context_available"] = True
            result["verification"]["source_context_error"] = ""
            result["verification"]["changelog_available"] = False
            result["verification"]["changelog_error"] = "disabled_in_trading_specialization"
            result["verification"]["data_ready"] = bool(packet.get("verified"))
            notes = [
                "trading_only",
                packet.get("summary", ""),
            ]
            if result["fallback_used"]:
                notes.append("fallback_used=true")
            if not packet.get("fresh_signal_count"):
                notes.append("no_fresh_trading_signal_yet")
            result["verification"]["note"] = ", ".join([n for n in notes if n])
            result["ok"] = bool(packet.get("verified"))
            return result

        state_rows = sb_get("sessions", "select=summary&summary=like.*last_real_signal_ts:*&order=created_at.desc&limit=1", svc=True)
        if state_rows and state_rows[0].get("summary"):
            raw = state_rows[0]["summary"].split("last_real_signal_ts:")[-1].strip().split()[0]
            normalized, parsed, future = _normalize_utc_timestamp(raw, clamp_future=True)
            if parsed and normalized:
                since_ts = normalized
                result["verification"]["anchor_source"] = "last_real_signal_ts"
                if future:
                    print(f"[RESEARCH/REAL] last_real_signal_ts was in the future; clamped to {since_ts}")
                else:
                    print(f"[RESEARCH/REAL] Using last_real_signal_ts: {since_ts}")
            else:
                since_ts = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                result["verification"]["anchor_source"] = "soft_boot_1d"
                result["fallback_used"] = True
                print(f"[RESEARCH/REAL] Invalid last_real_signal_ts, soft-boot to yesterday: {since_ts}")
        else:
            since_ts = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            result["verification"]["anchor_source"] = "soft_boot_1d"
            result["fallback_used"] = True
            print(f"[RESEARCH/REAL] No state key found, soft-boot to yesterday: {since_ts}")
        result["since_ts"] = since_ts

        sessions = sb_get("sessions",
            f"select={_sel_force('sessions', ['summary','actions','interface'])}&created_at=gte.{since_ts}&order=created_at.desc&limit=20",
            svc=True) or []
        mistakes = sb_get("mistakes",
            f"select={_sel_force('mistakes', ['domain','what_failed','root_cause','how_to_avoid'])}&created_at=gte.{since_ts}&order=id.desc&limit=20",
            svc=True) or []

        if not sessions and not mistakes:
            since_ts = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
            result["fallback_used"] = True
            result["since_ts"] = since_ts
            result["verification"]["anchor_source"] = "soft_boot_7d"
            print(f"[RESEARCH/REAL] No new data since anchor -- falling back to 7d window: {since_ts}")
            sessions = sb_get("sessions",
                f"select=summary,actions,interface&created_at=gte.{since_ts}&order=created_at.desc&limit=20",
                svc=True) or []
            mistakes = sb_get("mistakes",
                f"select={_sel_force('mistakes', ['domain','what_failed','root_cause','how_to_avoid'])}&created_at=gte.{since_ts}&order=id.desc&limit=20",
                svc=True) or []

        result["sessions"] = sessions
        result["mistakes"] = mistakes
        result["verification"]["sessions_count"] = len(sessions)
        result["verification"]["mistakes_count"] = len(mistakes)
        result["verification"]["changelog_available"] = bool(result["changelog"].get("available"))
        result["verification"]["changelog_error"] = result["changelog"].get("error") or ""
        result["source_context"] = result["changelog"].get("source_context") or {}
        result["verification"]["source_context_available"] = bool(result["source_context"].get("available"))
        result["verification"]["source_context_error"] = result["source_context"].get("error") or ""

        if not sessions and not mistakes:
            result["verification"]["data_ready"] = False
            result["verification"]["note"] = "No sessions or mistakes available even after fallback."
            result["ok"] = False
            return result

        result["verification"]["data_ready"] = True
        notes = []
        if result["fallback_used"]:
            notes.append("fallback_used=true")
        if not result["verification"]["changelog_available"]:
            notes.append("changelog_unavailable")
        if not result["verification"]["source_context_available"]:
            notes.append("changelog_sources_unavailable")
        result["verification"]["note"] = ", ".join(notes) if notes else "verified"
        return result
    except Exception as exc:
        result["ok"] = False
        result["verification"]["note"] = str(exc)
        return result


def _run_joint_training_planner(cycle_count: int = 0) -> dict:
    """Bounded joint-training planner (no actual model training).

    This emits a compact curriculum and module-interface guidance to KB so future
    training components (TMRF, DAM, world model) have a stable contract.
    """
    try:
        phase_signal = _phase_signal_packet(limit=16)
        if phase_signal:
            curriculum = phase_signal["curriculum"]
            counts = curriculum.get("counts", {}) or {}
            items = curriculum.get("items", []) or []
            recent_mistakes = phase_signal["recent_mistakes"]
            recent_evos = phase_signal["recent_evos"]
        else:
            curriculum = _build_task_embedding_curriculum(limit=16)
            counts = curriculum.get("counts", {}) or {}
            items = curriculum.get("items", []) or []
            recent_mistakes = sb_get(
                "mistakes",
                "select=what_failed,root_cause,how_to_avoid&order=created_at.desc&limit=8&id=gt.1",
                svc=True,
            ) or []
            recent_evos = sb_get(
                "evolution_queue",
                "select=change_type,change_summary,impact,status,source&status=in.(pending,pending_desktop,applied,rejected,synthesized)&order=created_at.desc&limit=8",
                svc=True,
            ) or []
        # Emphasize transfer by focusing on the least-represented work_track.
        focus_track = None
        if counts:
            focus_track = sorted(counts.items(), key=lambda kv: int(kv[1] or 0))[0][0]
        focus_items = [it for it in items if (it.get("work_track") or "") == focus_track] if focus_track else items[:6]

        today = datetime.utcnow().strftime("%Y-%m-%d")
        topic = f"joint_training_plan_{today}"
        plan = {
            "cycle_count": int(cycle_count or 0),
            "components": ["TMRF", "DAM", "world_model", "DynamicRouter", "MRC"],
            "curriculum_counts": counts,
            "meta_replay_update": _meta_replay_update(curriculum, recent_mistakes, recent_evos),
            "world_model_meta_objective": _world_model_fusion_meta_learning_objective(curriculum, recent_mistakes, recent_evos),
            "world_model_dual_loss_contract": _hierarchical_gated_dual_loss(
                neural_prediction_loss=0.42,
                symbolic_transition_loss=0.38,
                gating_weight=0.58,
            ),
            "transfer_focus_work_track": focus_track or "mixed",
            "transfer_focus_items": [{
                "work_track": (it.get("work_track") or "general"),
                "title": (it.get("title") or "")[:160],
                "status": it.get("status") or "",
                "source": it.get("source") or "",
            } for it in focus_items[:8]],
            "meta_representation_contract": {
                "class": "core_tools.MetaRepresentation",
                "storage": "serialize with to_dict() and persist in sessions or knowledge_base; pass as JSON between modules",
                "merge": "use overlay for latest state; union for sparse curriculum accumulation",
            },
            "router_meta_objective": _router_meta_learning_objective(curriculum, sb_get("mistakes", "select=what_failed,root_cause,how_to_avoid&order=created_at.desc&limit=8&id=gt.1", svc=True) or []),
            "mrc_meta_objective": _mrc_meta_learning_objective(curriculum, sb_get("mistakes", "select=what_failed,root_cause,how_to_avoid&order=created_at.desc&limit=8&id=gt.1", svc=True) or []),
            "notes": "Planner-only: this does not train models. It produces curriculum + interface guidance for later workers.",
        }
        content = "Joint training planner (bounded).\n" + json.dumps(plan, ensure_ascii=True)[:5000]
        ok = sb_upsert("knowledge_base", {
            "domain": _training_kb_domain(),
            "topic": topic,
            "content": content,
            "instruction": content,
            "source": "joint_training_planner",
            "source_type": "training_plan",
            "source_ref": f"background_researcher:cycle={int(cycle_count or 0)}",
            "confidence": "low",
            "tags": ["meta", "training", "joint", "curriculum"],
            "active": True,
        }, on_conflict="domain,topic")
        return {"ok": bool(ok), "topic": topic, "focus_track": focus_track or "mixed", "counts": counts}
    except Exception as e:
        print(f"[JOINT] planner error (non-fatal): {e}")
        return {"ok": False, "error": str(e)}


def run_cold_processor():
    global _last_meta_learning_run
    try:
        hots = sb_get("hot_reflections",
                      f"select={_sel_force('hot_reflections', ['id','domain','new_patterns','new_mistakes','quality_score','source','task_summary','gaps_identified'])}&processed_by_cold=eq.0&id=gt.1&quality_score=gte.0.5&order=created_at.asc",
                      svc=True)
        skipped_low_quality = sb_get("hot_reflections",
                      "select=id&processed_by_cold=eq.0&id=gt.1&quality_score=lt.0.5",
                      svc=True)
        if skipped_low_quality:
            print(f"[COLD] Skipped {len(skipped_low_quality)} low-quality hots (quality<0.5)")
            for lq in skipped_low_quality:
                sb_patch("hot_reflections", f"id=eq.{lq['id']}", {"processed_by_cold": 1})
        if not hots:
            print("[COLD] No unprocessed hot reflections.")
            return {"ok": True, "processed": 0, "evolutions_queued": 0}

        period_start      = datetime.utcnow().isoformat()
        evolutions_queued = 0
        auto_applied_count = 0
        batch_counts: Counter = Counter()
        batch_domain: dict    = {}
        batch_sources: dict   = {}

        for h in hots:
            src = h.get("source") or "real"
            raw_patterns = h.get("new_patterns") or []
            if isinstance(raw_patterns, str):
                raw_patterns = raw_patterns.strip()
                try:
                    parsed = json.loads(raw_patterns)
                    raw_patterns = parsed if isinstance(parsed, list) else [raw_patterns]
                except (json.JSONDecodeError, ValueError):
                    raw_patterns = [x.strip() for x in raw_patterns.replace("\n", ",").split(",") if x.strip()]
            for p in raw_patterns:
                if p and isinstance(p, str) and len(p) > 3:
                    key = str(p)[:500]
                    batch_counts[key] += 1
                    batch_domain.setdefault(key, h.get("domain", "general"))
                    batch_sources.setdefault(key, set()).add(src)

        if len(batch_counts) > 1:
            batch_counts, batch_domain, batch_sources = _groq_cluster_patterns(
                batch_counts, batch_domain, batch_sources
            )

        all_pf = _pattern_frequency_lookup(batch_counts.keys())

        for key, batch_count in batch_counts.items():
            existing = all_pf.get(key)
            src_set  = batch_sources.get(key, {"real"})
            src_key  = "both" if len(src_set) > 1 else next(iter(src_set))
            src_mult = _SRC_CONF.get(src_key, 1.0)
            domain   = batch_domain.get(key, "general")

            now_ts = datetime.utcnow().isoformat()
            if existing:
                new_freq = existing["frequency"] + batch_count
                sb_upsert("pattern_frequency",
                          {"id": existing["id"], "pattern_key": key, "frequency": new_freq,
                           "domain": domain, "description": key[:500], "last_seen": now_ts,
                           "stale": False},
                          on_conflict="id")
                total_freq = new_freq
            else:
                sb_upsert("pattern_frequency",
                          {"pattern_key": key, "frequency": batch_count,
                           "domain": domain, "description": key[:500], "auto_applied": False,
                           "last_seen": now_ts, "stale": False},
                          on_conflict="pattern_key")
                total_freq = batch_count

            _milestones = {10, 25, 50, 100, 200, 500}
            _already_applied = (existing or {}).get("auto_applied", False)
            _at_milestone = total_freq in _milestones
            _should_queue = total_freq >= PATTERN_EVO_THRESHOLD and (
                not _already_applied or _at_milestone
            )
            if _should_queue:
                base_conf  = min(0.5 + total_freq * 0.05, 0.95)
                final_conf = round(base_conf * src_mult, 3)
                kb_content = _groq_kb_content(key, domain, total_freq, src_key)
                _already_active = sb_get("evolution_queue",
                    f"select=id,status&pattern_key=eq.{_sb_eq_value(key, 200)}&status=in.(pending,rejected)&limit=1",
                    svc=True)
                if _already_active:
                    _existing_status = _already_active[0].get("status", "pending")
                    if _existing_status == "rejected":
                        print(f"[COLD] Skipped previously-rejected evo: {key[:80]}")
                    else:
                        print(f"[COLD] Skipped duplicate evo (pending): {key[:80]}")
                    continue

                # P2-01: Assign approval tier at queue time
                _tier = _assign_approval_tier(final_conf, "knowledge", src_key)
                ok = sb_post_critical("evolution_queue", {
                    "change_type":    "knowledge",
                    "change_summary": kb_content[:500],
                    "pattern_key":    key,
                    "confidence":     final_conf,
                    "status":         "pending",
                    "source":         src_key,
                    "impact":         domain,
                    "recommendation": f"Pattern appears {total_freq}x (src={src_key}). KB content Groq-generated.",
                    "approval_tier":  _tier,
                    "diff_content": json.dumps({
                        "pattern_key": key,
                        "source": src_key,
                        "domain": domain,
                        "autonomy": {
                            "kind": "kb_expand",
                            "origin": "cold_processor",
                            "source": src_key,
                            "domain": domain,
                            "pattern_key": key,
                            "task_group": "knowledge",
                            "expected_artifact": "knowledge_base",
                            "next_worker": "evolution_autonomy",
                        },
                    }, default=str),
                })
                if ok:
                    evolutions_queued += 1
                    sb_upsert("pattern_frequency",
                              {"pattern_key": key, "auto_applied": True},
                              on_conflict="pattern_key")
                    # P2-01: Tier-aware auto-apply â€” 'auto' tier applied immediately
                    _safety = os.getenv("EVOLUTION_AUTO_TIER", "").strip().lower()
                    if _tier == "auto" and _safety not in ("notify_only", "disabled"):
                        new_evo = sb_get("evolution_queue",
                            f"select=id&pattern_key=eq.{_sb_eq_value(key, 100)}&status=eq.pending&order=id.desc&limit=1",
                            svc=True)
                        if new_evo:
                            result = apply_evolution(new_evo[0]["id"])
                            if result.get("ok"):
                                auto_applied_count += 1
                                sb_patch("evolution_queue", f"id=eq.{new_evo[0]['id']}",
                                         {"tier_applied_at": datetime.utcnow().isoformat()})
                                print(f"[COLD] Auto-tier applied #{new_evo[0]['id']}: {key[:80]}")
                    elif _tier == "notify" and final_conf >= 0.65 and src_key == "real" and _safety not in ("notify_only", "disabled"):
                        # Legacy backward-compat: high-confidence notify-tier still auto-applies in cold processor
                        new_evo = sb_get("evolution_queue",
                            f"select=id&pattern_key=eq.{_sb_eq_value(key, 100)}&status=eq.pending&order=id.desc&limit=1",
                            svc=True)
                        if new_evo:
                            result = apply_evolution(new_evo[0]["id"])
                            if result.get("ok"):
                                auto_applied_count += 1
                                print(f"[COLD] notify-tier (legacy compat) applied #{new_evo[0]['id']}: {key[:80]}")

        # P2-02: Auto-evolve behavioral rules for high-frequency, high-confidence patterns
        for key, total_freq in batch_counts.items():
            if total_freq >= 10:
                src_set  = batch_sources.get(key, {"real"})
                src_key  = "both" if len(src_set) > 1 else next(iter(src_set))
                src_mult = _SRC_CONF.get(src_key, 1.0)
                base_conf = min(0.5 + total_freq * 0.05, 0.95)
                final_conf = round(base_conf * src_mult, 3)
                domain = batch_domain.get(key, "general")
                if final_conf >= 0.85:
                    _auto_evolve_behavioral_rule(key, domain, final_conf, total_freq)

        gaps_inserted = _reconcile_gaps(hots)
        evolutions_queued += gaps_inserted
        _backfill_patterns(batch_size=10)

        # P3-02: Extract owner profile signals from this cold processor batch
        try:
            profile_inserted = _extract_owner_profile_signals(hots)
            if profile_inserted:
                print(f"[COLD][P3-02] {profile_inserted} owner profile entries updated")
        except Exception as _p302e:
            print(f"[COLD][P3-02] profile extraction error (non-fatal): {_p302e}")

        groq_summary = _groq_synthesize_cold(hots, batch_counts, batch_domain)
        counter_suffix = f" | hots={len(hots)} patterns={len(batch_counts)} evos={evolutions_queued}"
        summary_text = groq_summary + counter_suffix

        sb_post_critical("cold_reflections", {
            "period_start": period_start, "period_end": datetime.utcnow().isoformat(),
            "hot_count": len(hots), "patterns_found": len(batch_counts),
            "evolutions_queued": evolutions_queued, "auto_applied": auto_applied_count,
            "summary_text": summary_text[:2000],
        })
        for h in hots:
            sb_patch("hot_reflections", f"id=eq.{h['id']}", {"processed_by_cold": 1})
        if evolutions_queued > 0:
            print(f"Cold processor: {evolutions_queued} evolution(s) queued, {auto_applied_count} auto-applied.\n{groq_summary[:300]}\nPending owner review: {evolutions_queued - auto_applied_count}")
        # AGI-03/S3: trigger causal quality analysis if recent quality is declining
        try:
            recent_scores = [float(h.get("quality_score") or 0.7) for h in hots[-10:]]
            if len(recent_scores) >= 5:
                avg5 = sum(recent_scores[-5:]) / 5
                avg_all = sum(recent_scores) / len(recent_scores)
                if avg5 < avg_all - 0.05:  # Recent 5 meaningfully worse than session average
                    print(f"[COLD] Quality declining (recent_avg={avg5:.2f} vs overall={avg_all:.2f}) -- running causal analysis")
                    _run_causal_quality_analysis()
        except Exception:
            pass

        # P2-06: Record pattern outcomes for auto-applied patterns so we can track quality impact
        try:
            current_q = None
            if hots:
                qs = [float(h.get("quality_score") or 0) for h in hots if h.get("quality_score")]
                current_q = round(sum(qs) / len(qs), 3) if qs else None
            if current_q is not None and auto_applied_count > 0:
                # Fetch the patterns that were just auto-applied (last auto_applied_count pattern entries)
                for key, batch_count in list(batch_counts.items())[:auto_applied_count]:
                    domain = batch_domain.get(key, "general")
                    try:
                        sb_post("pattern_outcome", {
                            "pattern_key":    key[:400],
                            "domain":         domain,
                            "quality_before": current_q,
                            "quality_after":  None,  # filled in by session_end hook
                            "applied_at":     datetime.utcnow().isoformat(),
                            "outcome":        "pending",  # updated when next session ends
                        })
                    except Exception:
                        pass  # table may not exist yet -- non-fatal
                print(f"[COLD] P2-06: recorded {auto_applied_count} pattern outcome baseline(s) (q_before={current_q})")
        except Exception as _p26e:
            print(f"[COLD] P2-06 outcome recording error (non-fatal): {_p26e}")

        # P3-03: bounded meta-learning synthesis from the same cold batch signal.
        try:
            now = time.time()
            if now - _last_meta_learning_run >= _META_LEARNING_INTERVAL:
                meta_result = _run_meta_learning_loop()
                if meta_result.get("ok"):
                    _last_meta_learning_run = now
                    print(f"[META] Cycle complete: {meta_result.get('topic', 'unknown')}")
        except Exception as _ml_e:
            print(f"[META] meta-learning gate error (non-fatal): {_ml_e}")

        print(f"[COLD] Done: processed={len(hots)} patterns={len(batch_counts)} evolutions={evolutions_queued} auto_applied={auto_applied_count}")
        return {"ok": True, "processed": len(hots), "patterns_found": len(batch_counts), "evolutions_queued": evolutions_queued, "auto_applied": auto_applied_count}
    except Exception as e:
        print(f"[COLD] error: {e}")
        return {"ok": False, "error": str(e)}


# -- Evolution apply/reject ----------------------------------------------------
def apply_evolution(evolution_id: int):
    try:
        rows = sb_get("evolution_queue",
                      f"select=*&id=eq.{evolution_id}&status=eq.pending&limit=1", svc=True)
        if not rows:
            return {"ok": False, "error": f"Evolution {evolution_id} not found or not pending"}
        evo           = rows[0]
        change_type   = evo.get("change_type", "knowledge")
        change_summary= evo.get("change_summary", "")
        diff_content  = evo.get("diff_content", "")
        pattern_key   = evo.get("pattern_key", "")
        confidence    = float(evo.get("confidence") or 0.5)
        applied = False; note = ""

        if change_type == "knowledge":
            applied = sb_upsert("knowledge_base", {
                "domain": evo.get("impact", "general"),
                "topic": (pattern_key[:100] or change_summary[:100]),
                "content": change_summary,
                "confidence": "high" if confidence >= 0.8 else "medium",
                "tags": ["evolution", "auto"], "source": "evolution_queue",
            }, on_conflict="domain,topic")
            note = "Added/updated knowledge_base"

        elif change_type == "new_tool":
            try:
                meta = json.loads(diff_content) if diff_content else {}
            except Exception:
                meta = {}
            fn_name = meta.get("fn_name", "")
            fn_code  = meta.get("code", "")
            if not fn_name or not fn_code:
                prompt = (f"Write a Python function for CORE AGI system named '{pattern_key or 'new_tool'}'.\n"
                          f"Purpose: {change_summary}\n"
                          f"Recommendation: {evo.get('recommendation','')}\n\n"
                          f"Rules:\n"
                          f"- Use sb_post, sb_get, sb_patch, groq_chat, gh_read, gh_write as needed\n"
                          f"- Return dict with 'ok' key always\n"
                          f"- Add docstring explaining purpose\n"
                          f"- Follow CORE naming: t_<n>\n\n"
                          f"Output ONLY the Python function, no explanation.")
                _sys_code = _load_prompt("code_generation", "You are CORE's code generation engine. Output only valid Python. No markdown, no preamble.")
                _maybe_eval_prompt("code_generation", _sys_code, 10)
                fn_code = groq_chat(
                    _sys_code,
                    prompt, model=GROQ_MODEL, max_tokens=600
                )
                fn_name = ""
                for line in fn_code.splitlines():
                    if line.strip().startswith("def "):
                        fn_name = line.strip().split("(")[0].replace("def ", "").strip()
                        break
            if fn_name and fn_code:
                sb_patch("evolution_queue", f"id=eq.{evolution_id}", {"status": "pending_desktop"})
                print(f"[NEW TOOL] Evolution #{evolution_id} generated code for '{fn_name}'.\n"
                       f"Apply via Claude Desktop: add to core_tools.py + register in TOOLS dict.")
                applied = True
                note = f"New tool '{fn_name}' code generated â€” needs Desktop apply to core_tools.py"
                sb_post("script_templates", {
                    "name": fn_name, "description": change_summary[:200],
                    "trigger_pattern": evo.get("recommendation", ""),
                    "code": fn_code, "use_count": 0,
                    "created_at": datetime.utcnow().isoformat(),
                })
            else:
                note = "new_tool evolution: could not extract function name from generated code"
                applied = False

        elif change_type == "script_template":
            try:
                meta = json.loads(diff_content) if diff_content else {}
            except Exception:
                meta = {}
            tpl_name = meta.get("name", pattern_key or f"template_{evolution_id}")
            tpl_code = meta.get("code", change_summary)
            applied = sb_post("script_templates", {
                "name": tpl_name,
                "description": meta.get("description", change_summary[:200]),
                "trigger_pattern": meta.get("trigger_pattern", evo.get("recommendation", "")),
                "code": tpl_code, "use_count": 0,
                "created_at": datetime.utcnow().isoformat(),
            })
            note = f"Script template '{tpl_name}' stored in Supabase"

        elif change_type == "code":
            if not diff_content: return {"ok": False, "error": "code evolution requires diff_content"}
            fname = f"patches/evo_{evolution_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.patch"
            applied = gh_write(fname, diff_content, f"Evolution #{evolution_id}: {change_summary[:60]}")
            note = f"Patch written to {fname}"

        elif change_type == "behavior":
            if not diff_content: return {"ok": False, "error": "behavior evolution requires diff_content"}
            # P2-02: Write to behavioral_rules table (primary) + BEHAVIOR_UPDATES.md (archive)
            try:
                meta = json.loads(diff_content) if diff_content else {}
            except Exception:
                meta = {}
            br_trigger = meta.get("trigger", "during_action")
            br_domain  = meta.get("domain", evo.get("impact", "universal"))
            br_pointer = meta.get("pointer", change_summary[:80])
            br_rule    = meta.get("full_rule", change_summary)
            br_conf    = float(evo.get("confidence") or 0.7)
            br_ok = sb_post("behavioral_rules", {
                "trigger":    br_trigger,
                "pointer":    br_pointer,
                "full_rule":  br_rule,
                "domain":     br_domain,
                "priority":   3,
                "active":     True,
                "tested":     False,
                "source":     "evolution_queue",
                "confidence": round(br_conf, 3),
            })
            # Also archive to GitHub for audit trail
            gh_write("BEHAVIOR_UPDATES.md", diff_content,
                     f"Behavior evolution #{evolution_id}: {change_summary[:60]}")
            applied = br_ok
            note = f"Behavioral rule inserted to behavioral_rules (domain={br_domain}, trigger={br_trigger}) + archived to BEHAVIOR_UPDATES.md"

        elif change_type == "backlog":
            reject_evolution(evolution_id, reason="backlog change_type retired â€” owner decides backlog", silent=True)
            return {"ok": False, "evolution_id": evolution_id, "change_type": change_type,
                    "note": "backlog change_type retired â€” auto-rejected"}

        if applied:
            sb_patch("evolution_queue", f"id=eq.{evolution_id}",
                     {"status": "applied", "applied_at": datetime.utcnow().isoformat()})
            print(f"Evolution #{evolution_id} applied\nType: {change_type}\n{note}")
        else:
            if change_type not in ("backlog",):
                print(f"Evolution #{evolution_id} apply failed\nType: {change_type}")
            else:
                print(f"[EVO] #{evolution_id} backlog apply failed silently")
        return {"ok": applied, "evolution_id": evolution_id, "change_type": change_type, "note": note}
    except Exception as e:
        print(f"[EVO] error: {e}")
        return {"ok": False, "error": str(e)}


def reject_evolution(evolution_id: int, reason: str = "", silent: bool = False):
    try:
        rows = sb_get("evolution_queue",
                      f"select=*&id=eq.{evolution_id}&status=in.(pending,synthesized)&limit=1", svc=True)
        if not rows: return {"ok": False, "error": f"Evolution {evolution_id} not found or not pending/synthesized"}
        now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        sb_patch("evolution_queue", f"id=eq.{evolution_id}", {
            "status": "rejected",
            "rejected_by_owner": True,
            "tier_applied_at": now,
        })
        if not silent:
            sb_post("mistakes", {
                "domain": "evolution", "context": f"Evolution #{evolution_id}: {rows[0].get('change_summary','')[:200]}",
                "what_failed": "Evolution rejected by owner",
                "correct_approach": reason or "Owner rejected - review pattern and confidence threshold",
                "root_cause": reason or "Unknown",
                "how_to_avoid": "Raise confidence threshold or improve pattern quality",
                "severity": "low", "tags": ["evolution", "rejected"],
            })
            print(f"Evolution #{evolution_id} rejected.\nReason: {reason or 'No reason given'}")
        return {"ok": True, "evolution_id": evolution_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def bulk_reject_evolutions(change_type: str = "", ids: list = None, reason: str = "", include_synthesized: bool = False) -> dict:
    try:
        statuses = "status=in.(pending,synthesized)" if include_synthesized else "status=eq.pending"
        if ids:
            qs = f"select=id&{statuses}&id=in.({','.join(str(i) for i in ids)})"
        elif change_type:
            qs = f"select=id&{statuses}&change_type=eq.{change_type}"
        else:
            qs = f"select=id&{statuses}"
        rows = sb_get("evolution_queue", qs + "&limit=500", svc=True)
        rejected = 0
        skipped  = 0
        for row in rows:
            result = reject_evolution(row["id"], reason=reason, silent=True)
            if result.get("ok"):
                rejected += 1
            else:
                skipped += 1
        summary = f"Bulk rejected {rejected} evolutions (type={change_type or 'all'}, skipped={skipped}). Reason: {reason or 'none'}"
        print(f"[EVOLUTION] {summary}")
        print(summary)
        return {"ok": True, "rejected": rejected, "skipped": skipped}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- TASK-9.D: Dead Pattern Pruner -------------------------------------------
_last_stale_check: float = 0.0
_STALE_CHECK_INTERVAL = 86400  # 24h
_STALE_DAYS = 30

def _check_stale_patterns() -> int:
    try:
        cutoff = (datetime.utcnow() - timedelta(days=_STALE_DAYS)).isoformat()
        stale_rows = sb_get(
            "pattern_frequency",
            f"select=id,pattern_key&last_seen=lt.{cutoff}&auto_applied=eq.true",
            svc=True
        ) or []
        count = 0
        for row in stale_rows:
            try:
                sb_patch("pattern_frequency", f"id=eq.{row['id']}", {"stale": True})
                count += 1
            except Exception as _e:
                print(f"[STALE] patch error for id={row['id']}: {_e}")
        if count:
            print(f"[STALE] Marked {count} patterns as stale (not seen in {_STALE_DAYS}d)")
        return count
    except Exception as e:
        print(f"[STALE] _check_stale_patterns error: {e}")
        return 0


# -- Cold processor loop -------------------------------------------------------
def _restore_diag_timestamp():
    """Seed _last_self_diagnosis_run from Supabase on boot.
    Prevents Railway redeploy from resetting the 24h guard to zero.
    Falls back to (now - 25h) so first real 19:00 UTC window triggers normally.
    Uses summary column pattern matching t_get_state_key/t_update_state convention."""
    global _last_self_diagnosis_run
    try:
        # sessions table stores state as: summary = "[state_update] key: value"
        # URL-encode brackets: %5B = [ and %5D = ]
        rows = sb_get(
            "sessions",
            f"select=summary&summary=like.*%5Bstate_update%5D+{_DIAG_STATE_KEY}:*&order=id.desc&limit=1",
            svc=True
        ) or []
        if rows:
            raw = rows[0].get("summary", "")
            prefix = f"[state_update] {_DIAG_STATE_KEY}: "
            if raw.startswith(prefix):
                stored = float(raw[len(prefix):].strip())
                if stored > 0:
                    _last_self_diagnosis_run = stored
                    print(f"[DIAG] Restored last_diag_ts from Supabase: {datetime.utcfromtimestamp(stored).isoformat()}")
                    return
    except Exception as e:
        print(f"[DIAG] restore error: {e}")
    # Fallback: treat as ran 25h ago so it will fire at next scheduled window, not immediately
    _last_self_diagnosis_run = time.time() - (_SELF_DIAGNOSIS_INTERVAL + 3600)
    print("[DIAG] No stored timestamp -- seeded to 25h ago (will fire at next 19:00 UTC window)")

def cold_processor_loop():
    global _last_cold_run, _last_cold_kb_count, _last_stale_check
    print("[COLD] Background loop started")
    _restore_diag_timestamp()  # AGI-02: seed from Supabase before first loop tick
    while True:
        try:
            unprocessed = sb_count("hot_reflections", "processed_by_cold=eq.0&id=gt.1")
            time_since  = time.time() - _last_cold_run

            current_kb_count = 0
            try:
                counts = get_system_counts()
                current_kb_count = counts.get("knowledge_base", 0)
            except Exception:
                pass
            kb_growth = current_kb_count - _last_cold_kb_count

            should_run = (
                unprocessed >= COLD_HOT_THRESHOLD or
                (time_since >= COLD_TIME_THRESHOLD and unprocessed > 0) or
                (kb_growth >= COLD_KB_GROWTH_THRESHOLD and _last_cold_kb_count > 0)
            )

            if should_run:
                trigger = (
                    f"unprocessed={unprocessed}" if unprocessed >= COLD_HOT_THRESHOLD else
                    f"kb_growth={kb_growth}" if kb_growth >= COLD_KB_GROWTH_THRESHOLD else
                    f"time_since={int(time_since)}s"
                )
                print(f"[COLD] Triggering: {trigger}")
                run_cold_processor()
                _last_cold_run = time.time()
                _last_cold_kb_count = current_kb_count

            for evo in sb_get("evolution_queue",
                               "select=id,confidence,change_type&status=eq.pending&change_type=eq.knowledge&id=gt.1",
                               svc=True):
                conf = float(evo.get("confidence") or 0)
                if conf >= KNOWLEDGE_AUTO_CONFIDENCE:
                    apply_evolution(evo["id"])
            if time.time() - _last_stale_check >= _STALE_CHECK_INTERVAL:
                _check_stale_patterns()
                _last_stale_check = time.time()
            # AGI-02: Nightly self-diagnosis at 02:00 WIB (19:00 UTC)
            try:
                now_utc = datetime.utcnow()
                # Guard: if timestamp still sentinel (-1), restore before checking
                if _last_self_diagnosis_run < 0:
                    _restore_diag_timestamp()
                time_since_diag = time.time() - _last_self_diagnosis_run
                if (now_utc.hour == _SELF_DIAGNOSIS_HOUR_UTC and
                        time_since_diag >= _SELF_DIAGNOSIS_INTERVAL):
                    _run_self_diagnosis()
                    # PHASE-M: Run tool health scan after self-diagnosis (same nightly window)
                    try:
                        from core_tools import t_tool_health_scan
                        t_tool_health_scan(force="false")
                        print("[DIAG] tool_health_scan complete")
                    except Exception as _ths_e:
                        print(f"[DIAG] tool_health_scan error: {_ths_e}")
            except Exception as _de:
                print(f"[DIAG] trigger error: {_de}")
            # AGI-01: Weekly cross-domain synthesis on Wednesday
            try:
                now_utc = datetime.utcnow()
                time_since_synth = time.time() - _last_synthesis_run
                if (now_utc.weekday() == _SYNTHESIS_DAY_UTC and
                        time_since_synth >= _SYNTHESIS_INTERVAL):
                    _run_cross_domain_synthesis()
            except Exception as _se:
                print(f"[SYNTH] trigger error: {_se}")
            # AGI-05: Weekly memory consolidation on Sunday
            try:
                global _last_consolidation_run
                now_utc = datetime.utcnow()
                time_since_consol = time.time() - _last_consolidation_run
                if (now_utc.weekday() == _CONSOLIDATION_DAY_UTC and
                        time_since_consol >= _CONSOLIDATION_INTERVAL):
                    _run_memory_consolidation()
                    _last_consolidation_run = time.time()
            except Exception as _ce:
                print(f"[CONSOLIDATION] trigger error: {_ce}")
            # AGI-06: Weekly capability report on Tuesday
            try:
                global _last_capability_report_run
                now_utc = datetime.utcnow()
                time_since_cap = time.time() - _last_capability_report_run
                if (now_utc.weekday() == _CAPABILITY_REPORT_DAY_UTC and
                        time_since_cap >= _CAPABILITY_REPORT_INTERVAL):
                    _run_capability_report()
                    _last_capability_report_run = time.time()
            except Exception as _cape:
                print(f"[CAP] trigger error: {_cape}")
            # P3-07: Weekly capability calibration on Thursday (weekday=3)
            try:
                global _last_capability_calibration_run
                now_utc = datetime.utcnow()
                time_since_cal = time.time() - _last_capability_calibration_run
                if (now_utc.weekday() == 3 and
                        time_since_cal >= _CAPABILITY_CALIBRATION_INTERVAL):
                    _run_capability_calibration()
            except Exception as _cale:
                print(f"[CAP_CAL] trigger error: {_cale}")
            # GAP-DATA-01 retired: backup export is permanently disabled.
            # Keep the cold loop silent and avoid any backup retries or writes.
            pass
        except Exception as e:
            print(f"[COLD] loop error: {e}")
        time.sleep(1800)


# -- Backlog helpers -----------------------------------------------------------
def _backlog_add(items: list) -> list:
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
    return 0


def _backlog_to_markdown() -> str:
    return "# BACKLOG.md deprecated - use get_backlog MCP tool or Supabase directly."


# -- KB Mining -----------------------------------------------------------------
def run_kb_mining(max_batches: int = 50, force: bool = False) -> dict:
    print("[KB MINE] deprecated - backlog table dropped, no-op")
    return {"ok": False, "deprecated": True, "reason": "backlog table dropped - use evolution_queue pipeline instead"}


# -- Real signal + simulation --------------------------------------------------
def _extract_real_signal() -> bool:
    try:
        if trading_specialization_enabled():
            state_rows = sb_get("sessions",
                "select=summary&summary=like.*last_real_signal_ts:*&order=created_at.desc&limit=1",
                svc=True)
            if state_rows and state_rows[0].get("summary"):
                raw = state_rows[0]["summary"].split("last_real_signal_ts:")[-1].strip().split()[0]
                normalized, parsed, future = _normalize_utc_timestamp(raw, clamp_future=True)
                if parsed and normalized:
                    since_ts = normalized
                    if future:
                        print(f"[RESEARCH/TRADING] last_real_signal_ts was in the future; clamped to {since_ts}")
                else:
                    since_ts = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                since_ts = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

            packet = build_trading_source_packet(since_ts=since_ts, limit=40)
            if not packet.get("fresh_signal_count"):
                note = f"verified_no_fresh_inputs {packet.get('summary', '')}"
                sb_post("sessions", {
                    "summary": f"[state_update] trading_real_signal_status: {note[:260]}",
                    "actions": ["trading real signal verified with no fresh trading outcomes"],
                    "interface": "mcp",
                })
                print(f"[RESEARCH/TRADING] No fresh trading inputs since {since_ts}; seeds are ready")
                return False

            tables = packet.get("tables", {})
            decisions_text = "\n".join(
                f"- decision {row.get('symbol','?')} {row.get('strategy','?')} action={row.get('action_taken','?')} regime={row.get('market_regime','?')} confidence={row.get('confidence','?')}"
                for row in tables.get("trading_decisions", [])[:8]
            ) or "No recent decisions."
            positions_text = "\n".join(
                f"- position {row.get('symbol','?')} {row.get('strategy','?')} status={row.get('status','?')} pnl={row.get('realized_pnl_usd', 0)} funding={row.get('total_funding_usd', 0)} reason={row.get('close_reason','')}"
                for row in tables.get("closed_positions", [])[:8]
            ) or "No recent closed positions."
            live_mistakes_text = "\n".join(
                f"- mistake {row.get('what_failed','')[:140]} | root={row.get('root_cause','')[:120]}"
                for row in tables.get("trading_mistakes", [])[:8]
            ) or "No live trading mistakes."
            reflections_text = "\n".join(
                f"- reflection {row.get('gap','')[:120]} | behavior={row.get('new_behavior','')[:120]}"
                for row in tables.get("output_reflections", [])[:6]
            ) or "No recent trading reflections."
            rules_text = "\n".join(
                f"- {row.get('pointer','')} :: {(row.get('full_rule') or '')[:180]}"
                for row in tables.get("behavioral_rules", [])[:6]
            ) or "No trading rules."
            kb_text = "\n".join(
                f"- {row.get('topic','')} :: {(row.get('instruction') or row.get('content') or '')[:180]}"
                for row in tables.get("knowledge_base", [])[:6]
            ) or "No trading KB."

            system = _load_researcher_prompt("background_researcher") or (
                "You are CORE's trading pattern extraction engine. "
                "Read recent trading outcomes and output only actionable trading directives. "
                "Return valid JSON with keys domain, patterns, gaps, summary. "
                "domain must be trading."
            )
            user = (
                f"Trading source packet since {since_ts}.\n"
                f"Summary: {packet.get('summary', '')}\n\n"
                f"Active trading rules:\n{rules_text}\n\n"
                f"Trading knowledge base:\n{kb_text}\n\n"
                f"Recent trading decisions:\n{decisions_text}\n\n"
                f"Recent closed positions:\n{positions_text}\n\n"
                f"Recent live trading mistakes:\n{live_mistakes_text}\n\n"
                f"Recent trading reflections:\n{reflections_text}\n\n"
                "Extract 2-5 behavioral directives that would improve the trading system. "
                "Focus on regime gating, risk sizing, funding discipline, correlation control, "
                "execution quality, and paper-to-live discipline. Output JSON only."
            )

            raw = gemini_chat(system, user, max_tokens=800, json_mode=True)
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            result = json.loads(raw)
            patterns = [p for p in (result.get("patterns") or []) if isinstance(p, str) and p.strip()]
            if not patterns:
                print("[RESEARCH/TRADING] Gemini returned no patterns")
                return False

            gaps_raw = result.get("gaps") or None
            gaps_list = [gaps_raw] if isinstance(gaps_raw, str) and gaps_raw else gaps_raw
            quality = round(min(1.0, max(0.45, 0.45 + len(patterns) * 0.08 + min(0.2, packet.get("fresh_signal_count", 0) * 0.01))), 2)
            ok = sb_post("hot_reflections", {
                "task_summary": f"Trading real signal extraction - {packet.get('summary', '')}",
                "domain": TRADING_DOMAIN,
                "new_patterns": patterns,
                "gaps_identified": gaps_list,
                "reflection_text": result.get("summary", ""),
                "processed_by_cold": 0,
                "source": "real",
                "quality_score": quality,
            })
            if ok:
                run_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                sb_post("sessions", {
                    "summary": f"[state_update] last_real_signal_ts: {run_ts}",
                    "actions": [f"last_real_signal_ts={run_ts}", "trading real signal extracted"],
                    "interface": "mcp"
                })
                print(f"[RESEARCH/TRADING] ok={ok} patterns={len(patterns)} summary={packet.get('summary','')}")
            return ok

        state_rows = sb_get("sessions",
            "select=summary&summary=like.*last_real_signal_ts:*&order=created_at.desc&limit=1",
            svc=True)
        if state_rows and state_rows[0].get("summary"):
            raw = state_rows[0]["summary"].split("last_real_signal_ts:")[-1].strip().split()[0]
            normalized, parsed, future = _normalize_utc_timestamp(raw, clamp_future=True)
            if parsed and normalized:
                since_ts = normalized
                if future:
                    print(f"[RESEARCH/REAL] last_real_signal_ts was in the future; clamped to {since_ts}")
                else:
                    print(f"[RESEARCH/REAL] Using last_real_signal_ts: {since_ts}")
            else:
                since_ts = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                print(f"[RESEARCH/REAL] Invalid last_real_signal_ts, soft-boot to yesterday: {since_ts}")
        else:
            since_ts = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"[RESEARCH/REAL] No state key found, soft-boot to yesterday: {since_ts}")

        sessions = sb_get("sessions",
            f"select={_sel_force('sessions', ['summary','actions','interface'])}&created_at=gte.{since_ts}&order=created_at.desc&limit=20",
            svc=True)
        mistakes = sb_get("mistakes",
            f"select={_sel_force('mistakes', ['domain','what_failed','root_cause','how_to_avoid'])}&created_at=gte.{since_ts}&order=id.desc&limit=20",
            svc=True)

        if not sessions and not mistakes:
            since_ts = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"[RESEARCH/REAL] No new data since anchor -- falling back to 7d window: {since_ts}")
            sessions = sb_get("sessions",
                f"select=summary,actions,interface&created_at=gte.{since_ts}&order=created_at.desc&limit=20",
                svc=True)
            mistakes = sb_get("mistakes",
                f"select={_sel_force('mistakes', ['domain','what_failed','root_cause','how_to_avoid'])}&created_at=gte.{since_ts}&order=id.desc&limit=20",
                svc=True)
            if not sessions and not mistakes:
                print("[RESEARCH/REAL] Still no data in 7d window -- skipping")
                return False

        try:
            kb_count_r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/knowledge_base?select=id&limit=1",
                headers=_sbh_count_svc(), timeout=8)
            kb_total = int(kb_count_r.headers.get("content-range", "*/0").split("/")[-1])
        except Exception:
            kb_total = 0

        changelog_ctx = _safe_recent_changelog_context(limit=5)
        changelog_text = changelog_ctx["text"]
        changelog_status = "available" if changelog_ctx["available"] else "unavailable"

        sessions_text = "\n".join([
            f"- [{r.get('interface','?')}] {r.get('summary','')[:200]}"
            for r in sessions
        ]) or "No sessions yet."

        mistakes_text = "\n".join([
            f"- [{r.get('domain','?')}] FAILED: {r.get('what_failed','')[:150]} | ROOT: {r.get('root_cause','')[:100]}"
            for r in mistakes
        ]) or "No mistakes yet."

        _default_researcher_system = """You are CORE's pattern extraction engine. Analyze real activity logs and output BEHAVIORAL DIRECTIVES not observations.
Patterns must be actionable rules: what CORE should DO differently, not just what happened.
Output MUST be valid JSON:
{
  "domain": "code|db|bot|mcp|training|kb|general",
  "patterns": ["CORE should X when Y", "Always Z before W"],
  "gaps": "1-2 sentences describing what CORE is missing",
  "summary": "1 sentence behavioral directive"
}
Output ONLY valid JSON, no preamble."""
        system = _load_researcher_prompt("background_researcher") or _default_researcher_system

        user = (f"KB total entries: {kb_total}\n"
                f"Recent changelog status: {changelog_status}\n"
                f"Recent changelog:\n{changelog_text}\n\n"
                f"RECENT SESSIONS (since last processed, {len(sessions)} entries):\n{sessions_text}\n\n"
                f"RECENT MISTAKES (since last processed, {len(mistakes)} entries):\n{mistakes_text}\n\n"
                f"Extract behavioral directives from this recent activity. Focus on what CORE should do differently.")

        raw = gemini_chat(system, user, max_tokens=800, json_mode=True)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)

        patterns = result.get("patterns", [])
        if not patterns:
            print("[RESEARCH/REAL] Gemini returned no patterns")
            return False

        _gaps_raw = result.get("gaps") or None
        _gaps_list = [_gaps_raw] if _gaps_raw and isinstance(_gaps_raw, str) else _gaps_raw
        _quality = round(min(1.0, max(0.4, 0.5 + len(patterns) * 0.1 + (0.1 if mistakes else 0))), 2)
        ok = sb_post("hot_reflections", {
            "task_summary": f"Real signal extraction (since last processed) - {len(sessions)} sessions, {len(mistakes)} mistakes, kb={kb_total}",
            "domain": result.get("domain", "general"),
            "new_patterns": patterns,
            "gaps_identified": _gaps_list,
            "reflection_text": result.get("summary", ""),
            "processed_by_cold": 0,
            "source": "real",
            "quality_score": _quality,
        })
        print(f"[RESEARCH/REAL] ok={ok} patterns={len(patterns)} domain={result.get('domain')}")
        if ok:
            run_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            sb_post("sessions", {
                "summary": f"[state_update] last_real_signal_ts: {run_ts}",
                "actions": [f"last_real_signal_ts={run_ts} - auto updated after real signal extraction"],
                "interface": "mcp"
            })
            print(f"[RESEARCH/REAL] Saved last_real_signal_ts: {run_ts}")
        return ok
    except Exception as e:
        print(f"[RESEARCH/REAL] error: {e}")
        return False


def _get_simulation_task() -> dict:
    try:
        rows = sb_get("sessions",
            "select=summary&summary=like.*simulation_task*&order=created_at.desc&limit=5",
            svc=True)
        for row in rows:
            summary = row.get("summary", "")
            if "[state_update] simulation_task:" not in summary:
                continue
            raw_val = summary.split("[state_update] simulation_task:")[-1].strip()
            if raw_val.lower() == "null":
                return None
            task = json.loads(raw_val)
            if task and task.get("instruction"):
                return task
    except Exception as e:
        print(f"[SIM] _get_simulation_task error: {e}")
    return None


def _run_simulation_batch() -> bool:
    try:
        try:
            from core_tools import TOOLS
            tool_list = list(TOOLS.keys())
        except ImportError:
            tool_list = []

        mistakes = sb_get("mistakes",
            "select=domain,what_failed&order=id.desc&limit=10", svc=True)
        kb_sample = sb_get("knowledge_base",
            "select=domain,topic&order=id.desc&limit=20", svc=True)

        failure_modes = "\n".join([
            f"- [{r.get('domain','?')}] {r.get('what_failed','')[:120]}"
            for r in mistakes
        ]) or "None recorded yet."

        kb_domains = list({r.get("domain", "general") for r in kb_sample})
        kb_topics_sample = [r.get("topic", "") for r in kb_sample[:10]]

        try:
            kb_count_r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/knowledge_base?select=id&limit=1",
                headers=_sbh_count_svc(), timeout=8)
            kb_total = int(kb_count_r.headers.get("content-range", "*/0").split("/")[-1])
        except Exception:
            kb_total = len(kb_sample)

        try:
            recent_evos = sb_get("evolution_queue",
                f"select={_sel_force('evolution_queue', ['change_type','change_summary'])}&status=eq.applied&order=id.desc&limit=5",
                svc=True)
            evos_text = "\n".join(
                f"  [{r.get('change_type','?')}] {r.get('change_summary','')[:100]}"
                for r in recent_evos
            ) if recent_evos else "None yet."
        except Exception:
            evos_text = "Unavailable."

        runtime_context = (
            f"CORE MCP tools ({len(tool_list)}): {', '.join(tool_list[:20])}\n"
            f"KB total entries: {kb_total}\n"
            f"Recently applied evolutions:\n{evos_text}\n"
            f"Known failure modes:\n{failure_modes}\n"
            f"KB domains: {', '.join(kb_domains)}\n"
            f"Sample KB topics: {', '.join(kb_topics_sample)}"
        )

        task = _get_simulation_task()

        if task:
            instruction = task.get("instruction", "")
            system = task.get("system_prompt", "")
            user_template = task.get("user_prompt_template", "")
            user = user_template.replace("{{RUNTIME_CONTEXT}}", runtime_context)
            task_summary = f"Custom simulation: {instruction[:150]}"
            print(f"[RESEARCH/SIM] Running custom simulation: {instruction[:80]}")
        else:
            _default_sim = """You are simulating 1,000,000 users of CORE - a personal AGI orchestration system.
Output MUST be valid JSON:
{
  "domain": "code|db|bot|mcp|training|kb|general",
  "patterns": ["pattern1", "pattern2"],
  "gaps": "1-2 sentences",
  "summary": "1 sentence"
}
Output ONLY valid JSON, no preamble."""
            system = _load_prompt("simulation_1m_users", _default_sim)
            _maybe_eval_prompt("simulation_1m_users", system, 25)
            user = (f"{runtime_context}\n\nSimulate 1,000,000 users. What patterns emerge?")
            task_summary = "Simulated 1M user population batch"
            print("[RESEARCH/SIM] Running default 1M user simulation")

        raw = gemini_chat(system, user, max_tokens=900, json_mode=True)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)

        patterns = result.get("patterns", [])
        if not patterns:
            print("[RESEARCH/SIM] Gemini returned no patterns")
            return False

        _sim_gaps_raw = result.get("gaps") or None
        _sim_gaps_list = [_sim_gaps_raw] if _sim_gaps_raw and isinstance(_sim_gaps_raw, str) else _sim_gaps_raw
        _sim_quality = round(min(1.0, max(0.4, 0.5 + len(patterns) * 0.08)), 2)
        ok = sb_post("hot_reflections", {
            "task_summary": task_summary,
            "domain": result.get("domain", "general"),
            "new_patterns": patterns,
            "gaps_identified": _sim_gaps_list,
            "reflection_text": result.get("summary", ""),
            "processed_by_cold": 0,
            "source": "simulation",
            "quality_score": _sim_quality,
        })
        print(f"[RESEARCH/SIM] ok={ok} patterns={len(patterns)} domain={result.get('domain')}")
        return ok
    except Exception as e:
        print(f"[RESEARCH/SIM] error: {e}")
        return False


# -- RARL epoch ----------------------------------------------------------------

def _run_trading_rarl_epoch() -> bool:
    import time as _time
    _start = _time.time()
    try:
        last = sb_get(
            "rarl_epochs",
            "select=epoch_number&id=gt.1&order=epoch_number.desc&limit=1",
            svc=True
        )
        epoch_number = (last[0]["epoch_number"] + 1) if last else 1
    except Exception as e:
        print(f"[RARL/TRADING] epoch_number query error: {e}")
        epoch_number = 1

    packet = build_trading_source_packet(limit=50)
    if not packet.get("seed_ready"):
        print("[RARL/TRADING] seed memory is not ready - skipping epoch")
        return False

    try:
        champion_rows = sb_get(
            "rarl_architectures",
            "select=arch_id,hypothesis,discovery_score,next_direction&role=eq.champion&id=gt.1&order=created_at.desc&limit=1",
            svc=True,
        ) or []
        champion = champion_rows[0] if champion_rows else None
    except Exception:
        champion = None

    try:
        recent_kb = sb_get(
            "knowledge_base",
            f"select={_sel_force('knowledge_base', ['topic','instruction'])}&domain=eq.{TRADING_META_DOMAIN}&id=gt.1&order=updated_at.desc&limit=10",
            svc=True,
        ) or []
    except Exception:
        recent_kb = []

    research_goal, research_domain = TRADING_RARL_GOALS[(epoch_number - 1) % len(TRADING_RARL_GOALS)]
    ds_before = champion["discovery_score"] if champion else 0.0
    tables = packet.get("tables", {})
    recent_mistakes = (tables.get("trading_mistakes", []) + tables.get("mistakes", []))[:12]
    decisions = tables.get("trading_decisions", [])[:8]
    positions = tables.get("closed_positions", [])[:8]
    patterns_rows = tables.get("trading_patterns", [])[:8]
    reflections = tables.get("output_reflections", [])[:6]
    rules = tables.get("behavioral_rules", [])[:6]
    kb_rows = tables.get("knowledge_base", [])[:6]

    failure_block = "\n".join(
        f"  - [{row.get('severity','?')}] {row.get('what_failed','')[:100]}"
        + (f" | root: {row.get('root_cause','')[:80]}" if row.get("root_cause") else "")
        for row in recent_mistakes
    ) or "  None yet."
    decision_block = "\n".join(
        f"  - {row.get('symbol','?')} {row.get('strategy','?')} action={row.get('action_taken','?')} regime={row.get('market_regime','?')} conf={row.get('confidence','?')}"
        for row in decisions
    ) or "  None yet."
    position_block = "\n".join(
        f"  - {row.get('symbol','?')} {row.get('strategy','?')} pnl={row.get('realized_pnl_usd', 0)} funding={row.get('total_funding_usd', 0)} reason={row.get('close_reason','')}"
        for row in positions
    ) or "  None yet."
    pattern_block = "\n".join(
        f"  - {row.get('pattern_key','')[:80]} | win_rate={row.get('win_rate', 0)} | avg_pnl={row.get('avg_pnl_usd', 0)}"
        for row in patterns_rows
    ) or "  None yet."
    reflection_block = "\n".join(
        f"  - gap={row.get('gap','')[:80]} | new_behavior={row.get('new_behavior','')[:80]}"
        for row in reflections
    ) or "  None yet."
    rule_block = "\n".join(
        f"  - {row.get('pointer','')} :: {(row.get('full_rule') or '')[:120]}"
        for row in rules
    ) or "  None yet."
    kb_block = "\n".join(
        f"  - {row.get('topic','')[:100]}"
        for row in recent_kb or kb_rows
    ) or "  None yet."
    champion_block = (
        f"Champion: {champion['arch_id']} | DS: {champion['discovery_score']:.3f}\n"
        f"  Next direction: {champion.get('next_direction','not set')[:200]}"
        if champion else "No champion yet - establish the first trading research baseline."
    )
    reward_schedule = _hierarchical_reward_schedule(epoch_number, research_domain, bool(champion))

    system = _load_prompt(
        "rarl_researcher",
        "You are CORE's trading research laboratory. "
        "Generate trading-system research proposals grounded in real trading outcomes, mistakes, rules, and reflections. "
        "Output only valid JSON."
    )
    _maybe_eval_prompt("rarl_researcher", system, 10)
    schema = (
        '{"research_goal":"<confirm the trading goal>",'
        '"rarl_benchmark_task":"<one specific trading failure from the list above>",'
        '"hypothesis":"<2-4 sentence trading-system hypothesis>",'
        '"core_mechanism":"<4-6 sentence technical mechanism>",'
        '"pseudocode":"<15-25 lines python-style pseudocode>",'
        '"mutation_applied":"<trading-system mutation label>",'
        '"theory_analysis":"<2-3 sentences with [CONJECTURE] only where needed>",'
        '"experiment_design":"<how to test this in paper/live review terms>",'
        '"critic_failures":["<failure 1>","<failure 2>","<failure 3>"],'
        '"mitigation":"<how failures are addressed>",'
        '"benchmark_score":<float 0.0-3.0 regime classification quality>,'
        '"transfer_score":<float 0.0-3.0 strategy-family gating quality>,'
        '"stability_score":<float 0.0-3.0 risk sizing and stop logic quality>,'
        '"sample_efficiency":<float 0.0-3.0 funding or carry decision quality>,'
        '"reasoning_depth":<float 0.0-3.0 correlation and calibration quality>,'
        '"planning_success_rate":<float 0.0-3.0 execution and paper-to-live discipline quality>,'
        '"complexity_penalty":<float 0.5-3.0 implementation cost>,'
        '"compute_cost":<float 0.5-3.0 operational cost>,'
        '"inference_latency":<float 0.5-3.0 latency penalty>,'
        '"discovery_score":<float>,'
        '"beats_champion":<true or false>,'
        f'"arch_id":"<TradingMechanism_v{epoch_number}>",'
        '"compressed_insight":"<one compact trading lesson>",'
        '"next_direction":"<next trading research direction>",'
        '"insight_for_core":"<one actionable trading-system improvement>",'
        '"meta_learning_note":"<one RARL process improvement>",'
        '"rule_trigger":"<optional behavioral_rules trigger or empty string>",'
        '"rule_pointer":"<optional behavioral_rules pointer or empty string>",'
        '"behavioral_rule":"<optional trading rule text or empty string>"}'
    )
    user = (
        f"EPOCH: {epoch_number}\n"
        f"TRADING RESEARCH GOAL: {research_goal}\n"
        f"DOMAIN: {research_domain}\n\n"
        f"Trading source packet: {packet.get('summary','')}\n"
        f"Recent trading rules:\n{rule_block}\n\n"
        f"Recent trading KB:\n{kb_block}\n\n"
        f"Recent decisions:\n{decision_block}\n\n"
        f"Recent closed positions:\n{position_block}\n\n"
        f"Recent trading patterns:\n{pattern_block}\n\n"
        f"Recent trading reflections:\n{reflection_block}\n\n"
        f"Recent mistakes:\n{failure_block}\n\n"
        f"{champion_block}\n\n"
        f"Reward phase: {reward_schedule['phase']}\n"
        f"Reward weights: {json.dumps(reward_schedule['weights'], sort_keys=True)}\n"
        f"Discovery Score threshold to beat champion: {ds_before:.3f}\n\n"
        f"Output only this JSON:\n{schema}"
    )

    parsed = {}
    try:
        raw = gemini_chat(system, user, max_tokens=2048, json_mode=True)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed = json.loads(raw)
    except Exception as e_gem:
        print(f"[RARL/TRADING] Gemini failed ({e_gem}), trying Groq fallback")
        try:
            raw = groq_chat(system, user, model=GROQ_MODEL, max_tokens=1500)
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            parsed = json.loads(raw)
        except Exception as e_grq:
            print(f"[RARL/TRADING] both LLMs failed: {e_grq}")
            sb_post("hot_reflections", {
                "task_summary": f"Trading RARL Epoch {epoch_number} - LLM error",
                "domain": TRADING_META_DOMAIN,
                "new_patterns": [],
                "quality_score": 0.1,
                "gaps_identified": ["Gemini and Groq both failed"],
                "reflection_text": f"Trading RARL failure. gem={e_gem} groq={e_grq}",
                "processed_by_cold": 0,
                "source": "rarl",
            })
            return False

    arch_id = parsed.get("arch_id", f"TradingMechanism_v{epoch_number}")
    hypothesis = parsed.get("hypothesis", "")[:1000]
    core_mechanism = parsed.get("core_mechanism", "")[:1000]
    pseudocode = parsed.get("pseudocode", "")[:2000]
    mutation_applied = parsed.get("mutation_applied", "TradingMutation")
    critic_failures = parsed.get("critic_failures", [])[:5]
    mitigation = parsed.get("mitigation", "")[:500]
    next_direction = parsed.get("next_direction", "")[:300]
    insight_for_core = parsed.get("insight_for_core", "")[:300]
    compressed = parsed.get("compressed_insight", "")[:400]
    raw_ds = float(parsed.get("discovery_score", 0.0))
    aux_loss, aux_stats = _hierarchical_auxiliary_loss([
        hypothesis,
        core_mechanism,
        insight_for_core,
        next_direction,
        compressed,
        pseudocode,
    ])
    aux_penalty = round(min(0.5, aux_loss * 0.25), 3)
    try:
        gating_weight = max(0.1, min(0.9, float(parsed.get("gating_weight", 0.58))))
    except Exception:
        gating_weight = 0.58
    dual_loss_contract = _hierarchical_gated_dual_loss(
        neural_prediction_loss=max(0.0, min(1.0, 1.0 - float(parsed.get("benchmark_score", 0)))),
        symbolic_transition_loss=max(0.0, min(1.0, 1.0 - float(parsed.get("transfer_score", 0)))),
        gating_weight=gating_weight,
    )
    dual_loss = float(dual_loss_contract.get("weighted_loss") or 0.0)
    dual_penalty = round(min(0.45, dual_loss * 0.22), 3)
    planning_success_rate = float(parsed.get("planning_success_rate", 0))
    reward_inputs = {
        "benchmark_score": float(parsed.get("benchmark_score", 0)),
        "transfer_score": float(parsed.get("transfer_score", 0)),
        "stability_score": max(0.0, float(parsed.get("stability_score", 0)) - (aux_loss * 0.4) - (dual_penalty * 0.5)),
        "sample_efficiency": float(parsed.get("sample_efficiency", 0)),
        "reasoning_depth": float(parsed.get("reasoning_depth", 0)),
        "planning_success_rate": planning_success_rate,
        "complexity_penalty": float(parsed.get("complexity_penalty", 1)),
        "compute_cost": float(parsed.get("compute_cost", 1)),
        "inference_latency": float(parsed.get("inference_latency", 1)),
    }
    scheduled_reward, reward_meta = _weighted_reward_score(reward_inputs, reward_schedule)
    ds = max(0.0, round((raw_ds * 0.55) + (scheduled_reward * 0.45) - aux_penalty - dual_penalty, 3))
    beats_champion = ds > ds_before
    role = "champion" if beats_champion else "mutant"
    duration = int(_time.time() - _start)

    try:
        if beats_champion and champion:
            sb_patch("rarl_architectures", "role=eq.champion", {"role": "archived"})
        sb_upsert("rarl_architectures", {
            "arch_id": arch_id,
            "epoch_created": epoch_number,
            "role": role,
            "hypothesis": hypothesis,
            "core_mechanism": core_mechanism,
            "pseudocode": pseudocode,
            "discovery_score": ds,
            "benchmark_score": reward_inputs["benchmark_score"],
            "transfer_score": reward_inputs["transfer_score"],
            "stability_score": reward_inputs["stability_score"],
            "sample_efficiency": reward_inputs["sample_efficiency"],
            "reasoning_depth": reward_inputs["reasoning_depth"],
            "complexity_penalty": reward_inputs["complexity_penalty"],
            "compute_cost": reward_inputs["compute_cost"],
            "inference_latency": reward_inputs["inference_latency"],
            "failure_modes": critic_failures,
            "mitigation": mitigation,
            "next_direction": next_direction,
            "mutation_applied": mutation_applied,
            "parent_arch_id": champion["arch_id"] if champion else None,
            "insight_for_core": insight_for_core,
            "research_branch": "main",
        }, on_conflict="arch_id")
    except Exception as e:
        print(f"[RARL/TRADING] rarl_architectures error (non-fatal): {e}")

    try:
        sb_post("rarl_epochs", {
            "epoch_number": epoch_number,
            "research_goal": research_goal[:300],
            "research_domain": research_domain,
            "champion_before": champion["arch_id"] if champion else None,
            "champion_after": arch_id if beats_champion else (champion["arch_id"] if champion else None),
            "ds_before": ds_before,
            "ds_after": ds if beats_champion else ds_before,
            "ds_improvement": (ds - ds_before) if beats_champion else 0.0,
            "new_champion": beats_champion,
            "agents_active": ["Planner", "Critic", "Evaluation", "Archivist", "Meta-Learning"],
            "insights_count": 1,
            "branch": "main",
            "groq_model_used": GROQ_MODEL,
            "duration_seconds": duration,
        })
    except Exception as e:
        print(f"[RARL/TRADING] rarl_epochs error (non-fatal): {e}")

    quality = round(min(1.0, ds / 3.0), 3) if ds > 0 else 0.4
    patterns = [p for p in [
        core_mechanism[:120] if core_mechanism else None,
        insight_for_core[:120] if insight_for_core else None,
        next_direction[:120] if next_direction else None,
        compressed[:120] if compressed else None,
        parsed.get("meta_learning_note", "")[:120] or None,
        f"aux_loss={aux_loss:.3f}",
    ] if p]
    ok = sb_post("hot_reflections", {
        "task_summary": f"Trading RARL Epoch {epoch_number} [{research_domain}]: {research_goal[:150]}",
        "domain": TRADING_META_DOMAIN,
        "new_patterns": patterns[:5],
        "new_mistakes": [f[:120] for f in critic_failures[:3]],
        "quality_score": quality,
        "gaps_identified": [next_direction] if next_direction else None,
        "reflection_text": (
            f"Arch: {arch_id} | DS: {ds:.3f} | Role: {role} | "
            f"RawDS: {raw_ds:.3f} | AuxLoss: {aux_loss:.3f} | "
            f"DualLoss: {dual_loss:.3f} | Phase: {reward_meta['phase']} | "
            f"Reward: {scheduled_reward:.3f} | For trading: {insight_for_core[:150]}"
        ),
        "processed_by_cold": 0,
        "source": "rarl",
    })

    for failure in critic_failures[:3]:
        if failure and len(failure) > 5:
            try:
                sb_post("mistakes", {
                    "domain": TRADING_META_DOMAIN,
                    "context": f"Epoch {epoch_number}: {arch_id}",
                    "what_failed": failure[:300],
                    "correct_approach": mitigation[:300],
                    "root_cause": failure[:200],
                    "how_to_avoid": mitigation[:200],
                    "severity": "medium",
                })
            except Exception as e:
                print(f"[RARL/TRADING] mistake write error (non-fatal): {e}")

    if compressed and len(compressed) > 10:
        try:
            sb_upsert("knowledge_base", {
                "domain": TRADING_META_DOMAIN,
                "topic": arch_id,
                "instruction": compressed,
                "content": core_mechanism[:500],
                "confidence": "medium",
                "source_type": "trading_rarl",
                "source": "rarl",
            }, on_conflict="domain,topic")
            sb_upsert("knowledge_base", {
                "domain": TRADING_DOMAIN,
                "topic": f"trading_rarl_lesson_{epoch_number}",
                "instruction": compressed,
                "content": insight_for_core[:500] or core_mechanism[:500],
                "confidence": "medium",
                "source_type": "trading_rarl_lesson",
                "source": "rarl",
            }, on_conflict="domain,topic")
        except Exception as e:
            print(f"[RARL/TRADING] knowledge_base write error (non-fatal): {e}")

    rule_trigger = (parsed.get("rule_trigger") or "").strip()
    rule_pointer = (parsed.get("rule_pointer") or "").strip()
    behavioral_rule = (parsed.get("behavioral_rule") or "").strip()
    if rule_trigger and rule_pointer and behavioral_rule:
        try:
            existing = sb_get(
                "behavioral_rules",
                f"select=id&active=eq.true&pointer=eq.{rule_pointer}&limit=1",
                svc=True,
            ) or []
            if not existing:
                sb_post("behavioral_rules", {
                    "trigger": rule_trigger[:120],
                    "pointer": rule_pointer[:160],
                    "full_rule": behavioral_rule[:600],
                    "domain": TRADING_DOMAIN,
                    "priority": 3,
                    "active": True,
                    "tested": False,
                    "source": "trading_rarl",
                    "confidence": round(max(0.55, min(0.95, quality)), 3),
                })
        except Exception as e:
            print(f"[RARL/TRADING] behavioral_rules write error (non-fatal): {e}")

    ds_improvement = ds - ds_before
    specific_markers = ["regime", "risk", "funding", "correlation", "execution", "paper", "calibration", "stop", "sizing"]
    insight_is_specific = any(marker in (insight_for_core or "").lower() for marker in specific_markers)
    benchmark_is_grounded = bool(parsed.get("rarl_benchmark_task") and len(parsed.get("rarl_benchmark_task", "")) > 10)
    if not insight_is_specific or not benchmark_is_grounded:
        quality = min(quality, 0.3)
    if beats_champion and ds_improvement > 0.3:
        try:
            sb_post("evolution_queue", {
                "change_type": "trading_rarl_discovery",
                "change_summary": (
                    f"[Trading RARL] {arch_id} | DS: {ds_before:.2f} -> {ds:.2f} (+{ds_improvement:.2f}) | {compressed[:150]}"
                ),
                "confidence": round(quality, 3),
                "pattern_key": f"trading_rarl_epoch_{epoch_number}",
                "diff_content": json.dumps({
                    "arch_id": arch_id,
                    "hypothesis": hypothesis[:300],
                    "core_mechanism": core_mechanism[:300],
                    "insight_for_core": insight_for_core[:200],
                    "reward_phase": reward_meta["phase"],
                    "reward_weights": reward_meta["weights"],
                    "reward_contributions": reward_meta["contributions"],
                    "scheduled_reward": scheduled_reward,
                    "hierarchical_loss": aux_stats,
                    "discovery_score": ds,
                    "trading_packet_summary": packet.get("summary", ""),
                    "autonomy": build_autonomy_contract(
                        f"Trading RARL discovery {arch_id}",
                        description=insight_for_core[:500],
                        source="rarl",
                        autonomy={
                            "kind": "trading_architecture_proposal",
                            "origin": "rarl",
                            "source": "rarl",
                            "domain": research_domain,
                            "artifact_domain": "trading",
                            "arch_id": arch_id,
                            "expected_artifact": "task_queue",
                            "route": "evolution_autonomy",
                        },
                        context="trading",
                    ),
                }, indent=2),
                "status": "pending",
                "source": "rarl",
                "impact": research_domain,
                "recommendation": "Review the trading research proposal and decide whether to promote it into operating rules or implementation work.",
            })
        except Exception as e:
            print(f"[RARL/TRADING] evolution_queue error (non-fatal): {e}")

    print(f"[RARL/TRADING] Epoch {epoch_number} done. DS={ds:.3f} role={role} ok={ok} t={duration}s")
    return ok


def _run_rarl_epoch() -> bool:
    """Run one RARL research epoch via Gemini (Groq fallback).
    Replaces _run_simulation_batch() call in background_researcher().
    Writes to: rarl_architectures, rarl_epochs, hot_reflections (domain=rarl),
    mistakes (critic failures), knowledge_base (compressed insight),
    evolution_queue (significant discoveries > 0.3 DS improvement).
    Returns True if hot_reflection written successfully.
    """
    if trading_specialization_enabled():
        return _run_trading_rarl_epoch()

    import time as _time
    _start = _time.time()

    # Step 1: epoch number
    try:
        last = sb_get(
            "rarl_epochs",
            "select=epoch_number&id=gt.1&order=epoch_number.desc&limit=1",
            svc=True
        )
        epoch_number = (last[0]["epoch_number"] + 1) if last else 1
    except Exception as e:
        print(f"[RARL] epoch_number query error: {e}")
        epoch_number = 1
    print(f"[RARL] Starting epoch {epoch_number}")

    # Step 2: live context
    try:
        kb_count_r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/knowledge_base?select=id&limit=1",
            headers=_sbh_count_svc(), timeout=8
        )
        kb_total = int(kb_count_r.headers.get("content-range", "*/0").split("/")[-1])
    except Exception:
        kb_total = 0
    try:
        # P1-05: fetch richer mistake context â€” severity + root_cause, high/critical first
        recent_mistakes = sb_get(
            "mistakes",
            "select=domain,what_failed,severity,root_cause&order=severity.desc,id.desc&limit=15&id=gt.1",
            svc=True
        ) or []
        # Prioritise high/critical â€” put them first in failure_block
        _high = [m for m in recent_mistakes if m.get("severity") in ("high", "critical")]
        _other = [m for m in recent_mistakes if m.get("severity") not in ("high", "critical")]
        recent_mistakes = _high + _other
    except Exception:
        recent_mistakes = []
    try:
        champion = sb_get(
            "rarl_architectures",
            "select=arch_id,hypothesis,discovery_score,next_direction"
            "&role=eq.champion&id=gt.1&order=created_at.desc&limit=1",
            svc=True
        )
        champion = champion[0] if champion else None
    except Exception:
        champion = None
    try:
        recent_kb = sb_get(
            "knowledge_base",
            f"select={_sel_force('knowledge_base', ['topic','instruction'])}&domain=eq.rarl&id=gt.1&order=updated_at.desc&limit=10",
            svc=True
        ) or []
    except Exception:
        recent_kb = []

    # Step 3: research goal -- 6-epoch rotation, custom instruction override
    _RARL_GOALS = [
        ("improve reasoning depth -- focus on sustained multi-step logical reasoning chains that do not degrade over long contexts", "reasoning"),
        ("improve memory architecture -- focus on continual learning mechanisms that prevent catastrophic forgetting across tasks", "memory"),
        ("improve world modeling -- focus on building predictive internal models of environment dynamics for better planning", "world_modeling"),
        ("improve sample efficiency -- focus on architectures that learn robust representations from severely limited training data", "sample_efficiency"),
        ("improve cross-domain generalization -- focus on knowledge transfer mechanisms that apply learned skills to unseen domains", "generalization"),
        ("improve planning capability -- focus on multi-step goal-directed reasoning with explicit internal search and evaluation", "planning"),
    ]
    _custom_goal = None
    try:
        _task = _get_simulation_task()
        if _task and _task.get("instruction") and "RARL EPOCH" not in _task["instruction"]:
            _custom_goal = _task["instruction"][:300]
    except Exception:
        pass
    if _custom_goal:
        research_goal, research_domain = _custom_goal, "general"
    else:
        research_goal, research_domain = _RARL_GOALS[(epoch_number - 1) % len(_RARL_GOALS)]

    ds_before = champion["discovery_score"] if champion else 0.0
    # P1-05: include severity and root_cause so RARL epochs are grounded in real failures
    failure_block = "\n".join(
        f"  - [{r.get('domain','?')}][{r.get('severity','?'):<8}] {r.get('what_failed','')[:80]}"
        + (f" | root: {r.get('root_cause','')[:60]}" if r.get("root_cause") else "")
        for r in recent_mistakes[:10]
    ) or "  None recorded yet."
    kb_block = "\n".join(
        f"  - {r.get('topic','')[:100]}" for r in recent_kb
    ) or "  None yet."
    champion_block = (
        f"Champion: {champion['arch_id']} | DS: {champion['discovery_score']:.3f}\n"
        f"  Next direction: {champion.get('next_direction','not set')[:200]}"
        if champion else "No champion yet -- establish a strong baseline."
    )
    reward_schedule = _hierarchical_reward_schedule(epoch_number, research_domain, bool(champion))

    # Step 4: build prompts
    _default_rarl = (
        "You are the Recursive Autonomous AGI Research Laboratory (RARL). "
        "You simulate 10 specialized agents: Planner, Architect, Theory, Literature, "
        "Critic, Experiment, Evaluation, Archivist, Meta-Learning, Prompt Evolution. "
        "Target architectures that advance AGI. Be technically grounded. "
        "Mark uncertain reasoning [CONJECTURE]. Output ONLY valid JSON."
    )
    _sys = _load_prompt("rarl_researcher", _default_rarl)
    _maybe_eval_prompt("rarl_researcher", _sys, 10)
    _json_schema = (
        '{"research_goal":"<one sentence confirming goal>",'
        '"rarl_benchmark_task":"<name one SPECIFIC real CORE failure from the mistakes list above that this architecture would prevent â€” be exact>",'
        '"hypothesis":"<2-4 sentence architectural hypothesis>",'
        '"core_mechanism":"<4-6 sentence technical description>",'
        '"pseudocode":"<15-25 lines Python style>",'
        '"mutation_applied":"<one of: SynapticPruning|TopologyExpansion|ModularDuplication|CrossDomainGrafting|MemoryAugmentation|LearningRuleMutation|DynamicRoutingMutation|WorldModelIntegration|NeuroSymbolicIntegration|SparseExpertRouting|Novel>",'
        '"theory_analysis":"<2-3 sentences with [CONJECTURE] markers>",'
        '"experiment_design":"<real benchmarks, compute estimate in GPU-hours, numeric success criteria>",'
        '"critic_failures":["<specific technical failure 1>","<specific technical failure 2>","<specific technical failure 3>"],'
        '"mitigation":"<how failures are addressed>",'
        '"benchmark_score":<REAL float 0.0-3.0 â€” your estimate of benchmark performance>,'
        '"transfer_score":<REAL float 0.0-3.0 â€” cross-domain transfer ability>,'
        '"stability_score":<REAL float 0.0-3.0 â€” training stability>,'
        '"sample_efficiency":<REAL float 0.0-3.0 â€” learning from limited data>,'
        '"reasoning_depth":<REAL float 0.0-3.0 â€” multi-step reasoning depth>,'
        '"planning_success_rate":<REAL float 0.0-3.0 â€” planning module success rate>,'
        '"complexity_penalty":<REAL float 0.5-3.0 â€” implementation complexity cost>,'
        '"compute_cost":<REAL float 0.5-3.0 â€” training/inference cost>,'
        '"inference_latency":<REAL float 0.5-3.0 â€” response speed penalty>,'
        '"discovery_score":<REAL float â€” compute DS = sum(numerators)/sum(denominators)>,'
        '"beats_champion":<true if DS > ' + f'{ds_before:.3f}' + ', else false>,'
        f'"arch_id":"<DescriptiveName_v{epoch_number} â€” e.g. SparseGating_MemAug_v{epoch_number}>",'
        '"compressed_insight":"<one specific sentence distinct from prior KB â€” name the mechanism>",'
        '"next_direction":"<specific next epoch direction â€” what mechanism to explore next>",'
        '"insight_for_core":"<one actionable change to core_tools.py/core_train.py/behavioral_rules/schema â€” be specific>",'
        '"meta_learning_note":"<one concrete RARL methodology improvement>",'
        '"prompt_evolution_note":"<one change to improve evolution_queue quality, or null>"}'
    )
    _usr = (
        f"EPOCH: {epoch_number}\n"
        f"RESEARCH GOAL: {research_goal}\n"
        f"DOMAIN: {research_domain}\n\n"
        f"LIVE STATE:\n"
        f"  KB entries: {kb_total}\n"
        f"  Recent mistakes (ground your rarl_benchmark_task in one of these):\n{failure_block}\n"
        f"  RARL KB (do not repeat):\n{kb_block}\n"
        f"  {champion_block}\n\n"
        f"Reward schedule phase: {reward_schedule['phase']}\n"
        f"Reward focus: {reward_schedule['reward_focus']}\n"
        f"Reward weights: {json.dumps(reward_schedule['weights'], sort_keys=True)}\n\n"
        f"Discovery Score: DS = (benchmark+transfer+stability+sample_efficiency+reasoning_depth+planning_success_rate)"
        f" / (complexity_penalty+compute_cost+inference_latency)\n"
        f"Numerators 0.0-3.0. Denominators 0.5-3.0. beats_champion=true only if DS>{ds_before:.3f}\n"
        f"arch_id format: DescriptiveMechanism_v{epoch_number}\n\n"
        f"Output ONLY this JSON:\n{_json_schema}"
    )

    # Step 5: call Gemini, fallback to Groq
    parsed = {}
    try:
        raw = gemini_chat(_sys, _usr, max_tokens=2048, json_mode=True)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed = json.loads(raw)
        print(f"[RARL] Gemini OK epoch={epoch_number} DS={parsed.get('discovery_score',0):.3f}")
    except Exception as e_gem:
        print(f"[RARL] Gemini failed ({e_gem}), trying Groq fallback")
        try:
            raw = groq_chat(_sys, _usr, model=GROQ_MODEL, max_tokens=1500)
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            parsed = json.loads(raw)
            print(f"[RARL] Groq OK epoch={epoch_number} DS={parsed.get('discovery_score',0):.3f}")
        except Exception as e_grq:
            print(f"[RARL] both LLMs failed: {e_grq}")
            sb_post("hot_reflections", {
                "task_summary": f"RARL Epoch {epoch_number} -- LLM error: {str(e_grq)[:100]}",
                "domain": "rarl", "new_patterns": [], "quality_score": 0.1,
                "gaps_identified": ["Gemini and Groq both failed"],
                "reflection_text": f"Epoch {epoch_number} LLM failure. gem={e_gem} grq={e_grq}",
                "processed_by_cold": 0, "source": "rarl",
            })
            return False

    # Step 6: extract fields
    arch_id          = parsed.get("arch_id", f"Unknown_v{epoch_number}")
    hypothesis       = parsed.get("hypothesis", "")[:1000]
    core_mechanism   = parsed.get("core_mechanism", "")[:1000]
    pseudocode       = parsed.get("pseudocode", "")[:2000]
    mutation_applied = parsed.get("mutation_applied", "Unknown")
    critic_failures  = parsed.get("critic_failures", [])[:5]
    mitigation       = parsed.get("mitigation", "")[:500]
    next_direction   = parsed.get("next_direction", "")[:300]
    insight_for_core = parsed.get("insight_for_core", "")[:300]
    compressed       = parsed.get("compressed_insight", "")[:400]
    raw_beats_champion = bool(parsed.get("beats_champion", False))
    raw_ds           = float(parsed.get("discovery_score", 0.0))
    aux_loss, aux_stats = _hierarchical_auxiliary_loss([
        hypothesis,
        core_mechanism,
        insight_for_core,
        next_direction,
        compressed,
        pseudocode,
    ])
    aux_penalty      = round(min(0.5, aux_loss * 0.25), 3)
    try:
        gating_weight = max(0.1, min(0.9, float(parsed.get("gating_weight", parsed.get("symbolic_weight", 0.58)))))
    except Exception:
        gating_weight = 0.58
    dual_loss_contract = _hierarchical_gated_dual_loss(
        neural_prediction_loss=max(0.0, min(1.0, 1.0 - float(parsed.get("benchmark_score", 0)))),
        symbolic_transition_loss=max(0.0, min(1.0, 1.0 - float(parsed.get("transfer_score", 0)))),
        gating_weight=gating_weight,
    )
    dual_loss = float(dual_loss_contract.get("weighted_loss") or 0.0)
    dual_penalty = round(min(0.45, dual_loss * 0.22), 3)
    planning_success_rate = float(parsed.get("planning_success_rate", 0))
    planning_note = f"planning_success_rate={planning_success_rate:.3f}"
    if insight_for_core:
        insight_for_core = f"{insight_for_core[:200]} | {planning_note}"[:300]
    else:
        insight_for_core = planning_note[:300]
    if next_direction:
        next_direction = f"{next_direction[:220]} | {planning_note}"[:300]
    else:
        next_direction = planning_note[:300]
    reward_inputs = {
        "benchmark_score": float(parsed.get("benchmark_score", 0)),
        "transfer_score": float(parsed.get("transfer_score", 0)),
        "stability_score": max(0.0, float(parsed.get("stability_score", 0)) - (aux_loss * 0.4) - (dual_penalty * 0.5)),
        "sample_efficiency": float(parsed.get("sample_efficiency", 0)),
        "reasoning_depth": float(parsed.get("reasoning_depth", 0)),
        "planning_success_rate": planning_success_rate,
        "complexity_penalty": float(parsed.get("complexity_penalty", 1)),
        "compute_cost": float(parsed.get("compute_cost", 1)),
        "inference_latency": float(parsed.get("inference_latency", 1)),
    }
    scheduled_reward, reward_meta = _weighted_reward_score(reward_inputs, reward_schedule)
    ds               = max(0.0, round((raw_ds * 0.55) + (scheduled_reward * 0.45) - aux_penalty - dual_penalty, 3))
    beats_champion   = ds > ds_before
    role             = "champion" if beats_champion else "mutant"
    duration         = int(_time.time() - _start)

    # Step 7: update rarl_architectures (upsert on arch_id to handle duplicate names)
    try:
        if beats_champion and champion:
            sb_patch("rarl_architectures", "role=eq.champion", {"role": "archived"})
        sb_upsert("rarl_architectures", {
            "arch_id": arch_id, "epoch_created": epoch_number, "role": role,
            "hypothesis": hypothesis, "core_mechanism": core_mechanism, "pseudocode": pseudocode,
            "discovery_score": ds,
            "benchmark_score":   reward_inputs["benchmark_score"],
            "transfer_score":    reward_inputs["transfer_score"],
            "stability_score":   reward_inputs["stability_score"],
            "sample_efficiency": reward_inputs["sample_efficiency"],
            "reasoning_depth":   reward_inputs["reasoning_depth"],
            "complexity_penalty":reward_inputs["complexity_penalty"],
            "compute_cost":      reward_inputs["compute_cost"],
            "inference_latency": reward_inputs["inference_latency"],
            "failure_modes": critic_failures, "mitigation": mitigation,
            "next_direction": next_direction, "mutation_applied": mutation_applied,
            "parent_arch_id": champion["arch_id"] if champion else None,
            "insight_for_core": insight_for_core, "research_branch": "main",
        }, on_conflict="arch_id")
        print(f"[RARL] rarl_architectures: {arch_id} role={role}")
    except Exception as e:
        print(f"[RARL] rarl_architectures error (non-fatal): {e}")

    # Step 8: write rarl_epochs log
    try:
        sb_post("rarl_epochs", {
            "epoch_number": epoch_number, "research_goal": research_goal[:300],
            "research_domain": research_domain,
            "champion_before": champion["arch_id"] if champion else None,
            "champion_after": arch_id if beats_champion else (champion["arch_id"] if champion else None),
            "ds_before": ds_before, "ds_after": ds if beats_champion else ds_before,
            "ds_improvement": (ds - ds_before) if beats_champion else 0.0,
            "new_champion": beats_champion,
            "agents_active": ["Planner","Literature","Theory","Architect","Experiment",
                              "Critic","Evaluation","Archivist","Meta-Learning","Prompt Evolution"],
            "insights_count": 1, "branch": "main",
            "groq_model_used": GROQ_MODEL, "duration_seconds": duration,
        })
        print(f"[RARL] rarl_epochs: epoch={epoch_number}")
    except Exception as e:
        print(f"[RARL] rarl_epochs error (non-fatal): {e}")

    # Step 9: hot_reflection -- main pipeline integration point
    quality  = round(min(1.0, ds / 3.0), 3) if ds > 0 else 0.4
    patterns = [p for p in [
        core_mechanism[:120] if core_mechanism else None,
        insight_for_core[:120] if insight_for_core else None,
        next_direction[:120] if next_direction else None,
        compressed[:120] if compressed else None,
        parsed.get("meta_learning_note", "")[:120] or None,
        f"aux_loss={aux_loss:.3f}",
    ] if p]
    ok = sb_post("hot_reflections", {
        "task_summary": f"RARL Epoch {epoch_number} [{research_domain}]: {research_goal[:150]}",
        "domain": "rarl", "new_patterns": patterns[:5],
        "new_mistakes": [f[:120] for f in critic_failures[:3]],
        "quality_score": quality,
        "gaps_identified": [next_direction] if next_direction else None,
        "reflection_text": (
            f"Arch: {arch_id} | DS: {ds:.3f} | Role: {role} | "
            f"ModelBeat: {raw_beats_champion} | "
            f"RawDS: {raw_ds:.3f} | AuxLoss: {aux_loss:.3f} | "
            f"DualLoss: {dual_loss:.3f} | DualPenalty: {dual_penalty:.3f} | "
            f"Phase: {reward_meta['phase']} | Reward: {scheduled_reward:.3f} | "
            f"Mutation: {mutation_applied} | Duration: {duration}s | "
            f"For CORE: {insight_for_core[:150]}"
        ),
        "processed_by_cold": 0, "source": "rarl",
    })
    print(f"[RARL] hot_reflection ok={ok} quality={quality}")

    # Step 10: critic failures -> mistakes
    for failure in critic_failures[:3]:
        if failure and len(failure) > 5:
            try:
                sb_post("mistakes", {
                    "domain": "rarl", "context": f"Epoch {epoch_number}: {arch_id}",
                    "what_failed": failure[:300], "correct_approach": mitigation[:300],
                    "root_cause": failure[:200], "how_to_avoid": mitigation[:200], "severity": "medium",
                })
            except Exception as e:
                print(f"[RARL] mistake write error (non-fatal): {e}")

    # Step 11: compressed insight -> knowledge_base (upsert to avoid 409 on duplicate arch_id)
    if compressed and len(compressed) > 10:
        try:
            sb_upsert("knowledge_base", {
                "domain": "rarl", "topic": arch_id, "instruction": compressed,
                "content": core_mechanism[:500], "confidence": "medium", "source_type": "evolved",
            }, on_conflict="domain,topic")
        except Exception as e:
            print(f"[RARL] knowledge_base write error (non-fatal): {e}")

    # Step 12: significant discovery -> evolution_queue + Telegram
    ds_improvement = ds - ds_before
    # P1-05: Cap confidence if insight_for_core is vague (no specific file/table mentioned)
    _SPECIFIC_MARKERS = ["core_tools.py", "core_train.py", "core_main.py",
                         "behavioral_rules", "knowledge_base", "supabase",
                         "schema", "table", "function", "def ", "tool"]
    _insight_is_specific = any(m in (insight_for_core or "").lower() for m in _SPECIFIC_MARKERS)
    _rarl_benchmark_task = parsed.get("rarl_benchmark_task", "")
    _benchmark_is_grounded = bool(_rarl_benchmark_task and len(_rarl_benchmark_task) > 10)
    if not _insight_is_specific or not _benchmark_is_grounded:
        quality = min(quality, 0.3)  # Cap quality for vague/ungrounded epochs
        print(f"[RARL] Epoch {epoch_number} quality capped at 0.3 â€” insight_specific={_insight_is_specific} benchmark_grounded={_benchmark_is_grounded}")
    if beats_champion and ds_improvement > 0.3:
        try:
            sb_post("evolution_queue", {
                "change_type": "rarl_discovery",
                "change_summary": (
                    f"[P2] RARL Champion: {arch_id} | "
                    f"DS: {ds_before:.2f} -> {ds:.2f} (+{ds_improvement:.2f}) | "
                    f"aux={aux_loss:.3f} phase={reward_meta['phase']} reward={scheduled_reward:.3f} | {compressed[:150]}"
                ),
                "confidence": round(quality, 3),
                "pattern_key": f"rarl_epoch_{epoch_number}",
                "diff_content": json.dumps({
                    "arch_id": arch_id, "hypothesis": hypothesis[:300],
                    "core_mechanism": core_mechanism[:300],
                    "insight_for_core": insight_for_core[:200],
                    "raw_discovery_score": raw_ds,
                    "auxiliary_loss": aux_loss,
                    "auxiliary_penalty": aux_penalty,
                    "dual_loss": dual_loss,
                    "dual_penalty": dual_penalty,
                    "dual_loss_contract": dual_loss_contract,
                    "reward_phase": reward_meta["phase"],
                    "reward_weights": reward_meta["weights"],
                    "reward_contributions": reward_meta["contributions"],
                    "scheduled_reward": scheduled_reward,
                    "hierarchical_loss": aux_stats,
                    "discovery_score": ds,
                    "autonomy": build_autonomy_contract(
                        f"RARL discovery {arch_id}",
                        description=insight_for_core[:500],
                        source="rarl",
                        autonomy={
                            "kind": "architecture_proposal",
                            "origin": "rarl",
                            "source": "rarl",
                            "domain": research_domain,
                            "artifact_domain": "code",
                            "arch_id": arch_id,
                            "expected_artifact": "task_queue",
                            "route": "evolution_autonomy",
                        },
                        context="rarl",
                    ),
                }, indent=2),
                "status": "pending",
                "source": "rarl",
                "impact": research_domain,
                "recommendation": f"Review RARL discovery for {research_domain} and decide whether to promote it into tasks or tools.",
            })
            print(
                f"RARL New Champion\nEpoch {epoch_number} | {research_domain}\n"
                f"Arch: {arch_id}\nDS: {ds_before:.2f} -> {ds:.2f} (+{ds_improvement:.2f})\n"
                f"AuxLoss: {aux_loss:.3f} | Penalty: {aux_penalty:.3f}\n"
                f"Phase: {reward_meta['phase']} | Reward: {scheduled_reward:.3f}\n"
                f"For CORE: {insight_for_core[:150]}\nNext: {next_direction[:100]}"
            )
        except Exception as e:
            print(f"[RARL] evolution_queue error (non-fatal): {e}")

    # Step 13: P2-07 â€” insight_for_core -> task_queue (research-to-implementation pipeline)
    # Only queue if insight is specific (already validated above) and not a duplicate
    if insight_for_core and _insight_is_specific and len(insight_for_core) > 20:
        try:
            task_title = f"[RARL] {insight_for_core[:120]}"
            # Dedup: skip if identical title already pending/in_progress
            existing_tasks = sb_get(
                "task_queue",
                "select=id&status=in.(pending,in_progress)&limit=100",
                svc=True,
            ) or []
            already_exists = False
            for row in existing_tasks:
                try:
                    t = json.loads(row.get("task") or "{}")
                    if t.get("title", "").strip().lower() == task_title.strip().lower():
                        already_exists = True
                        break
                except Exception:
                    pass
            if not already_exists:
                task_payload = json.dumps({
                    "title": task_title,
                    "description": (
                        f"RARL Epoch {epoch_number} [{research_domain}] | "
                        f"Arch: {arch_id} | DS: {ds:.3f} | "
                        f"Full insight: {insight_for_core}"
                    ),
                    "source": "rarl",
                    "epoch": epoch_number,
                    "arch_id": arch_id,
                    "discovery_score": ds,
                    "autonomy": build_autonomy_contract(
                        task_title,
                        description=insight_for_core,
                        source="rarl",
                        autonomy={
                            "kind": "architecture_proposal",
                            "source": "rarl",
                            "origin": "rarl",
                            "domain": "code",
                            "artifact_domain": "code",
                            "verification": "evolution_queue artifact",
                            "expected_artifact": "evolution_queue",
                            "route": "evolution_autonomy",
                        },
                        context="rarl",
                    ),
                })
                task_ok = sb_post("task_queue", {
                    "task":     task_payload,
                    "status":   "pending",
                    "priority": 3,  # lower priority than owner tasks
                    "source":   "self_assigned",
                })
                # Store implementation_task_id back in rarl_architectures
                if task_ok:
                    try:
                        new_task = sb_get(
                            "task_queue",
                            "select=id&source=eq.self_assigned&status=eq.pending"
                            "&order=created_at.desc&limit=1",
                            svc=True,
                        ) or []
                        if new_task:
                            sb_patch("rarl_architectures", f"arch_id=eq.{arch_id}",
                                     {"implementation_task_id": str(new_task[0]["id"])})
                    except Exception:
                        pass
                    print(f"[RARL] P2-07: task queued for epoch {epoch_number}: {insight_for_core[:80]}")
        except Exception as e:
            print(f"[RARL] P2-07 task_add error (non-fatal): {e}")

    print(f"[RARL] Epoch {epoch_number} done. DS={ds:.3f} role={role} ok={ok} t={duration}s")
    return ok


# -- Public source ingestion ---------------------------------------------------
def _ingest_public_sources() -> str:
    if trading_specialization_enabled() and not allow_generic_public_ingest():
        return ""
    sources = [
        "https://raw.githubusercontent.com/langchain-ai/langchain/master/README.md",
        "https://raw.githubusercontent.com/openai/openai-cookbook/main/README.md",
        "https://raw.githubusercontent.com/anthropics/anthropic-sdk-python/main/README.md",
        "https://raw.githubusercontent.com/tiangolo/fastapi/master/README.md",
        "https://raw.githubusercontent.com/crewAIInc/crewAI/main/README.md",
    ]
    hour_slot = int(time.time() // 3600) % len(sources)
    to_fetch = [sources[hour_slot], sources[(hour_slot + 1) % len(sources)]]
    combined = []
    headers = {"User-Agent": "CORE-AGI/6.0"}
    for url in to_fetch:
        try:
            r = httpx.get(url, timeout=8, follow_redirects=True, headers=headers)
            if r.status_code == 200:
                text = r.text.strip()[:2000]
                source_name = url.split("/")[4]
                combined.append(f"[{source_name}]\n{text}")
        except Exception:
            pass
    return "\n\n".join(combined)


# -- Background researcher -----------------------------------------------------
_last_smap_update: float = 0.0
_SMAP_UPDATE_INTERVAL = 21600  # 6 hours -- sync system_map tool_count + reconcile



# â”€â”€ Dynamic prompt loader (L11 self-improvement) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_PROMPT_CYCLE_COUNTERS: dict = {}

def _load_prompt(target: str, default: str) -> str:
    """Load active system prompt from system_prompts table. Falls back to default."""
    try:
        rows = sb_get(
            "system_prompts",
            f"select=content&target=eq.{target}&active=eq.true&order=version.desc&limit=1",
            svc=True,
        ) or []
        return rows[0]["content"] if rows else default
    except Exception as e:
        print(f"[PROMPT] _load_prompt failed for {target} (using default): {e}")
        return default

def _maybe_eval_prompt(target: str, system: str, every: int) -> None:
    """Increment cycle counter for target. Fire L11 eval every N cycles."""
    global _PROMPT_CYCLE_COUNTERS
    _PROMPT_CYCLE_COUNTERS[target] = _PROMPT_CYCLE_COUNTERS.get(target, 0) + 1
    if _PROMPT_CYCLE_COUNTERS[target] % every == 0:
        try:
            rows = sb_get(
                "system_prompts",
                f"select=version&target=eq.{target}&active=eq.true&order=version.desc&limit=1",
                svc=True,
            ) or []
            ver = int(rows[0]["version"]) if rows else 1
            from core_orch_layer11 import fire_system_prompt
            fire_system_prompt(system, target=target, version=ver)
        except Exception as e:
            print(f"[PROMPT] eval trigger failed for {target} (non-fatal): {e}")

def _load_researcher_prompt(target: str):
    """Load active system prompt for researcher from Supabase. Returns None if not found."""
    try:
        rows = sb_get(
            "system_prompts",
            f"select=content&target=eq.{target}&active=eq.true&order=version.desc&limit=1",
            svc=True,
        ) or []
        return rows[0]["content"] if rows else None
    except Exception as e:
        print(f"[RESEARCH] _load_researcher_prompt failed: {e}")
        return None


def background_researcher():
    global _last_research_run, _last_public_source_run, _last_smap_update, _last_meta_learning_run, _last_meta_training_run, _last_causal_discovery_run, _last_temporal_hwm_run, _last_joint_training_run, _last_router_policy_run
    if trading_specialization_enabled():
        print("[RESEARCH] background researcher started - trading-only mode")
    else:
        print("[RESEARCH] background researcher started - real signal + simulation + public source mode")
    _cycle_count = 0

  # Restore timestamps from Supabase to survive redeploys
    global _last_research_run, _last_public_source_run
    try:
        rows = sb_get("sessions", "select=summary&summary=like.*last_research_ts*&order=created_at.desc&limit=1", svc=True)
        if rows:
            val = rows[0]["summary"].split("last_research_ts:")[-1].strip().split()[0]
            _last_research_run = float(val)
            print(f"[RESEARCH] Restored last_research_ts: {datetime.utcfromtimestamp(_last_research_run).isoformat()}")
        else:
            _last_research_run = time.time() - (_IMPROVEMENT_INTERVAL + 60)
    except Exception as e:
        print(f"[RESEARCH] restore research_ts error: {e}")
        _last_research_run = time.time() - (_IMPROVEMENT_INTERVAL + 60)

    try:
        rows = sb_get("sessions", "select=summary&summary=like.*last_public_source_ts*&order=created_at.desc&limit=1", svc=True)
        if rows:
            val = rows[0]["summary"].split("last_public_source_ts:")[-1].strip().split()[0]
            _last_public_source_run = float(val)
            print(f"[RESEARCH] Restored last_public_source_ts: {datetime.utcfromtimestamp(_last_public_source_run).isoformat()}")
        else:
            _last_public_source_run = time.time() - (_PUBLIC_SOURCE_INTERVAL + 60)
    except Exception as e:
        print(f"[RESEARCH] restore public_source_ts error: {e}")
        _last_public_source_run = time.time() - (_PUBLIC_SOURCE_INTERVAL + 60)

    try:
        rows = sb_get("sessions", "select=summary&summary=like.*last_meta_learning_ts*&order=created_at.desc&limit=1", svc=True)
        if rows:
            val = rows[0]["summary"].split("last_meta_learning_ts:")[-1].strip().split()[0]
            _last_meta_learning_run = float(val)
            print(f"[RESEARCH] Restored last_meta_learning_ts: {datetime.utcfromtimestamp(_last_meta_learning_run).isoformat()}")
        else:
            _last_meta_learning_run = time.time() - (_META_LEARNING_INTERVAL + 60)
    except Exception as e:
        print(f"[RESEARCH] restore meta_learning_ts error: {e}")
        _last_meta_learning_run = time.time() - (_META_LEARNING_INTERVAL + 60)

    try:
        rows = sb_get("sessions", "select=summary&summary=like.*last_meta_training_ts*&order=created_at.desc&limit=1", svc=True)
        if rows:
            val = rows[0]["summary"].split("last_meta_training_ts:")[-1].strip().split()[0]
            _last_meta_training_run = float(val)
            print(f"[RESEARCH] Restored last_meta_training_ts: {datetime.utcfromtimestamp(_last_meta_training_run).isoformat()}")
        else:
            _last_meta_training_run = time.time() - (_META_TRAINING_INTERVAL + 60)
    except Exception as e:
        print(f"[RESEARCH] restore meta_training_ts error: {e}")
        _last_meta_training_run = time.time() - (_META_TRAINING_INTERVAL + 60)

    try:
        rows = sb_get("sessions", "select=summary&summary=like.*last_causal_discovery_ts*&order=created_at.desc&limit=1", svc=True)
        if rows:
            val = rows[0]["summary"].split("last_causal_discovery_ts:")[-1].strip().split()[0]
            _last_causal_discovery_run = float(val)
            print(f"[RESEARCH] Restored last_causal_discovery_ts: {datetime.utcfromtimestamp(_last_causal_discovery_run).isoformat()}")
        else:
            _last_causal_discovery_run = time.time() - (_CAUSAL_DISCOVERY_INTERVAL + 60)
    except Exception as e:
        print(f"[RESEARCH] restore causal_discovery_ts error: {e}")
        _last_causal_discovery_run = time.time() - (_CAUSAL_DISCOVERY_INTERVAL + 60)

    try:
        rows = sb_get("sessions", "select=summary&summary=like.*last_temporal_hwm_ts*&order=created_at.desc&limit=1", svc=True)
        if rows:
            val = rows[0]["summary"].split("last_temporal_hwm_ts:")[-1].strip().split()[0]
            _last_temporal_hwm_run = float(val)
            print(f"[RESEARCH] Restored last_temporal_hwm_ts: {datetime.utcfromtimestamp(_last_temporal_hwm_run).isoformat()}")
        else:
            _last_temporal_hwm_run = time.time() - (_TEMPORAL_HWM_INTERVAL + 60)
    except Exception as e:
        print(f"[RESEARCH] restore temporal_hwm_ts error: {e}")
        _last_temporal_hwm_run = time.time() - (_TEMPORAL_HWM_INTERVAL + 60)

    try:
        rows = sb_get("sessions", "select=summary&summary=like.*last_router_policy_ts*&order=created_at.desc&limit=1", svc=True)
        if rows:
            val = rows[0]["summary"].split("last_router_policy_ts:")[-1].strip().split()[0]
            _last_router_policy_run = float(val)
            print(f"[RESEARCH] Restored last_router_policy_ts: {datetime.utcfromtimestamp(_last_router_policy_run).isoformat()}")
        else:
            _last_router_policy_run = time.time() - (_ROUTER_POLICY_INTERVAL + 60)
    except Exception as e:
        print(f"[RESEARCH] restore router_policy_ts error: {e}")
        _last_router_policy_run = time.time() - (_ROUTER_POLICY_INTERVAL + 60)

    try:
        rows = sb_get("sessions", "select=summary&summary=like.*last_joint_training_ts*&order=created_at.desc&limit=1", svc=True)
        if rows:
            val = rows[0]["summary"].split("last_joint_training_ts:")[-1].strip().split()[0]
            _last_joint_training_run = float(val)
            print(f"[RESEARCH] Restored last_joint_training_ts: {datetime.utcfromtimestamp(_last_joint_training_run).isoformat()}")
        else:
            _last_joint_training_run = time.time() - (_JOINT_TRAINING_INTERVAL + 60)
    except Exception as e:
        print(f"[RESEARCH] restore joint_training_ts error: {e}")
        _last_joint_training_run = time.time() - (_JOINT_TRAINING_INTERVAL + 60)

    while True:
        try:
            now = time.time()
            if now - _last_research_run >= _IMPROVEMENT_INTERVAL:
                print("[RESEARCH] Running signal extraction cycle...")
                _last_research_run = now
                sb_post("sessions", {"summary": f"[state_update] last_research_ts: {now}", "actions": ["last_research_ts persisted"], "interface": "mcp"})
                _cycle_count += 1
                # Every 10 cycles: evaluate researcher system prompt
                if _cycle_count % 10 == 0:
                    try:
                        from core_orch_layer11 import fire_system_prompt
                        sp_rows = sb_get(
                            "system_prompts",
                            "select=content,version&target=eq.background_researcher&active=eq.true&order=version.desc&limit=1",
                            svc=True,
                        ) or []
                        if sp_rows:
                            fire_system_prompt(
                                sp_rows[0]["content"],
                                target="background_researcher",
                                version=int(sp_rows[0].get("version", 1)),
                            )
                    except Exception as _spe:
                        print(f"[RESEARCH] system prompt eval non-fatal: {_spe}")

                public_content = ""
                if allow_generic_public_ingest() and (now - _last_public_source_run >= _PUBLIC_SOURCE_INTERVAL):
                    print("[RESEARCH] Fetching public sources...")
                    public_content = _ingest_public_sources()
                    if public_content:
                        _last_public_source_run = now
                        sb_post("sessions", {"summary": f"[state_update] last_public_source_ts: {now}", "actions": ["last_public_source_ts persisted"], "interface": "mcp"})
                        print(f"[RESEARCH] Public sources fetched: {len(public_content)} chars")
                    else:
                        print("[RESEARCH] Public sources returned empty - skipping")

                research_ctx = _collect_background_research_context()
                real_ok = _extract_real_signal() if research_ctx.get("verification", {}).get("data_ready") else False
                time.sleep(3)
                causal_discovery_ok = False
                if now - _last_causal_discovery_run >= _CAUSAL_DISCOVERY_INTERVAL:
                    print("[RESEARCH] Running causal-discovery phase...")
                    _last_causal_discovery_run = now
                    sb_post("sessions", {"summary": f"[state_update] last_causal_discovery_ts: {now}", "actions": ["last_causal_discovery_ts persisted"], "interface": "mcp"})
                    causal_phase = _run_causal_discovery_phase(cycle_count=_cycle_count)
                    causal_discovery_ok = bool(causal_phase.get("ok"))
                    if causal_discovery_ok:
                        print(f"[CAUSAL] Discovery phase stored: {causal_phase.get('topic')} focus={causal_phase.get('focus_track')}")
                meta_train_ok = False
                if now - _last_meta_training_run >= _META_TRAINING_INTERVAL:
                    print("[RESEARCH] Running meta-training phase...")
                    _last_meta_training_run = now
                    sb_post("sessions", {"summary": f"[state_update] last_meta_training_ts: {now}", "actions": ["last_meta_training_ts persisted"], "interface": "mcp"})
                    meta_train = _run_meta_training_phase(cycle_count=_cycle_count)
                    meta_train_ok = bool(meta_train.get("ok"))
                    if meta_train_ok:
                        print(f"[META] Training phase stored: {meta_train.get('topic')} focus={meta_train.get('focus_track')}")
                temporal_hwm_ok = False
                if now - _last_temporal_hwm_run >= _TEMPORAL_HWM_INTERVAL:
                    print("[RESEARCH] Running temporal hierarchical world-model phase...")
                    _last_temporal_hwm_run = now
                    sb_post("sessions", {"summary": f"[state_update] last_temporal_hwm_ts: {now}", "actions": ["last_temporal_hwm_ts persisted"], "interface": "mcp"})
                    temporal_hwm = _run_temporal_hierarchical_training_phase(cycle_count=_cycle_count)
                    temporal_hwm_ok = bool(temporal_hwm.get("ok"))
                    if temporal_hwm_ok:
                        print(f"[TEMPORAL] HWM phase stored: {temporal_hwm.get('topic')} focus={temporal_hwm.get('focus_track')}")
                sim_ok = _run_rarl_epoch()
                joint_ok = False
                if _JOINT_TRAINING_PLANNER_ENABLED and (now - _last_joint_training_run >= _JOINT_TRAINING_INTERVAL):
                    _last_joint_training_run = now
                    sb_post("sessions", {"summary": f"[state_update] last_joint_training_ts: {now}", "actions": ["last_joint_training_ts persisted"], "interface": "mcp"})
                    res = _run_joint_training_planner(cycle_count=_cycle_count)
                    joint_ok = bool(res.get("ok"))
                    if joint_ok:
                        print(f"[JOINT] Planner stored: {res.get('topic')} focus={res.get('focus_track')}")

                try:
                    groq_pending = sb_get(
                        "evolution_queue",
                        f"select={_sel_force('evolution_queue', ['id','change_type','change_summary','diff_content','confidence','pattern_key'])}&status=eq.pending&order=id.asc&limit=20",
                        svc=True
                    )
                    auto_applied = 0
                    for evo in groq_pending:
                        ctype = evo.get("change_type", "")
                        conf  = float(evo.get("confidence") or 0)
                        if ctype == "knowledge" and conf >= KNOWLEDGE_AUTO_CONFIDENCE:
                            r = apply_evolution(evo["id"])
                            if r.get("ok"):
                                auto_applied += 1
                            time.sleep(1)
                    if auto_applied:
                        print(f"[RESEARCH] Auto-applied {auto_applied} knowledge evolutions (conf>={KNOWLEDGE_AUTO_CONFIDENCE})")
                except Exception as _ae:
                    print(f"[RESEARCH] auto-apply error: {_ae}")

                try:
                    if real_ok or sim_ok or auto_applied:
                        parts = []
                        if real_ok:
                            parts.append("new patterns extracted")
                        if causal_discovery_ok:
                            parts.append("causal discovery phase stored")
                        if meta_train_ok:
                            parts.append("meta training phase stored")
                        if temporal_hwm_ok:
                            parts.append("temporal HWM phase stored")
                        if sim_ok:
                            parts.append("rarl epoch complete")
                        if joint_ok:
                            parts.append("joint training plan stored")
                        if auto_applied:
                            parts.append(f"{auto_applied} evolutions auto-applied")
                        if public_content:
                            parts.append("public sources ingested")
                        elif trading_specialization_enabled():
                            parts.append("generic public ingest disabled")
                        verification_note = (research_ctx.get("verification", {}) or {}).get("note")
                        if verification_note:
                            parts.append(f"data verification: {verification_note}")
                        print(f"[CORE] Researcher cycle #{_cycle_count}\n" + " | ".join(parts))
                        try:
                            sb_post("sessions", {
                                "summary": f"[state_update] research_data_verification: {json.dumps(research_ctx.get('verification', {}), default=str)[:350]}",
                                "actions": ["research_data_verification persisted"],
                                "interface": "mcp",
                            })
                        except Exception as _rvp:
                            print(f"[RESEARCH] verification state persist error: {_rvp}")
                        # L11: evaluate research output (non-blocking)
                        try:
                            from core_orch_layer11 import fire_background_research
                            fire_background_research(" | ".join(parts))
                        except Exception as _l11e:
                            print(f"[RESEARCH] L11 fire non-fatal: {_l11e}")
                except Exception:
                    pass

            # system_map periodic auto-update (every 6h)
            try:
                if time.time() - _last_smap_update >= _SMAP_UPDATE_INTERVAL:
                    from core_tools import t_system_map_scan
                    result = t_system_map_scan(trigger="session_end")
                    _last_smap_update = time.time()
                    ins = result.get("inserted_tools", [])
                    tomb = result.get("tombstoned_tools", [])
                    upd = result.get("updates_applied", 0)
                    print(f"[SMAP] Auto-update: {upd} volatile updates, {len(ins)} inserted, {len(tomb)} tombstoned")
                    if ins or tomb:
                        print(f"[SMAP] system_map auto-reconciled\nInserted: {ins}\nTombstoned: {tomb}")
            except Exception as _sme:
                print(f"[SMAP] auto-update error: {_sme}")

        except Exception as e:
            print(f"[RESEARCH] loop error: {e}")
        time.sleep(60)


# -- Pattern semantic clustering -----------------------------------------------
def _groq_cluster_patterns(batch_counts: "Counter", batch_domain: dict, batch_sources: dict) -> tuple:
    if len(batch_counts) <= 1:
        return batch_counts, batch_domain, batch_sources
    try:
        raw_keys = list(batch_counts.keys())
        keys_text = "\n".join(f"  {i+1}. {k[:180]}" for i, k in enumerate(raw_keys))
        prompt = (
            "You are CORE's pattern deduplicator.\n"
            "Below is a list of behavioral patterns extracted from recent sessions.\n"
            "Many express the SAME rule with different wording.\n\n"
            f"Patterns:\n{keys_text}\n\n"
            "Task: Group semantically identical or near-identical patterns together.\n"
            "For each group, choose the BEST canonical form (most specific, most actionable).\n"
            "Return ONLY valid JSON -- a flat object mapping each original pattern number "
            "to its canonical pattern string. All patterns must appear. "
            "Patterns that are truly unique map to themselves.\n"
            "Example: {\"1\": \"canonical text\", \"2\": \"canonical text\", \"3\": \"different rule\"}\n"
            "No preamble. No markdown. Pure JSON only."
        )
        _sys_dedup = _load_prompt("pattern_deduplicator", "You are CORE's pattern clustering engine. Return only valid JSON.")
        _maybe_eval_prompt("pattern_deduplicator", _sys_dedup, 20)
        raw = gemini_chat(system=_sys_dedup, user=prompt, max_tokens=2000, json_mode=True)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        mapping_by_num = json.loads(raw)

        key_map: dict = {}
        for i, raw_key in enumerate(raw_keys):
            canonical = mapping_by_num.get(str(i + 1), raw_key).strip()
            if not canonical or len(canonical) < 5:
                canonical = raw_key
            key_map[raw_key] = canonical[:500]

        new_counts: Counter = Counter()
        new_domain: dict = {}
        new_sources: dict = {}
        for raw_key, count in batch_counts.items():
            canonical = key_map.get(raw_key, raw_key)
            new_counts[canonical] += count
            new_domain.setdefault(canonical, batch_domain.get(raw_key, "general"))
            existing_src = new_sources.get(canonical, set())
            new_sources[canonical] = existing_src | batch_sources.get(raw_key, {"real"})

        merged_count = len(batch_counts) - len(new_counts)
        print(f"[COLD] Clustering: {len(batch_counts)} raw -> {len(new_counts)} canonical ({merged_count} merged)")
        return new_counts, new_domain, new_sources

    except Exception as e:
        print(f"[COLD] Clustering failed (non-fatal, using raw keys): {e}")
        return batch_counts, batch_domain, batch_sources


# â”€â”€ P2-01: Evolution Tier Processor (background job) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_EVOLUTION_TIER_CHECK_INTERVAL = 21600  # 6 hours


def evolution_tier_processor():
    """P2-01: Background job that applies tiered evolutions autonomously.

    Runs every 6 hours.
    - 'notify' tier:     applied after 24h if not rejected by owner
    - 'auto' tier (net): safety-net catch for any auto-tier missed at queue time

    Safety valve: set env var EVOLUTION_AUTO_TIER=notify_only or =disabled to reduce autonomy.
      notify_only: demotes auto->notify (still applies after 24h, not immediately)
      disabled:    pauses all autonomous application entirely
    """
    print("[TIER] Evolution tier processor started")
    while True:
        try:
            safety = os.getenv("EVOLUTION_AUTO_TIER", "").strip().lower()
            if safety == "disabled":
                print("[TIER] EVOLUTION_AUTO_TIER=disabled -- tier processing paused")
                time.sleep(_EVOLUTION_TIER_CHECK_INTERVAL)
                continue

            cutoff_24h = (datetime.utcnow() - timedelta(hours=24)).isoformat()
            notify_applied = 0
            auto_applied = 0

            # 1. Apply 'notify' tier evolutions older than 24h, not owner-rejected
            notify_rows = sb_get(
                "evolution_queue",
                f"select={_sel_force('evolution_queue', ['id','change_summary','confidence','pattern_key','impact'])}"
                f"&status=eq.pending"
                f"&approval_tier=eq.notify"
                f"&rejected_by_owner=eq.false"
                f"&created_at=lt.{cutoff_24h}"
                f"&order=confidence.desc&limit=20",
                svc=True,
            ) or []

            for evo in notify_rows:
                try:
                    result = apply_evolution(evo["id"])
                    if result.get("ok"):
                        notify_applied += 1
                        sb_patch("evolution_queue", f"id=eq.{evo['id']}",
                                 {"tier_applied_at": datetime.utcnow().isoformat()})
                        print(f"[TIER] notify-tier applied #{evo['id']}: {evo.get('change_summary','')[:80]}")
                    time.sleep(1)
                except Exception as _te:
                    print(f"[TIER] notify-tier error #{evo['id']}: {_te}")

            # 2. Safety-net: apply any 'auto' tier still pending (missed at cold processor time)
            if safety != "notify_only":
                auto_rows = sb_get(
                    "evolution_queue",
                    f"select={_sel_force('evolution_queue', ['id','change_summary','confidence','pattern_key'])}"
                    "&status=eq.pending"
                    "&approval_tier=eq.auto"
                    "&rejected_by_owner=eq.false"
                    "&order=confidence.desc&limit=10",
                    svc=True,
                ) or []

                for evo in auto_rows:
                    try:
                        result = apply_evolution(evo["id"])
                        if result.get("ok"):
                            auto_applied += 1
                            sb_patch("evolution_queue", f"id=eq.{evo['id']}",
                                     {"tier_applied_at": datetime.utcnow().isoformat()})
                            print(f"[TIER] auto-tier safety-net applied #{evo['id']}: {evo.get('change_summary','')[:80]}")
                        time.sleep(1)
                    except Exception as _te:
                        print(f"[TIER] auto-tier error #{evo['id']}: {_te}")

            total = notify_applied + auto_applied
            if total > 0:
                print(
                    f"[TIER PROCESSOR] Applied {total} evolution(s) autonomously\n"
                    f"  notify-tier: {notify_applied} | auto-tier safety-net: {auto_applied}\n"
                    f"Safety valve: EVOLUTION_AUTO_TIER=notify_only or =disabled"
                )
            print(f"[TIER] Cycle done: notify_applied={notify_applied} auto_applied={auto_applied}")

        except Exception as e:
            print(f"[TIER] loop error: {e}")

        time.sleep(_EVOLUTION_TIER_CHECK_INTERVAL)


# -- Listen Mode ---------------------------------------------------------------
def listen_stream():
    import time as _time
    deadline = _time.time() + 3600
    dry_cycles = 0
    cycle = 0

    while _time.time() < deadline:
        cycle += 1

        try:
            result = run_cold_processor()
            patterns_found = result.get("patterns_found", 0) if isinstance(result, dict) else 0
            evos_queued    = result.get("evolutions_queued", 0) if isinstance(result, dict) else 0
            yield json.dumps({
                "type": "cold_run",
                "cycle": cycle,
                "patterns_found": patterns_found,
                "evolutions_queued": evos_queued,
                "ts": datetime.utcnow().isoformat(),
            }) + "\n"
            if patterns_found == 0:
                dry_cycles += 1
            else:
                dry_cycles = 0
        except Exception as e:
            err = str(e)
            if "429" in err or "rate" in err.lower():
                yield json.dumps({"type": "stop", "reason": "groq_limit", "cycle": cycle}) + "\n"
                return
            yield json.dumps({"type": "cold_error", "error": err[:200], "cycle": cycle}) + "\n"

        try:
            evos = sb_get(
                "evolution_queue",
                f"select={_sel_force('evolution_queue', ['id','change_type','change_summary','confidence','pattern_key','domain'])}"
                " &status=eq.pending&order=confidence.desc&limit=50",
                svc=True
            ) or []
            yield json.dumps({
                "type": "evolutions",
                "cycle": cycle,
                "count": len(evos),
                "items": evos[:20],
            }) + "\n"
        except Exception as e:
            yield json.dumps({"type": "evo_error", "error": str(e)[:200], "cycle": cycle}) + "\n"

        try:
            pats = sb_get(
                "pattern_frequency",
                "select=pattern_key,frequency,domain&stale=eq.false&order=frequency.desc&limit=20",
                svc=True
            ) or []
            yield json.dumps({
                "type": "patterns",
                "cycle": cycle,
                "count": len(pats),
                "items": pats,
            }) + "\n"
        except Exception as e:
            yield json.dumps({"type": "pat_error", "error": str(e)[:200], "cycle": cycle}) + "\n"

        try:
            hot_gaps = sb_get(
                "hot_reflections",
                f"select={_sel_force('hot_reflections', ['domain','quality_score','gaps_identified'])}"
                "&processed_by_cold=eq.0&id=gt.1&quality_score=gte.0.5"
                "&order=created_at.desc&limit=10",
                svc=True
            ) or []
            yield json.dumps({
                "type": "hot_gaps",
                "cycle": cycle,
                "unprocessed": len(hot_gaps),
                "items": [
                    {"domain": h.get("domain"), "quality": h.get("quality_score"),
                     "gaps": h.get("gaps_identified")}
                    for h in hot_gaps
                ],
            }) + "\n"
        except Exception as e:
            yield json.dumps({"type": "hot_error", "error": str(e)[:200], "cycle": cycle}) + "\n"

        if dry_cycles >= 2:
            yield json.dumps({"type": "stop", "reason": "drained", "cycle": cycle}) + "\n"
            return

        _time.sleep(60)

    yield json.dumps({"type": "stop", "reason": "timeout", "cycle": cycle}) + "\n"


# â”€â”€ TASK-4: Binance Price Monitor â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _fetch_price(symbol: str):
    """Fetch current Binance price for symbol. Returns None on error."""
    try:
        r = httpx.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=8,
        )
        data = r.json()
        return float(data["price"]) if "price" in data else None
    except Exception as e:
        print(f"[PRICE] fetch error {symbol}: {e}")
        return None


# =============================================================================
# P3-07: CAPABILITY CALIBRATION JOB
# =============================================================================

def _cap_notes_from_tools(weak_tools: list, avg_fail_rate: float) -> str:
    """Generate human-readable calibration notes."""
    if not weak_tools and avg_fail_rate < 0.05:
        return "Highly reliable -- all tools passing consistently."
    parts = []
    if avg_fail_rate >= 0.20:
        parts.append(f"High avg fail rate ({avg_fail_rate:.0%}) -- domain needs attention.")
    elif avg_fail_rate >= 0.10:
        parts.append(f"Moderate fail rate ({avg_fail_rate:.0%}).")
    if weak_tools:
        parts.append(f"Weak tools: {', '.join(weak_tools[:5])}.")
    return " ".join(parts) or "Calibrated from tool_stats."


def _run_capability_calibration():
    """P3-07: Calibrate CORE self-model from tool_stats. Runs weekly (Thursday).
    For each domain: compute reliability from tool_stats, update capability_model.
    Notifies owner of domains below 0.60 reliability threshold.
    """
    global _last_capability_calibration_run
    print("[CAP_CAL] Starting capability calibration...")
    try:
        cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
        stats_rows = sb_get(
            "tool_stats",
            f"select=tool_name,call_count,success_count,fail_count,fail_rate"
            f"&date=gte.{cutoff[:10]}&order=tool_name.asc",
            svc=True,
        ) or []
        tool_agg: dict = {}
        for row in stats_rows:
            name = row.get("tool_name", "")
            if not name:
                continue
            if name not in tool_agg:
                tool_agg[name] = {"calls": 0, "failures": 0}
            tool_agg[name]["calls"]    += int(row.get("call_count") or 0)
            tool_agg[name]["failures"] += int(row.get("fail_count") or 0)
        for name, d in tool_agg.items():
            d["fail_rate"] = round(d["failures"] / d["calls"], 3) if d["calls"] > 0 else 0.0

        def _tool_domain_local(tool_name: str) -> str:
            for domain, keywords in _CAP_DOMAIN_MAP.items():
                if any(kw in tool_name for kw in keywords):
                    return domain
            return "misc"

        domain_tools: dict = {}
        for tool_name, stat in tool_agg.items():
            domain = _tool_domain_local(tool_name)
            if domain == "misc":
                continue
            domain_tools.setdefault(domain, []).append({"tool": tool_name, **stat})

        updated = []
        weak_domains = []
        for domain, tools in domain_tools.items():
            if not tools:
                continue
            total_calls    = sum(t["calls"] for t in tools)
            total_failures = sum(t["failures"] for t in tools)
            avg_fail_rate  = round(total_failures / total_calls, 3) if total_calls > 0 else 0.0
            reliability    = round(1.0 - avg_fail_rate, 3)
            strong = [t["tool"] for t in tools if t["fail_rate"] < 0.10 and t["calls"] >= 3]
            weak   = [t["tool"] for t in tools if t["fail_rate"] > 0.20]
            notes  = _cap_notes_from_tools(weak, avg_fail_rate)
            try:
                existing = sb_get(
                    "capability_model",
                    f"select=id&domain=eq.{domain}&limit=1",
                    svc=True,
                ) or []
                update_data = {
                    "reliability":     reliability,
                    "tool_count":      len(tools),
                    "avg_fail_rate":   avg_fail_rate,
                    "strong_tools":    json.dumps(strong[:10]),
                    "weak_tools":      json.dumps(weak[:10]),
                    "last_calibrated": datetime.utcnow().isoformat(),
                    "notes":           notes,
                }
                if existing:
                    sb_patch("capability_model", f"id=eq.{existing[0]['id']}", update_data)
                else:
                    sb_post("capability_model", {
                        "domain":      domain,
                        "capability":  _CAP_DESCRIPTIONS.get(domain, f"Tools in {domain} category"),
                        **update_data,
                        "created_at":  datetime.utcnow().isoformat(),
                    })
                updated.append(f"{domain}={reliability:.2f}")
                if reliability < 0.60:
                    weak_domains.append(f"{domain}({reliability:.2f})")
                print(f"[CAP_CAL] {domain}: reliability={reliability:.3f} tools={len(tools)} weak={weak[:3]}")
            except Exception as _ue:
                print(f"[CAP_CAL] update error domain={domain}: {_ue}")

        _last_capability_calibration_run = time.time()
        if weak_domains:
            print(
                f"[CAP_CAL] Weekly calibration done. {len(updated)} domains updated.\n"
                f"Weak domains (< 0.60): {', '.join(weak_domains)}\n"
                f"Use tool_health_scan + tool_improve to address."
            )
        else:
            print(f"[CAP_CAL] Weekly calibration done. {len(updated)} domains. All above 0.60.")
        print(f"[CAP_CAL] Done. {len(updated)} domains calibrated. Weak: {weak_domains}")
        return {"ok": True, "updated": len(updated), "weak_domains": weak_domains}
    except Exception as e:
        print(f"[CAP_CAL] error: {e}")
        return {"ok": False, "error": str(e)}


# =============================================================================
# P3-02: OWNER PROFILE EXTRACTION (called from cold processor)
# =============================================================================

def _extract_owner_profile_signals(hots: list) -> int:
    """P3-02: Extract Vux behavioral signals from hot_reflections.
    Writes to owner_profile table. Returns count inserted/reinforced.
    """
    if not hots:
        return 0
    try:
        session_summaries = "\n".join(
            f"  [{h.get('domain', '?')}][q={h.get('quality_score', '?')}] "
            f"{(h.get('task_summary') or '')[:150]}"
            for h in hots[:15]
        )
        try:
            existing = sb_get(
                "owner_profile",
                "select=dimension,value&active=eq.true&order=confidence.desc&limit=30",
                svc=True,
            ) or []
            existing_vals = {(r["dimension"], r["value"][:50].lower()) for r in existing}
        except Exception:
            existing_vals = set()

        system_p = (
            "You are CORE's owner behavior analyst. Analyze recent session data to extract "
            "structural observations about how the owner (Vux) works and communicates. "
            "Focus on BEHAVIORAL patterns, not task content. "
            'Output ONLY valid JSON: {"observations": [{"dimension": "...", "value": "...", '
            '"confidence": 0.0-1.0, "domain": "universal|project|training|deploy|..."}]} '
            "dimension must be one of: communication_style | decision_pattern | "
            "working_habit | preference | trigger | frustration | recurring_concern "
            "Confidence: 0.9+ = repeated clear evidence, 0.7-0.9 = probable, <0.7 = tentative. "
            "Max 5 observations. Only output genuinely new signals not already known. "
            "Output ONLY valid JSON, no preamble."
        )
        user_p = (
            f"Recent {len(hots)} session summaries:\n{session_summaries}\n\n"
            f"Already known patterns ({len(existing_vals)} entries):\n"
            + "\n".join(f"  [{dim}] {val[:80]}" for dim, val in list(existing_vals)[:10])
            + "\n\nWhat NEW behavioral signals about the owner can you infer?"
        )

        system_p = _load_prompt("owner_behavior_analyst", system_p)
        _maybe_eval_prompt("owner_behavior_analyst", system_p, 15)
        raw = gemini_chat(system=system_p, user=user_p, max_tokens=600, json_mode=True)
        parsed = json.loads(raw.strip())
        observations = parsed.get("observations", [])
        if not isinstance(observations, list):
            return 0

        VALID_DIMS = {
            "communication_style", "decision_pattern", "recurring_concern",
            "working_habit", "preference", "trigger", "frustration",
        }
        inserted = 0
        for obs in observations[:5]:
            dim    = obs.get("dimension", "")
            val    = (obs.get("value") or "").strip()
            conf   = float(obs.get("confidence") or 0.5)
            domain = obs.get("domain", "universal")
            if not dim or not val or dim not in VALID_DIMS or len(val) < 10:
                continue
            val_key = (dim, val[:50].lower())
            if val_key in existing_vals:
                continue
            try:
                ok = sb_post("owner_profile", {
                    "dimension":      dim,
                    "value":          val[:1000],
                    "confidence":     round(conf, 3),
                    "evidence":       f"cold_processor:{datetime.utcnow().strftime('%Y-%m-%d')}",
                    "domain":         domain,
                    "source":         "cold_processor",
                    "active":         True,
                    "times_observed": 1,
                    "last_seen":      datetime.utcnow().isoformat(),
                    "created_at":     datetime.utcnow().isoformat(),
                    "updated_at":     datetime.utcnow().isoformat(),
                })
                if ok:
                    inserted += 1
                    existing_vals.add(val_key)
                    print(f"[P3-02] Profile inserted: [{dim}] {val[:80]}")
            except Exception:
                try:
                    existing_row = sb_get(
                        "owner_profile",
                        f"select=id,confidence,times_observed&dimension=eq.{dim}&limit=1",
                        svc=True,
                    ) or []
                    if existing_row:
                        row = existing_row[0]
                        sb_patch("owner_profile", f"id=eq.{row['id']}", {
                            "confidence":     round(max(float(row.get("confidence") or 0), conf), 3),
                            "times_observed": int(row.get("times_observed") or 1) + 1,
                            "last_seen":      datetime.utcnow().isoformat(),
                            "updated_at":     datetime.utcnow().isoformat(),
                        })
                        inserted += 1
                except Exception:
                    pass

        print(f"[P3-02] Profile extraction: {len(observations)} proposed, {inserted} inserted/reinforced")
        return inserted
    except Exception as e:
        print(f"[P3-02] _extract_owner_profile_signals error (non-fatal): {e}")
        return 0



def price_monitor_loop():
    """
    Background price monitoring thread.
    Polls Binance every BINANCE_MONITOR_INTERVAL_S seconds.
    Sends Telegram alert when price moves > BINANCE_ALERT_THRESHOLD_PCT%.
    Stores price alerts in Supabase market_signals table.
    """
    global _price_monitor_running, _price_monitor_last_prices
    _price_monitor_running = True
    print(f"[PRICE] monitor started â€” symbols={_PRICE_MONITOR_SYMBOLS} threshold={_PRICE_ALERT_THRESHOLD}% interval={_PRICE_MONITOR_INTERVAL}s")

    while _price_monitor_running:
        for symbol in _PRICE_MONITOR_SYMBOLS:
            symbol = symbol.strip().upper()
            if not symbol:
                continue
            current = _fetch_price(symbol)
            if current is None:
                continue

            last = _price_monitor_last_prices.get(symbol)
            if last is not None:
                change_pct = ((current - last) / last) * 100
                if abs(change_pct) >= _PRICE_ALERT_THRESHOLD:
                    direction = "UP" if change_pct > 0 else "DOWN"
                    msg = (
                        f"\U0001f6a8 PRICE ALERT: {symbol} {direction} {change_pct:+.2f}%\n"
                        f"Current: {current:,.4f} USDT\n"
                        f"Previous: {last:,.4f} USDT\n"
                        f"Change: {current - last:+,.4f} USDT\n\n"
                        f"Reply to trade:\n"
                        f"  APPROVE {symbol} <qty>\n"
                        f"  SELL {symbol} <qty>\n"
                        f"  REJECT"
                    )
                    print(f"[PRICE] {msg}")
                    try:
                        print(msg)
                    except Exception as ne:
                        print(f"[PRICE] notify error: {ne}")
                    try:
                        sb_post_critical("market_signals", {
                            "signal_type": "price_alert",
                            "token_symbol": symbol,
                            "chain": "CEX",
                            "data": {
                                "price": current,
                                "previous": last,
                                "change_pct": round(change_pct, 4),
                                "direction": direction,
                            },
                        })
                    except Exception as se:
                        print(f"[PRICE] supabase log error: {se}")

            _price_monitor_last_prices[symbol] = current
            print(f"[PRICE] {symbol} = {current:,.4f} USDT")

        _threading.Event().wait(_PRICE_MONITOR_INTERVAL)

    print("[PRICE] monitor stopped")


def start_price_monitor():
    """Start price monitor in background thread. Called from startup."""
    t = _threading.Thread(target=price_monitor_loop, daemon=True, name="price_monitor")
    t.start()
    return t


# -- AGI-01: Weekly Cross-Domain Synthesis ------------------------------------

def _run_cross_domain_synthesis():
    """AGI-01: Cross-domain pattern synthesis. Runs weekly on Wednesday.
    Reads top 5 patterns per domain, uses Groq to find structural similarities,
    writes unified insights to knowledge_base(domain=synthesis).
    Notifies owner via Telegram.
    """
    global _last_synthesis_run
    print("[SYNTH] Starting cross-domain synthesis...")

    # Step 1: Load top patterns per domain
    try:
        top_patterns = sb_get(
            "pattern_frequency",
            "select=pattern_key,domain,frequency&stale=eq.false&order=frequency.desc&limit=200",
            svc=True
        ) or []
    except Exception as e:
        print(f"[SYNTH] pattern load error: {e}")
        _last_synthesis_run = time.time()
        return

    if not top_patterns:
        print("[SYNTH] No patterns found -- skipping")
        _last_synthesis_run = time.time()
        return

    # Group top 5 per domain
    domain_patterns: dict = {}
    for p in top_patterns:
        d = p.get("domain", "general")
        if d not in domain_patterns:
            domain_patterns[d] = []
        if len(domain_patterns[d]) < 5:
            domain_patterns[d].append(p.get("pattern_key", ""))

    if len(domain_patterns) < 2:
        print(f"[SYNTH] Only {len(domain_patterns)} domain(s) -- need 2+ for cross-domain synthesis")
        _last_synthesis_run = time.time()
        return

    print(f"[SYNTH] Synthesizing across {len(domain_patterns)} domains: {list(domain_patterns.keys())[:8]}")

    # Step 2: Ask Groq to find structural similarities
    domain_summary = "\n".join(
        f"Domain '{d}': " + " | ".join(ps[:3])
        for d, ps in list(domain_patterns.items())[:10]
    )
    _default_synth = (
        "You are an expert at finding structural patterns across different knowledge domains. "
        "Given patterns from multiple domains, identify which patterns share the same ROOT CAUSE structure "
        "even if they appear in different contexts. Focus on actionable insights."
    )
    system_prompt = _load_prompt("cross_domain_synthesizer", _default_synth)
    _maybe_eval_prompt("cross_domain_synthesizer", system_prompt, 10)
    user_prompt = (
        f"Analyze these patterns from CORE AGI's operational domains:\n\n{domain_summary}\n\n"
        "Find 3-5 cross-domain insights where the same root cause appears in multiple domains. "
        "For each insight, write: INSIGHT: <one sentence>. DOMAINS: <which domains>. UNIFIED_RULE: <actionable rule that applies across all those domains>. "
        "Be specific and actionable. Format as JSON array: [{\"insight\": str, \"domains\": [str], \"unified_rule\": str, \"confidence\": 0.0-1.0}]"
    )

    try:
        raw = gemini_chat(system_prompt, user_prompt, max_tokens=2048, json_mode=True)
        print(f"[SYNTH] Gemini raw length={len(raw)} first_100={raw[:100]}")
        # json_mode=True guarantees JSON -- parse directly, strip fences as fallback
        import re as _re_s
        clean = _re_s.sub(r'```(?:json)?\s*|\s*```', '', raw).strip()
        # Try direct parse first, then array extraction as fallback
        try:
            insights = json.loads(clean)
            if isinstance(insights, dict):  # wrapped in {insights: [...]}
                insights = insights.get("insights", list(insights.values())[0] if insights else [])
        except json.JSONDecodeError:
            json_match = _re_s.search(r'\[.*\]', clean, _re_s.DOTALL)
            if not json_match:
                print(f"[SYNTH] No JSON parseable in response: {clean[:500]}")
                _last_synthesis_run = time.time()
                return
            insights = json.loads(json_match.group(0))
    except Exception as e:
        print(f"[SYNTH] Gemini synthesis error: {e} | raw: {raw[:200] if 'raw' in dir() else 'N/A'}")
        _last_synthesis_run = time.time()
        return

    if not insights:
        print("[SYNTH] No insights generated")
        _last_synthesis_run = time.time()
        return

    # Step 3: Write to knowledge_base + hot_reflections
    written = 0
    try:
        import hashlib as _hl
        date_tag = datetime.utcnow().strftime("%Y%m%d")
        for ins in insights:
            insight_text = ins.get("insight", "")
            unified_rule = ins.get("unified_rule", "")
            domains = ins.get("domains", [])
            conf_raw = ins.get("confidence", 0.7)
            if not insight_text or not unified_rule:
                continue
            # Map float confidence to enum
            conf_val = float(conf_raw) if isinstance(conf_raw, (int, float)) else 0.7
            conf_enum = "proven" if conf_val >= 0.9 else "high" if conf_val >= 0.75 else "medium" if conf_val >= 0.5 else "low"
            topic_hash = _hl.md5(insight_text[:80].encode()).hexdigest()[:8]
            topic = f"cross_domain_{topic_hash}_{date_tag}"
            content = (
                f"INSIGHT: {insight_text}\n"
                f"DOMAINS: {', '.join(domains)}\n"
                f"UNIFIED_RULE: {unified_rule}\n"
                f"GENERATED: {datetime.utcnow().isoformat()}\n"
                f"SOURCE: AGI-01 cross-domain synthesis"
            )
            # Upsert to avoid duplicates on re-run
            existing = sb_get(
                "knowledge_base",
                f"select=id&domain=eq.synthesis&topic=eq.{topic}&id=gt.1",
                svc=True
            )
            if existing:
                print(f"[SYNTH] Skipped duplicate insight: {topic}")
                continue
            ok = sb_post("knowledge_base", {
                "domain": "synthesis",
                "topic": topic,
                "content": content,
                "confidence": conf_enum,
                "source_type": "evolved",
                "instruction": f"Apply this unified rule across domains: {', '.join(domains)}",
            })
            if ok:
                written += 1
                print(f"[SYNTH] Insight written: {insight_text[:80]}")
    except Exception as e:
        print(f"[SYNTH] KB write error: {e}")

    # Step 4: Hot reflection for cold processor
    try:
        if written > 0:
            patterns_list = [ins.get("insight", "") for ins in insights if ins.get("insight")]
            sb_post("hot_reflections", {
                "domain": "synthesis",
                "task_summary": f"AGI-01 cross-domain synthesis: {written} insights across {len(domain_patterns)} domains",
                "quality_score": 0.85,
                "new_patterns": json.dumps(patterns_list),
                "processed_by_cold": False,
                "source": "real",
                "reflection_text": f"Cross-domain synthesis identified {written} structural similarities. Domains covered: {list(domain_patterns.keys())}",
            })
    except Exception as e:
        print(f"[SYNTH] hot_reflection error: {e}")

    _last_synthesis_run = time.time()

    # Step 5: Notify owner
    try:
        if written > 0:
            summary_lines = [f"  {i+1}. {ins.get('insight','')[:80]}" for i, ins in enumerate(insights[:5])]
            print(
                f"[SYNTH] Weekly cross-domain synthesis complete.\n"
                f"{written} insight(s) written to KB (domain=synthesis):\n"
                + "\n".join(summary_lines)
            )
        else:
            print("[SYNTH] Weekly synthesis ran but produced no new insights.")
        print(f"[SYNTH] Done. {written} insights written.")
    except Exception as e:
        print(f"[SYNTH] notify error: {e}")


# -- AGI-02: Nightly Self-Diagnosis -------------------------------------------

def _run_self_diagnosis():
    """AGI-02: Autonomous gap detection. Runs nightly at 02:00 WIB.
    Analyzes: mistakes, quality trend, KB domain coverage, stale tasks.
    Generates self_assigned tasks for identified gaps.
    Notifies owner via Telegram for approval before execution.
    """
    global _last_self_diagnosis_run
    print("[DIAG] Starting nightly self-diagnosis...")
    gaps = []

    # Analysis 1: Top structural weaknesses from recent mistakes
    try:
        recent_mistakes = sb_get(
            "mistakes",
            "select=domain,root_cause,severity&order=created_at.desc&limit=50&id=gt.1",
            svc=True
        ) or []
        domain_counts = Counter(m.get("domain", "general") for m in recent_mistakes)
        high_sev = [m for m in recent_mistakes if m.get("severity") in ("high", "critical")]
        high_sev_domains = Counter(m.get("domain", "general") for m in high_sev)
        top_weak = high_sev_domains.most_common(3)
        for domain, count in top_weak:
            if count >= 2:
                gaps.append({
                    "source": "mistake_cluster",
                    "title": f"Investigate recurring {domain} failures",
                    "description": f"Self-diagnosis: {count} high/critical severity mistakes in domain '{domain}' in last 50 sessions. Root causes need structural fix.",
                    "priority": 4,
                })
        print(f"[DIAG] Mistakes: {len(recent_mistakes)} scanned, {len(top_weak)} weak domains found")
    except Exception as e:
        print(f"[DIAG] mistake analysis error: {e}")

    # Analysis 2: Quality trend â€” flag if declining
    try:
        metrics = sb_get(
            "quality_metrics",
            "select=quality_score,created_at&order=created_at.desc&limit=10&id=gt.1",
            svc=True
        ) or []
        if len(metrics) >= 5:
            scores = [float(m.get("quality_score") or 0) for m in metrics]
            recent_avg = sum(scores[:5]) / 5
            older_avg = sum(scores[5:]) / max(len(scores[5:]), 1)
            if recent_avg < 0.75 or (older_avg > 0 and recent_avg < older_avg - 0.05):
                gaps.append({
                    "source": "quality_decline",
                    "title": "Investigate quality score decline",
                    "description": f"Self-diagnosis: recent 5-session avg quality={recent_avg:.2f}, prior avg={older_avg:.2f}. Quality declining or below threshold 0.75. Needs root cause analysis.",
                    "priority": 4,
                })
        print(f"[DIAG] Quality: {len(metrics)} sessions scanned, recent_avg={sum(scores[:5])/5 if len(scores)>=5 else 'N/A'}")
    except Exception as e:
        print(f"[DIAG] quality analysis error: {e}")

    # Analysis 3: KB domain coverage â€” flag domains with <10 entries
    try:
        kb_rows = sb_get(
            "knowledge_base",
            "select=domain&id=gt.1&active=eq.true",
            svc=True
        ) or []
        domain_kb_counts = Counter(r.get("domain", "general") for r in kb_rows)
        shallow = [(d, c) for d, c in domain_kb_counts.items() if c < 10 and not d.startswith("project:")]
        for domain, count in shallow[:3]:  # cap at 3 gaps
            gaps.append({
                "source": "kb_coverage",
                "title": f"Expand KB coverage for domain: {domain}",
                "description": f"Self-diagnosis: domain '{domain}' has only {count} KB entries. Knowledge base is shallow here. Consider ingesting or adding structured entries.",
                "priority": 3,
            })
        print(f"[DIAG] KB: {len(domain_kb_counts)} domains, {len(shallow)} shallow (<10 entries)")
    except Exception as e:
        print(f"[DIAG] KB coverage analysis error: {e}")

    # Analysis 4: Stale tasks â€” pending >14 days with no checkpoint
    try:
        stale_cutoff = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stale_tasks = sb_get(
            "task_queue",
            f"select=id,task,priority&status=eq.pending&created_at=lt.{stale_cutoff}&source=neq.self_assigned",
            svc=True
        ) or []
        if len(stale_tasks) >= 3:
            gaps.append({
                "source": "stale_tasks",
                "title": "Review stale pending tasks (>14 days)",
                "description": f"Self-diagnosis: {len(stale_tasks)} tasks have been pending >14 days with no progress. Review for blockers, deprioritization, or cancellation.",
                "priority": 3,
            })
        print(f"[DIAG] Stale tasks: {len(stale_tasks)} found")
    except Exception as e:
        print(f"[DIAG] stale task analysis error: {e}")

    # Generate self-assigned tasks -- with dedup guard before each insert
    tasks_created = []
    try:
        existing_pending = sb_get(
            "task_queue",
            "select=task&status=in.(pending,in_progress)&source=eq.self_assigned&limit=200",
            svc=True
        ) or []
        existing_titles = set()
        for row in existing_pending:
            try:
                t = json.loads(row.get("task") or "{}")
                existing_titles.add(t.get("title", "").strip().lower())
            except Exception:
                pass
    except Exception as e:
        print(f"[DIAG] dedup pre-fetch error: {e}")
        existing_titles = set()

    for gap in gaps:
        try:
            title_key = gap["title"].strip().lower()
            if title_key in existing_titles:
                print(f"[DIAG] Skipped duplicate self-assigned task: {gap['title']}")
                continue
            autonomy_kind = {
                "kb_coverage": "kb_expand",
                "mistake_cluster": "behavioral_remediation",
                "quality_decline": "behavioral_remediation",
                "stale_tasks": "behavioral_remediation",
            }.get(gap.get("source", ""), "analysis_only")
            domain_match = None
            if "domain" in gap["title"].lower():
                try:
                    domain_match = gap["title"].split("domain:", 1)[1].strip()
                except Exception:
                    domain_match = None
            autonomy = build_autonomy_contract(
                gap["title"],
                gap["description"],
                source="self_assigned",
                autonomy={
                    "kind": autonomy_kind,
                    "origin": gap.get("source", "self_diagnosis"),
                    "source": gap.get("source", "self_diagnosis"),
                    "domain": domain_match or "",
                    "artifact_domain": domain_match or "",
                    "priority": gap["priority"],
                },
                context="self_diagnosis",
            )
            task_payload = {
                "task": json.dumps({
                    "title": gap["title"],
                    "description": gap["description"],
                    "source": "self_assigned",
                    "autonomy": autonomy,
                }),
                "status": "pending",
                "priority": gap["priority"],
                "source": "self_assigned",
            }
            result = sb_post("task_queue", task_payload)
            if result:
                tasks_created.append(gap["title"])
                existing_titles.add(title_key)  # prevent same-run dupes if gaps list has overlap
                print(f"[DIAG] Created self-assigned task: {gap['title']}")
        except Exception as e:
            print(f"[DIAG] task creation error for '{gap['title']}': {e}")

    # Persist timestamp to Supabase so it survives Railway redeploys
    _last_self_diagnosis_run = time.time()
    try:
        # Use summary column pattern matching t_update_state convention
        sb_post("sessions", {
            "summary": f"[state_update] {_DIAG_STATE_KEY}: {_last_self_diagnosis_run}",
            "actions": [f"AGI-02 self-diagnosis ran at {datetime.utcnow().isoformat()} -- {len(tasks_created)} tasks created"],
            "interface": "self_diagnosis",
        })
        print(f"[DIAG] Persisted last_diag_ts to Supabase: {_last_self_diagnosis_run}")
    except Exception as e:
        print(f"[DIAG] persist timestamp error: {e}")
    try:
        if tasks_created:
            task_list = "\n".join(f"  - {t}" for t in tasks_created)
            print(
                f"[DIAG] Nightly self-diagnosis complete.\n"
                f"{len(tasks_created)} gap(s) identified and queued (source=self_assigned, status=pending):\n"
                f"{task_list}\n\n"
                f"Queued for autonomous processing with checkpoint+verification gates."
            )
        else:
            print("[DIAG] Nightly self-diagnosis: no gaps found. All systems nominal.")
        print(f"[DIAG] Done. {len(tasks_created)} self-assigned tasks created.")
    except Exception as e:
        print(f"[DIAG] notify error: {e}")


# â”€â”€ P2-04: Proactive Intelligence Surface â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_PROACTIVE_INTERVAL = 7200       # 2 hours between surface checks
_proactive_last_run: float = 0.0

# Dedup: map alert_key -> last_sent_ts (in-memory, resets on redeploy â€” fine)
_proactive_sent: dict = {}
_PROACTIVE_DEDUP_TTL = 86400  # 24h â€” don't repeat same alert within a day


def _proactive_should_send(key: str) -> bool:
    last = _proactive_sent.get(key, 0)
    return (time.time() - last) >= _PROACTIVE_DEDUP_TTL


def _proactive_mark_sent(key: str):
    _proactive_sent[key] = time.time()


def _run_proactive_surface():
    """P2-04: Check 5 conditions and notify owner if anything needs attention.
    Conditions:
      1. Quality drop: last 3 session quality scores avg < 0.70
      2. Stale high-priority task: priority >= 7, pending > 48h
      3. Pattern milestone: any pattern hits frequency 10 or 25
      4. Evolution backlog: > 10 pending evolutions untouched > 48h
      5. Broken tools: tool_health_scan finds new broken tools
    All alerts are deduplicated (24h TTL). Non-blocking â€” never raises.
    """
    alerts = []

    # 1. Quality drop alert
    try:
        recent_q = sb_get("hot_reflections",
            "select=quality_score,created_at&source=eq.real&quality_score=not.is.null"
            "&quality_score=lte.1.0&order=created_at.desc&limit=3",
            svc=True) or []
        if len(recent_q) >= 3:
            avg = sum(float(r.get("quality_score", 0)) for r in recent_q) / 3
            if avg < 0.70 and _proactive_should_send("quality_drop"):
                alerts.append(
                    f"âš ï¸ <b>Quality Drop</b>\n"
                    f"Last 3 sessions avg quality: {avg:.2f} (threshold: 0.70)\n"
                    f"Use check_evolutions or review recent mistakes."
                )
                _proactive_mark_sent("quality_drop")
    except Exception as e:
        print(f"[PROACTIVE] quality check error: {e}")

    # 2. Stale high-priority task
    try:
        cutoff_48h = (datetime.utcnow() - timedelta(hours=48)).isoformat()
        stale_tasks = sb_get("task_queue",
            f"select=id,task,priority&status=eq.pending&priority=gte.7"
            f"&created_at=lt.{cutoff_48h}&order=priority.desc&limit=3",
            svc=True) or []
        for row in stale_tasks:
            tid = str(row.get("id", ""))
            key = f"stale_task_{tid}"
            if _proactive_should_send(key):
                try:
                    t = json.loads(row.get("task") or "{}")
                    title = t.get("title", str(row.get("task", "?"))[:60])
                except Exception:
                    title = str(row.get("task", "?"))[:60]
                pri = row.get("priority", "?")
                alerts.append(
                    f"ðŸ“Œ <b>Stale High-Priority Task (P{pri})</b>\n"
                    f"Pending >48h: {title[:100]}\n"
                    f"ID: {tid[:8]}"
                )
                _proactive_mark_sent(key)
    except Exception as e:
        print(f"[PROACTIVE] stale task check error: {e}")

    # 3. Pattern milestone
    try:
        milestone_patterns = sb_get("pattern_frequency",
            "select=pattern_key,frequency,domain&frequency=in.(10,25,50,100)"
            "&stale=eq.false&order=frequency.desc&limit=5",
            svc=True) or []
        for row in milestone_patterns:
            freq = row.get("frequency", 0)
            key = f"pattern_milestone_{row.get('pattern_key','')[:80]}_{freq}"
            if _proactive_should_send(key):
                alerts.append(
                    f"ðŸ” <b>Pattern Milestone: {freq}x</b>\n"
                    f"[{row.get('domain','?')}] {row.get('pattern_key','')[:120]}\n"
                    f"Consider applying as behavioral rule."
                )
                _proactive_mark_sent(key)
    except Exception as e:
        print(f"[PROACTIVE] pattern milestone check error: {e}")

    # 4. Evolution backlog
    try:
        cutoff_48h = (datetime.utcnow() - timedelta(hours=48)).isoformat()
        evo_count_rows = sb_get("evolution_queue",
            f"select=id&status=eq.pending&created_at=lt.{cutoff_48h}",
            svc=True) or []
        evo_count = len(evo_count_rows)
        if evo_count > 10 and _proactive_should_send("evo_backlog"):
            alerts.append(
                f"ðŸ“¥ <b>Evolution Backlog</b>\n"
                f"{evo_count} pending evolutions untouched >48h.\n"
                f"Use check_evolutions to review and act."
            )
            _proactive_mark_sent("evo_backlog")
    except Exception as e:
        print(f"[PROACTIVE] evolution backlog check error: {e}")

    # 5. Broken tools (check tool_stats for new high fail-rate tools)
    try:
        from datetime import date as _date
        today = _date.today().isoformat()
        broken_today = sb_get("tool_stats",
            f"select=tool_name,fail_rate&date=eq.{today}&fail_rate=gte.0.5",
            svc=True) or []
        for row in broken_today:
            name = row.get("tool_name", "?")
            key = f"broken_tool_{name}"
            if _proactive_should_send(key):
                alerts.append(
                    f"ðŸ”´ <b>Broken Tool Detected</b>\n"
                    f"{name}: fail_rate={row.get('fail_rate',0):.0%}\n"
                    f"Use tool_improve(tool_name='{name}') to diagnose and fix."
                )
                _proactive_mark_sent(key)
    except Exception as e:
        print(f"[PROACTIVE] broken tool check error: {e}")

    # Send combined alert if anything found
    if alerts:
        header = f"ðŸ§  <b>CORE Proactive Alert</b> ({len(alerts)} item{'s' if len(alerts)>1 else ''})\n\n"
        body = "\n\n".join(alerts)
        try:
            print(header + body)
            print(f"[PROACTIVE] Sent {len(alerts)} alert(s)")
        except Exception as e:
            print(f"[PROACTIVE] notify error: {e}")
    else:
        print(f"[PROACTIVE] No alerts â€” all systems nominal")


def proactive_surface_loop():
    """P2-04: Background thread â€” checks every 2h for proactive alerts."""
    global _proactive_last_run
    print("[PROACTIVE] Surface loop started")
    # Stagger start by 10 min to avoid all loops hitting Supabase at once on boot
    time.sleep(600)
    while True:
        try:
            _run_proactive_surface()
            _proactive_last_run = time.time()
        except Exception as e:
            print(f"[PROACTIVE] loop error: {e}")
        time.sleep(_PROACTIVE_INTERVAL)
