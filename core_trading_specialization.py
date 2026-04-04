"""Trading-only specialization helpers for phase-1 CORE AGI rollout."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from core_config import (
    CORE_ENABLE_GENERIC_PUBLIC_INGEST,
    CORE_ENABLE_GENERIC_RESEARCH_AUTONOMY,
    CORE_SPECIALIZATION,
    CORE_TRAINING_MODE,
    sb_get,
)

TRADING_DOMAIN = "trading"
TRADING_META_DOMAIN = "trading_meta"
UNIVERSAL_DOMAIN = "universal"

_TRUE_SET = {"1", "true", "yes", "on"}
_TRADING_TASK_KEYWORDS = {
    "trading",
    "trade",
    "paper",
    "portfolio",
    "position",
    "signal",
    "strategy",
    "regime",
    "risk",
    "funding",
    "carry",
    "correlation",
    "execution",
    "slippage",
    "drawdown",
    "market",
    "symbol",
    "backtest",
    "matrix",
    "btc",
    "eth",
    "sol",
    "bnb",
}

TRADING_RULES = [
    {
        "trigger": "before_entry",
        "pointer": "check_regime_before_entry",
        "full_rule": (
            "Never enter a directional trade when market_classifier returns CHOP. "
            "CHOP means ATR is compressed and breakout expectancy is weak. "
            "Wait for TREND, RANGE, or EXPANSION-specific confirmation before taking risk."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.97,
    },
    {
        "trigger": "before_entry",
        "pointer": "check_bias_alignment",
        "full_rule": (
            "Never open a LONG when higher-timeframe bias is BEAR with confidence >= 0.80, "
            "and never open a SHORT when higher-timeframe bias is BULL with confidence >= 0.80. "
            "Bias blocks override narrative confidence."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.95,
    },
    {
        "trigger": "sizing_trade",
        "pointer": "risk_first_sizing_from_atr",
        "full_rule": (
            "Position size must come from ATR-based stop distance, never conviction. "
            "Capital at risk per trade stays within 0.25-0.75 percent, "
            "and single-direction allocation must remain bounded even when confidence is high."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.96,
    },
    {
        "trigger": "funding_harvest_decision",
        "pointer": "funding_rate_threshold",
        "full_rule": (
            "Only open funding-harvest positions when funding is clearly positive and persistent. "
            "Exit immediately when funding weakens materially or turns negative."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 2,
        "confidence": 0.98,
    },
    {
        "trigger": "after_3_consecutive_losses",
        "pointer": "circuit_breaker_pause",
        "full_rule": (
            "After three consecutive losses or a severe daily drawdown, pause trading and require human review. "
            "No automatic revenge trading or confidence escalation is allowed."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.99,
    },
    {
        "trigger": "selecting_strategy",
        "pointer": "candidate_menu_only",
        "full_rule": (
            "Only select from the deterministic candidate menu produced by the strategy engine. "
            "If no candidate clears the minimum setup threshold, return strategy=nothing."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.97,
    },
    {
        "trigger": "directional_crowding",
        "pointer": "max_same_symbol_strategy_pair",
        "full_rule": (
            "Never open the exact same symbol plus strategy pair twice. "
            "Manage correlated crowding across BTC, ETH, SOL, and BNB before adding exposure."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.98,
    },
]

TRADING_KB_ENTRIES = [
    {
        "domain": TRADING_DOMAIN,
        "topic": "regime_strategy_matrix",
        "content": (
            "Regime strategy matrix. CHOP: no directional entries. RANGE: funding harvest first, "
            "then range reversion at extremes. TREND: trend breakout and pullback dominate. "
            "EXPANSION: prefer short-horizon scalp logic with tight risk. "
            "Higher-timeframe bias blocks the opposite direction when confidence is high."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "funding_harvest_mechanics",
        "content": (
            "Funding harvest is a carry trade that requires positive funding, low directional drift, "
            "and clean hedge management. It fails quickly when funding compresses, flips negative, "
            "or volatility expands faster than the hedge can stay neutral."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "chop_regime_loss_pattern",
        "content": (
            "CHOP is the most common source of repeated momentum losses. "
            "Compressed ATR and flat structure produce fake breaks and low reward-to-risk. "
            "The correct default in CHOP is patience, not creativity."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "paper_trading_graduation_path",
        "content": (
            "Promotion from paper to live requires statistical evidence, regime coverage, "
            "controlled drawdown, and operational discipline. "
            "A small winning streak is not enough to justify live exposure."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
]

TRADING_MISTAKE_ENTRIES = [
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Directional entry attempted in CHOP instead of standing down.",
        "root_cause": "Regime compression was ignored and breakout expectancy was overstated.",
        "correct_approach": "Treat CHOP as a hard no-trade state for momentum entries until structure changes.",
        "how_to_avoid": "Gate all directional trades through regime validation before sizing or narrative review.",
        "severity": "high",
        "context": "seed: chop directional loss",
        "tags": ["seed", "regime", "chop", "directional"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Trade direction conflicted with higher-timeframe bias.",
        "root_cause": "Lower-timeframe setup was treated as sufficient despite strong top-down opposition.",
        "correct_approach": "Respect high-confidence higher-timeframe bias as a hard directional constraint.",
        "how_to_avoid": "Block opposing entries when higher-timeframe bias confidence is elevated.",
        "severity": "high",
        "context": "seed: htf bias conflict",
        "tags": ["seed", "bias", "htf", "direction"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Position size expanded because conviction was high rather than ATR risk being acceptable.",
        "root_cause": "Sizing discipline was replaced by narrative confidence.",
        "correct_approach": "Use ATR stop distance and risk budget to set size before evaluating conviction.",
        "how_to_avoid": "Reject any position whose size cannot be justified by risk-first sizing math.",
        "severity": "critical",
        "context": "seed: oversizing by conviction",
        "tags": ["seed", "risk", "sizing", "atr"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Funding harvest was opened with weak or deteriorating funding support.",
        "root_cause": "Carry yield quality was not verified before entry.",
        "correct_approach": "Require strong positive funding and exit as soon as funding weakens materially.",
        "how_to_avoid": "Treat negative or fading funding as an immediate invalidation of the carry thesis.",
        "severity": "high",
        "context": "seed: weak funding harvest",
        "tags": ["seed", "funding", "carry", "yield"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Duplicate symbol plus strategy exposure was allowed.",
        "root_cause": "Exposure accounting focused on entries individually instead of the portfolio state.",
        "correct_approach": "Prevent duplicate symbol-strategy pairs and review crowding before every add.",
        "how_to_avoid": "Check open exposure inventory before approving any new trade.",
        "severity": "medium",
        "context": "seed: duplicate exposure",
        "tags": ["seed", "portfolio", "exposure", "crowding"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Portfolio became crowded with correlated directional risk across majors.",
        "root_cause": "Correlation guard was weaker than single-trade conviction.",
        "correct_approach": "Size and gate entries at the portfolio level, not just trade level.",
        "how_to_avoid": "Review BTC, ETH, SOL, and BNB correlation before adding another directional trade.",
        "severity": "high",
        "context": "seed: correlated crowding",
        "tags": ["seed", "correlation", "portfolio", "risk"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Carry or reversion logic was used during an expansion regime.",
        "root_cause": "Regime-specific playbook was ignored when volatility expanded.",
        "correct_approach": "Use expansion-specific logic with smaller size, tighter stops, and shorter hold time.",
        "how_to_avoid": "Map each strategy family to valid regimes before entry.",
        "severity": "high",
        "context": "seed: regime strategy mismatch",
        "tags": ["seed", "regime", "expansion", "strategy"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Paper-trading results were promoted to live expectations too early.",
        "root_cause": "Short sample performance was mistaken for robust edge.",
        "correct_approach": "Require sample size, drawdown control, and multi-regime evidence before graduation.",
        "how_to_avoid": "Treat paper-to-live promotion as a gated risk decision, not a morale milestone.",
        "severity": "critical",
        "context": "seed: paper graduation overreach",
        "tags": ["seed", "paper", "live", "graduation"],
    },
]

TRADING_RARL_GOALS = [
    ("Improve regime classification robustness so trading logic stands down faster in ambiguous structure.", "regime_classification"),
    ("Improve strategy-family gating quality so each strategy fires only in the regimes it was built for.", "strategy_gating"),
    ("Improve risk sizing and stop logic so capital at risk stays stable across volatility changes.", "risk_sizing"),
    ("Improve funding and carry entry-exit logic so yield is harvested only when the carry thesis is durable.", "funding_logic"),
    ("Improve correlation and crowding protection across the portfolio before new exposure is added.", "correlation_guard"),
    ("Improve execution and slippage handling so realized outcomes stay close to planned outcomes.", "execution"),
    ("Improve paper-to-live graduation policy so promotion requires durable evidence instead of optimism.", "paper_graduation"),
    ("Improve decision calibration so confidence and action intensity match the actual edge quality.", "decision_calibration"),
]


def trading_specialization_enabled() -> bool:
    return CORE_SPECIALIZATION == "trading" or CORE_TRAINING_MODE == "trading_only"


def trading_training_only_enabled() -> bool:
    return CORE_TRAINING_MODE == "trading_only" or trading_specialization_enabled()


def allow_generic_public_ingest() -> bool:
    if trading_specialization_enabled():
        return CORE_ENABLE_GENERIC_PUBLIC_INGEST
    return True


def allow_generic_research_autonomy() -> bool:
    if trading_specialization_enabled():
        return CORE_ENABLE_GENERIC_RESEARCH_AUTONOMY
    return True


def training_meta_domain() -> str:
    return TRADING_META_DOMAIN if trading_specialization_enabled() else "meta"


def detect_trading_task_domain(task_json: str = "") -> str | None:
    text = str(task_json or "").lower()
    if any(keyword in text for keyword in _TRADING_TASK_KEYWORDS):
        return TRADING_DOMAIN
    return None


def _safe_rows(table: str, query: str) -> list[dict]:
    try:
        rows = sb_get(table, query, svc=True) or []
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _default_since_ts(days: int = 30) -> str:
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_trading_source_packet(since_ts: str | None = None, limit: int = 40) -> dict:
    since_ts = (since_ts or _default_since_ts()).strip()
    rules = _safe_rows(
        "behavioral_rules",
        "select=id,trigger,pointer,full_rule,domain,priority,confidence,source,created_at"
        f"&active=eq.true&domain=in.({UNIVERSAL_DOMAIN},{TRADING_DOMAIN})&order=priority.asc&limit={limit}",
    )
    kb = _safe_rows(
        "knowledge_base",
        "select=id,domain,topic,content,instruction,confidence,source,created_at,updated_at"
        f"&domain=eq.{TRADING_DOMAIN}&order=updated_at.desc&limit={limit}",
    )
    seed_sources = _safe_rows(
        "kb_sources",
        "select=id,url,title,source_type,source_platform,published_at,ingested_at,last_refreshed,trust_level,topics,engagement_score,status"
        f"&topics=cs.{{trading}}&order=last_refreshed.desc&limit={limit}",
    )
    external_concepts = _safe_rows(
        "kb_concepts",
        "select=id,concept_name,category,definition,source_count,avg_engagement,trend,related_concepts,implementations"
        f"&category=like.trading*&order=source_count.desc&limit={limit}",
    )
    memory_mistakes = _safe_rows(
        "mistakes",
        "select=id,domain,what_failed,root_cause,how_to_avoid,correct_approach,severity,created_at,context"
        f"&domain=eq.{TRADING_DOMAIN}&order=created_at.desc&limit={limit}",
    )
    hot_reflections = _safe_rows(
        "hot_reflections",
        "select=id,domain,task_summary,reflection_text,quality_score,source,created_at,new_patterns,new_mistakes,gaps_identified"
        f"&domain=eq.{TRADING_DOMAIN}&created_at=gte.{since_ts}&order=created_at.desc&limit={limit}",
    )
    trading_patterns = _safe_rows(
        "trading_patterns",
        "select=id,pattern_key,description,conditions,outcome,win_count,total_count,avg_pnl_usd,win_rate,last_seen,created_at"
        f"&order=created_at.desc&limit={limit}",
    )
    trading_mistakes = _safe_rows(
        "trading_mistakes",
        "select=id,position_id,decision_id,what_failed,market_context,root_cause,how_to_avoid,loss_usd,severity,created_at"
        f"&created_at=gte.{since_ts}&order=created_at.desc&limit={limit}",
    )
    trading_decisions = _safe_rows(
        "trading_decisions",
        "select=id,market_regime,strategy,symbol,direction,confidence,risk_level,reasoning,expected_pnl,action_taken,position_id,created_at,context_snapshot"
        f"&created_at=gte.{since_ts}&order=created_at.desc&limit={limit}",
    )
    trading_positions = _safe_rows(
        "trading_positions",
        "select=id,strategy,symbol,direction,capital_usd,status,opened_at,closed_at,total_funding_usd,realized_pnl_usd,close_reason,decision_id,notes"
        f"&order=id.desc&limit={limit}",
    )
    output_critiques = _safe_rows(
        "output_critiques",
        "select=id,session_id,source,output_text,score,verdict,failure_pattern,failure_category,reason,suggested_improvement,created_at"
        f"&source=eq.{TRADING_DOMAIN}&created_at=gte.{since_ts}&order=created_at.desc&limit={limit}",
    )
    causal_chains = _safe_rows(
        "causal_chains",
        "select=id,session_id,source,output_text,why_reasoning,root_knowledge,knowledge_source,reasoning_type,confidence,potential_bias,created_at"
        f"&source=eq.{TRADING_DOMAIN}&created_at=gte.{since_ts}&order=created_at.desc&limit={limit}",
    )
    output_reflections = _safe_rows(
        "output_reflections",
        "select=id,session_id,source,critique_score,verdict,gap,gap_domain,new_behavior,evo_worthy,prompt_patch,created_at"
        f"&source=eq.{TRADING_DOMAIN}&created_at=gte.{since_ts}&order=created_at.desc&limit={limit}",
    )

    closed_positions = [
        row for row in trading_positions
        if row.get("closed_at") or str(row.get("status") or "").lower() in {"closed", "complete", "completed"}
    ]
    reflection_count = len(output_critiques) + len(causal_chains) + len(output_reflections) + len(hot_reflections)
    source_seed_ready = len(seed_sources) >= 6 and len(external_concepts) >= 8
    seed_ready = len(rules) > 0 and len(kb) > 0 and len(memory_mistakes) > 0 and source_seed_ready
    fresh_signal_count = (
        len(seed_sources)
        + len(external_concepts)
        + len(trading_patterns)
        + len(trading_mistakes)
        + len(trading_decisions)
        + len(closed_positions)
        + reflection_count
    )

    return {
        "ok": True,
        "mode": "trading_only",
        "specialization": CORE_SPECIALIZATION,
        "training_mode": CORE_TRAINING_MODE,
        "since_ts": since_ts,
        "seed_ready": seed_ready,
        "fresh_signal_count": fresh_signal_count,
        "verified": seed_ready,
        "counts": {
            "rules": len(rules),
            "knowledge_base": len(kb),
            "seed_sources": len(seed_sources),
            "seed_concepts": len(external_concepts),
            "memory_mistakes": len(memory_mistakes),
            "hot_reflections": len(hot_reflections),
            "trading_patterns": len(trading_patterns),
            "trading_mistakes": len(trading_mistakes),
            "trading_decisions": len(trading_decisions),
            "trading_positions": len(trading_positions),
            "closed_positions": len(closed_positions),
            "output_critiques": len(output_critiques),
            "causal_chains": len(causal_chains),
            "output_reflections": len(output_reflections),
        },
        "tables": {
            "behavioral_rules": rules,
            "knowledge_base": kb,
            "kb_sources": seed_sources,
            "kb_concepts": external_concepts,
            "mistakes": memory_mistakes,
            "hot_reflections": hot_reflections,
            "trading_patterns": trading_patterns,
            "trading_mistakes": trading_mistakes,
            "trading_decisions": trading_decisions,
            "trading_positions": trading_positions,
            "closed_positions": closed_positions,
            "output_critiques": output_critiques,
            "causal_chains": causal_chains,
            "output_reflections": output_reflections,
        },
        "summary": (
            f"rules={len(rules)} kb={len(kb)} seed_sources={len(seed_sources)} "
            f"seed_concepts={len(external_concepts)} memory_mistakes={len(memory_mistakes)} "
            f"decisions={len(trading_decisions)} closed_positions={len(closed_positions)} "
            f"patterns={len(trading_patterns)} live_mistakes={len(trading_mistakes)} reflections={reflection_count}"
        ),
    }


def build_trading_readiness(limit: int = 12) -> dict:
    packet = build_trading_source_packet(limit=max(limit, 40))
    counts = packet.get("counts", {})
    blockers: list[str] = []
    if counts.get("seed_sources", 0) < 6:
        blockers.append("external_trading_seed_sources_below_target")
    if counts.get("seed_concepts", 0) < 8:
        blockers.append("external_trading_seed_concepts_below_target")
    if counts.get("rules", 0) == 0:
        blockers.append("behavioral_rules_missing")
    if counts.get("knowledge_base", 0) == 0:
        blockers.append("trading_knowledge_base_empty")
    if counts.get("memory_mistakes", 0) == 0:
        blockers.append("trading_mistake_memory_empty")
    return {
        "ok": len(blockers) == 0,
        "ready": len(blockers) == 0,
        "blockers": blockers,
        "counts": counts,
        "summary": packet.get("summary", ""),
        "sample_sources": packet.get("tables", {}).get("kb_sources", [])[:min(limit, 6)],
        "sample_concepts": packet.get("tables", {}).get("kb_concepts", [])[:min(limit, 8)],
        "sample_rules": packet.get("tables", {}).get("behavioral_rules", [])[:min(limit, 6)],
        "sample_kb": packet.get("tables", {}).get("knowledge_base", [])[:min(limit, 8)],
        "fresh_signal_count": packet.get("fresh_signal_count", 0),
    }


def build_trading_curriculum(limit: int = 16, packet: dict | None = None) -> dict:
    packet = packet or build_trading_source_packet(limit=max(limit, 40))
    tables = packet.get("tables", {})
    items: list[dict] = []

    for row in tables.get("trading_decisions", []):
        items.append({
            "work_track": "trading_decision",
            "title": f"{row.get('symbol', '?')} {row.get('strategy', '?')} {row.get('action_taken', '?')}",
            "description": (row.get("reasoning") or "")[:220],
            "status": row.get("action_taken") or "recorded",
            "result": f"regime={row.get('market_regime', '')} risk={row.get('risk_level', '')}",
            "source": "trading_decisions",
            "priority": row.get("confidence"),
        })
    for row in tables.get("closed_positions", []):
        items.append({
            "work_track": "trading_position",
            "title": f"{row.get('symbol', '?')} {row.get('strategy', '?')} closed",
            "description": (row.get("notes") or row.get("close_reason") or "")[:220],
            "status": row.get("status") or "closed",
            "result": f"pnl={row.get('realized_pnl_usd', 0)} funding={row.get('total_funding_usd', 0)}",
            "source": "trading_positions",
            "priority": row.get("realized_pnl_usd"),
        })
    for row in tables.get("trading_mistakes", []):
        items.append({
            "work_track": "trading_mistake",
            "title": (row.get("what_failed") or "trading mistake")[:160],
            "description": (row.get("root_cause") or row.get("market_context") or "")[:220],
            "status": row.get("severity") or "recorded",
            "result": (row.get("how_to_avoid") or "")[:220],
            "source": "trading_mistakes",
            "priority": row.get("loss_usd"),
        })
    for row in tables.get("trading_patterns", []):
        items.append({
            "work_track": "trading_pattern",
            "title": (row.get("pattern_key") or "trading_pattern")[:160],
            "description": (row.get("description") or "")[:220],
            "status": row.get("outcome") or "tracked",
            "result": f"win_rate={row.get('win_rate', 0)} avg_pnl={row.get('avg_pnl_usd', 0)}",
            "source": "trading_patterns",
            "priority": row.get("win_rate"),
        })
    for row in tables.get("output_reflections", []):
        items.append({
            "work_track": "trading_reflection",
            "title": (row.get("gap") or row.get("new_behavior") or "trading_reflection")[:160],
            "description": (row.get("prompt_patch") or "")[:220],
            "status": row.get("verdict") or "reflected",
            "result": (row.get("new_behavior") or "")[:220],
            "source": "output_reflections",
            "priority": row.get("critique_score"),
        })
    for row in tables.get("behavioral_rules", []):
        items.append({
            "work_track": "trading_rule",
            "title": (row.get("pointer") or row.get("trigger") or "trading_rule")[:160],
            "description": (row.get("full_rule") or "")[:220],
            "status": "active",
            "result": row.get("trigger") or "",
            "source": "behavioral_rules",
            "priority": row.get("priority"),
        })
    for row in tables.get("knowledge_base", []):
        items.append({
            "work_track": "trading_kb",
            "title": (row.get("topic") or "trading_kb")[:160],
            "description": (row.get("instruction") or row.get("content") or "")[:220],
            "status": row.get("confidence") or "seeded",
            "result": row.get("source") or "",
            "source": "knowledge_base",
            "priority": row.get("confidence"),
        })
    for row in tables.get("kb_concepts", []):
        items.append({
            "work_track": "trading_concept",
            "title": (row.get("concept_name") or "trading_concept")[:160],
            "description": (row.get("definition") or "")[:220],
            "status": row.get("trend") or "tracked",
            "result": f"sources={row.get('source_count', 0)} avg_eng={row.get('avg_engagement', 0)}",
            "source": "kb_concepts",
            "priority": row.get("source_count"),
        })
    for row in tables.get("kb_sources", []):
        items.append({
            "work_track": "trading_source",
            "title": (row.get("title") or row.get("url") or "trading_source")[:160],
            "description": (row.get("source_platform") or "")[:220],
            "status": row.get("source_type") or "seeded",
            "result": f"trust={row.get('trust_level', 0)} engagement={row.get('engagement_score', 0)}",
            "source": "kb_sources",
            "priority": row.get("engagement_score"),
        })
    for row in tables.get("mistakes", []):
        items.append({
            "work_track": "trading_memory",
            "title": (row.get("what_failed") or "trading_memory")[:160],
            "description": (row.get("root_cause") or "")[:220],
            "status": row.get("severity") or "seeded",
            "result": (row.get("how_to_avoid") or "")[:220],
            "source": "mistakes",
            "priority": row.get("severity"),
        })

    deduped: list[dict] = []
    seen = set()
    for item in items:
        key = (item.get("work_track"), item.get("title"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    counts = Counter(item.get("work_track") or "general" for item in deduped)
    return {"counts": dict(counts), "items": deduped, "source_packet": packet}
