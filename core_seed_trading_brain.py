"""One-time production-grade trading seed runner for CORE AGI."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone

from core_config import sb_get, sb_patch, sb_post, sb_upsert
from core_trading_corpus import TRADING_CORPUS_STATS
from core_trading_specialization import (
    TRADING_DOMAIN,
    TRADING_KB_ENTRIES,
    TRADING_META_DOMAIN,
    TRADING_META_KB_ENTRIES,
    TRADING_MISTAKE_ENTRIES,
    TRADING_READINESS_TARGETS,
    TRADING_RULES,
    TRADING_SEED_HOT_REFLECTIONS,
    build_trading_readiness,
)
from scraper.trading_knowledge import (
    ARXIV_QUERY_PLAN,
    CURATED_PDF_SOURCES,
    CURATED_WEB_SOURCES,
    TRADING_CONCEPTS,
    ingest_trading_knowledge,
)

SEED_SOURCE = "core_seed_trading_brain"
SEED_SESSION_MARKERS = [
    {
        "summary": "[state_update] trading_seed_doctrine_installed",
        "actions": [
            "deterministic trading doctrine seeded",
            "runtime truth tables intentionally left empty",
        ],
        "interface": "seed",
    },
    {
        "summary": "[state_update] trading_seed_runtime_boundary_preserved",
        "actions": [
            "no synthetic trading_decisions inserted",
            "no synthetic trading_positions inserted",
            "no synthetic trading_mistakes inserted",
            "no synthetic trading_patterns inserted",
        ],
        "interface": "seed",
    },
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _existing_values(table: str, select_cols: str, limit: int = 500) -> list[dict]:
    rows = sb_get(table, f"select={select_cols}&order=id.asc&limit={limit}", svc=True) or []
    return rows if isinstance(rows, list) else []


def _seed_kb_entries(entries: list[dict], source_ref: str, tag_roots: list[str]) -> dict:
    existing = {
        (str(row.get("domain") or "").strip().lower(), str(row.get("topic") or "").strip().lower())
        for row in _existing_values("knowledge_base", "domain,topic", limit=1000)
    }
    seeded = 0
    refreshed = 0
    errors: list[str] = []
    for entry in entries:
        topic = str(entry.get("topic") or "").strip()
        domain = str(entry.get("domain") or "").strip()
        tags = list(dict.fromkeys([*tag_roots, domain, _slug(topic)]))
        key = (domain.lower(), topic.lower())
        ok = sb_upsert(
            "knowledge_base",
            {
                "domain": domain,
                "topic": topic,
                "content": entry["content"],
                "instruction": entry["content"],
                "confidence": entry["confidence"],
                "source": entry.get("source", SEED_SOURCE),
                "source_type": "seed",
                "source_ref": source_ref,
                "source_ts": _now_iso(),
                "tags": tags,
                "active": True,
            },
            on_conflict="domain,topic",
        )
        if not ok:
            errors.append(topic)
            continue
        if key in existing:
            refreshed += 1
        else:
            seeded += 1
            existing.add(key)
    return {"seeded": seeded, "refreshed": refreshed, "errors": errors[:10]}


def seed_behavioral_rules() -> dict:
    existing_rows = {
        (
            str(row.get("domain") or "").strip().lower(),
            str(row.get("pointer") or "").strip().lower(),
        ): row
        for row in _existing_values("behavioral_rules", "id,domain,pointer", limit=500)
    }
    seeded = 0
    refreshed = 0
    errors: list[str] = []
    for rule in TRADING_RULES:
        key = (rule["domain"].lower(), rule["pointer"].lower())
        payload = {
            "trigger": rule["trigger"],
            "pointer": rule["pointer"],
            "full_rule": rule["full_rule"],
            "domain": rule["domain"],
            "priority": rule["priority"],
            "confidence": rule["confidence"],
            "active": True,
            "tested": False,
            "source": SEED_SOURCE,
        }
        existing = existing_rows.get(key)
        ok = sb_patch("behavioral_rules", f"id=eq.{existing['id']}", payload) if existing else sb_post("behavioral_rules", payload)
        if not ok:
            errors.append(rule["pointer"])
            continue
        if existing:
            refreshed += 1
        else:
            seeded += 1
    return {"seeded": seeded, "refreshed": refreshed, "errors": errors[:10]}


def seed_mistakes() -> dict:
    existing_rows = {
        (
            str(row.get("domain") or "").strip().lower(),
            str(row.get("what_failed") or "").strip().lower(),
        ): row
        for row in _existing_values("mistakes", "id,domain,what_failed", limit=500)
    }
    seeded = 0
    refreshed = 0
    errors: list[str] = []
    for mistake in TRADING_MISTAKE_ENTRIES:
        key = (mistake["domain"].lower(), mistake["what_failed"].lower())
        payload = {
            **mistake,
            "created_at": _now_iso(),
        }
        existing = existing_rows.get(key)
        ok = sb_patch("mistakes", f"id=eq.{existing['id']}", payload) if existing else sb_post("mistakes", payload)
        if not ok:
            errors.append(mistake["what_failed"][:80])
            continue
        if existing:
            refreshed += 1
        else:
            seeded += 1
    return {"seeded": seeded, "refreshed": refreshed, "errors": errors[:10]}


def seed_curated_knowledge_base() -> dict:
    trading = _seed_kb_entries(
        TRADING_KB_ENTRIES,
        source_ref="trading_curated_seed",
        tag_roots=["trading", "seed", "curated"],
    )
    trading_meta = _seed_kb_entries(
        TRADING_META_KB_ENTRIES,
        source_ref="trading_meta_seed",
        tag_roots=["trading", "seed", "meta"],
    )
    return {
        "trading": trading,
        "trading_meta": trading_meta,
        "seeded": trading["seeded"] + trading_meta["seeded"],
        "refreshed": trading["refreshed"] + trading_meta["refreshed"],
        "errors": (trading["errors"] + trading_meta["errors"])[:10],
    }


def seed_trading_concepts() -> dict:
    existing_concepts = {
        str(row.get("concept_name") or "").strip().lower()
        for row in _existing_values("kb_concepts", "concept_name", limit=500)
    }
    concept_seeded = 0
    concept_refreshed = 0
    concept_errors: list[str] = []

    concept_card_entries: list[dict] = []
    for concept_name, meta in TRADING_CONCEPTS.items():
        ok = sb_upsert(
            "kb_concepts",
            {
                "concept_name": concept_name,
                "category": meta["category"],
                "definition": meta["definition"],
                "best_source_id": None,
                "source_count": 0,
                "avg_engagement": 0,
                "first_seen": _now_iso(),
                "trend": "seeded",
                "related_concepts": meta["related"],
                "implementations": meta["implementations"],
            },
            on_conflict="concept_name",
        )
        if not ok:
            concept_errors.append(concept_name)
            continue
        if concept_name.lower() in existing_concepts:
            concept_refreshed += 1
        else:
            concept_seeded += 1
            existing_concepts.add(concept_name.lower())

        concept_card_entries.append(
            {
                "domain": TRADING_DOMAIN,
                "topic": f"trading_seed_card_{_slug(concept_name)}",
                "content": (
                    f"{meta['definition']} Related concepts: {', '.join(meta['related'])}. "
                    f"Operational implementations: {', '.join(meta['implementations'])}."
                ),
                "confidence": "high",
                "source": SEED_SOURCE,
            }
        )

    knowledge_cards = _seed_kb_entries(
        concept_card_entries,
        source_ref="trading_concept_seed_cards",
        tag_roots=["trading", "seed", "concept_card"],
    )
    return {
        "concept_rows_seeded": concept_seeded,
        "concept_rows_refreshed": concept_refreshed,
        "knowledge_cards": knowledge_cards,
        "errors": concept_errors[:10],
    }


def seed_hot_reflections() -> dict:
    existing = {
        (
            str(row.get("domain") or "").strip().lower(),
            str(row.get("source") or "").strip().lower(),
            str(row.get("task_summary") or "").strip().lower(),
        )
        for row in _existing_values("hot_reflections", "domain,source,task_summary", limit=500)
    }
    seeded = 0
    skipped = 0
    errors: list[str] = []
    for reflection in TRADING_SEED_HOT_REFLECTIONS:
        key = (
            reflection["domain"].lower(),
            str(reflection.get("source") or "").lower(),
            reflection["task_summary"].lower(),
        )
        if key in existing:
            skipped += 1
            continue
        ok = sb_post(
            "hot_reflections",
            {
                **reflection,
            },
        )
        if not ok:
            errors.append(reflection["task_summary"])
            continue
        seeded += 1
        existing.add(key)
    return {"seeded": seeded, "skipped": skipped, "errors": errors[:10]}


def seed_session_markers() -> dict:
    existing = {
        str(row.get("summary") or "").strip().lower()
        for row in _existing_values("sessions", "summary", limit=300)
    }
    seeded = 0
    skipped = 0
    errors: list[str] = []
    for marker in SEED_SESSION_MARKERS:
        key = marker["summary"].lower()
        if key in existing:
            skipped += 1
            continue
        ok = sb_post("sessions", marker)
        if not ok:
            errors.append(marker["summary"])
            continue
        seeded += 1
        existing.add(key)
    return {"seeded": seeded, "skipped": skipped, "errors": errors[:10]}


async def seed_external_knowledge(max_arxiv_per_query: int = 0, since_days: int = 3650) -> dict:
    try:
        result = await ingest_trading_knowledge(
            max_arxiv_per_query=max_arxiv_per_query,
            since_days=since_days,
        )
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def build_seed_plan(max_arxiv_per_query: int) -> dict:
    curated_source_budget = len(CURATED_WEB_SOURCES) + len(CURATED_PDF_SOURCES)
    research_budget = len(ARXIV_QUERY_PLAN) * max(0, int(max_arxiv_per_query))
    deterministic_kb_rows = (
        len(TRADING_KB_ENTRIES)
        + len(TRADING_META_KB_ENTRIES)
        + len(TRADING_CONCEPTS)
    )
    return {
        "principles": [
            "Seed priors, doctrine, concepts, and review scaffolding only.",
            "Do not fabricate live trading outcomes or PnL history.",
            "Reruns refresh deterministic doctrine instead of duplicating it.",
            "External research augments the seed but does not replace deterministic coverage.",
        ],
        "expected_injection": {
            "behavioral_rules": {
                "exact_seed_rows": len(TRADING_RULES),
                "generated_corpus_rules": TRADING_CORPUS_STATS["generated_rules"],
                "generated_anti_rules": TRADING_CORPUS_STATS["generated_anti_rules"],
                "keys": [rule["pointer"] for rule in TRADING_RULES],
            },
            "mistakes": {
                "exact_seed_rows": len(TRADING_MISTAKE_ENTRIES),
                "keys": [entry["what_failed"][:80] for entry in TRADING_MISTAKE_ENTRIES],
            },
            "knowledge_base": {
                "exact_seed_rows": deterministic_kb_rows,
                "scenario_and_failure_cards": TRADING_CORPUS_STATS["scenario_cards"],
                "source_catalog_cards": TRADING_CORPUS_STATS["source_catalog_cards"],
                "trading_topics": [entry["topic"] for entry in TRADING_KB_ENTRIES[:12]],
                "trading_meta_topics": [entry["topic"] for entry in TRADING_META_KB_ENTRIES],
                "concept_seed_cards": [f"trading_seed_card_{_slug(name)}" for name in list(TRADING_CONCEPTS.keys())[:12]],
            },
            "kb_concepts": {
                "exact_seed_rows": len(TRADING_CONCEPTS),
                "keys": list(TRADING_CONCEPTS.keys()),
            },
            "kb_sources": {
                "curated_source_budget": curated_source_budget,
                "research_query_budget": research_budget,
                "expected_behavior": "external fetch plus dedupe; actual rows depend on reachability and relevance",
            },
            "kb_articles": {
                "expected_behavior": "should track successful kb_sources writes with full content bodies",
                "critical_requirement": "article rows must be non-zero when sources are fetched",
            },
            "hot_reflections": {
                "exact_seed_rows": len(TRADING_SEED_HOT_REFLECTIONS),
                "keys": [entry["task_summary"] for entry in TRADING_SEED_HOT_REFLECTIONS],
            },
            "sessions": {
                "exact_seed_rows": len(SEED_SESSION_MARKERS),
                "keys": [entry["summary"] for entry in SEED_SESSION_MARKERS],
            },
            "pattern_frequency": {
                "exact_seed_rows": 0,
                "reason": "observed patterns should come from cold processing or live/runtime evidence only",
            },
            "trading_patterns": {
                "exact_seed_rows": 0,
                "reason": "runtime truth only",
            },
            "trading_mistakes": {
                "exact_seed_rows": 0,
                "reason": "runtime truth only",
            },
            "trading_decisions": {
                "exact_seed_rows": 0,
                "reason": "runtime truth only",
            },
            "trading_positions": {
                "exact_seed_rows": 0,
                "reason": "runtime truth only",
            },
        },
        "readiness_targets": dict(TRADING_READINESS_TARGETS),
    }


def seed_trading_brain(max_arxiv_per_query: int = 0, since_days: int = 3650, plan_only: bool = False) -> dict:
    plan = build_seed_plan(max_arxiv_per_query=max_arxiv_per_query)
    if plan_only:
        return {"ok": True, "plan_only": True, "plan": plan}

    concept_seed = seed_trading_concepts()
    rules = seed_behavioral_rules()
    mistakes = seed_mistakes()
    curated_kb = seed_curated_knowledge_base()
    reflections = seed_hot_reflections()
    sessions = seed_session_markers()
    external = asyncio.run(
        seed_external_knowledge(
            max_arxiv_per_query=max_arxiv_per_query,
            since_days=since_days,
        )
    )
    readiness = build_trading_readiness(limit=20)

    deterministic_seed = {
        "kb_concepts": concept_seed,
        "behavioral_rules": rules,
        "mistakes": mistakes,
        "curated_knowledge_base": curated_kb,
        "hot_reflections": reflections,
        "sessions": sessions,
    }
    return {
        "ok": bool(readiness.get("ready")),
        "plan": plan,
        "deterministic_seed": deterministic_seed,
        "external_seed": external,
        "actual_after_seed": readiness.get("counts", {}),
        "readiness": readiness,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed CORE AGI with production-grade trading knowledge.")
    parser.add_argument("--max-arxiv-per-query", type=int, default=0, help="Max arXiv abstracts per trading query. Use 0 to skip research fetches.")
    parser.add_argument("--since-days", type=int, default=3650, help="Recency horizon for research scoring.")
    parser.add_argument("--plan-only", action="store_true", help="Print the seed manifest without writing any rows.")
    args = parser.parse_args()

    result = seed_trading_brain(
        max_arxiv_per_query=max(0, args.max_arxiv_per_query),
        since_days=max(30, args.since_days),
        plan_only=bool(args.plan_only),
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
