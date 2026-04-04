"""One-time trading seed runner for CORE AGI."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

from core_config import sb_get, sb_post, sb_upsert
from core_trading_specialization import (
    TRADING_KB_ENTRIES,
    TRADING_MISTAKE_ENTRIES,
    TRADING_RULES,
    build_trading_readiness,
)
from scraper.trading_knowledge import ingest_trading_knowledge


def _existing_values(table: str, select_cols: str, limit: int = 500) -> list[dict]:
    rows = sb_get(table, f"select={select_cols}&order=id.asc&limit={limit}", svc=True) or []
    return rows if isinstance(rows, list) else []


def seed_behavioral_rules() -> dict:
    existing = {
        (str(row.get("domain") or "").strip().lower(), str(row.get("pointer") or "").strip().lower())
        for row in _existing_values("behavioral_rules", "domain,pointer")
    }
    seeded = 0
    skipped = 0
    for rule in TRADING_RULES:
        key = (rule["domain"].lower(), rule["pointer"].lower())
        if key in existing:
            skipped += 1
            continue
        ok = sb_post("behavioral_rules", {
            "trigger": rule["trigger"],
            "pointer": rule["pointer"],
            "full_rule": rule["full_rule"],
            "domain": rule["domain"],
            "priority": rule["priority"],
            "confidence": rule["confidence"],
            "active": True,
            "tested": False,
            "source": "core_seed_trading_brain",
        })
        if ok:
            seeded += 1
            existing.add(key)
    return {"seeded": seeded, "skipped": skipped}


def seed_mistakes() -> dict:
    existing = {
        (str(row.get("domain") or "").strip().lower(), str(row.get("what_failed") or "").strip().lower())
        for row in _existing_values("mistakes", "domain,what_failed")
    }
    seeded = 0
    skipped = 0
    for mistake in TRADING_MISTAKE_ENTRIES:
        key = (mistake["domain"].lower(), mistake["what_failed"].lower())
        if key in existing:
            skipped += 1
            continue
        ok = sb_post("mistakes", {
            **mistake,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        if ok:
            seeded += 1
            existing.add(key)
    return {"seeded": seeded, "skipped": skipped}


def seed_curated_knowledge_base() -> dict:
    existing = {
        (str(row.get("domain") or "").strip().lower(), str(row.get("topic") or "").strip().lower())
        for row in _existing_values("knowledge_base", "domain,topic")
    }
    seeded = 0
    skipped = 0
    for entry in TRADING_KB_ENTRIES:
        key = (entry["domain"].lower(), entry["topic"].lower())
        if key in existing:
            skipped += 1
            continue
        ok = sb_upsert("knowledge_base", {
            "domain": entry["domain"],
            "topic": entry["topic"],
            "content": entry["content"],
            "instruction": entry["content"],
            "confidence": entry["confidence"],
            "source": entry["source"],
            "source_type": "seed",
            "source_ref": "phase1_trading_seed",
            "source_ts": datetime.now(timezone.utc).isoformat(),
            "tags": ["trading", "phase1", "seed"],
            "active": True,
        }, on_conflict="domain,topic")
        if ok:
            seeded += 1
            existing.add(key)
    return {"seeded": seeded, "skipped": skipped}


async def seed_external_knowledge(max_arxiv_per_query: int = 3, since_days: int = 3650) -> dict:
    return await ingest_trading_knowledge(
        max_arxiv_per_query=max_arxiv_per_query,
        since_days=since_days,
    )


def seed_trading_brain(max_arxiv_per_query: int = 3, since_days: int = 3650) -> dict:
    external = asyncio.run(seed_external_knowledge(
        max_arxiv_per_query=max_arxiv_per_query,
        since_days=since_days,
    ))
    rules = seed_behavioral_rules()
    mistakes = seed_mistakes()
    curated_kb = seed_curated_knowledge_base()
    readiness = build_trading_readiness(limit=12)
    return {
        "ok": bool(readiness.get("ready")),
        "external_seed": external,
        "behavioral_rules": rules,
        "mistakes": mistakes,
        "curated_knowledge_base": curated_kb,
        "readiness": readiness,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed CORE AGI with external trading knowledge.")
    parser.add_argument("--max-arxiv-per-query", type=int, default=3, help="Max arXiv abstracts per trading query.")
    parser.add_argument("--since-days", type=int, default=3650, help="Recency horizon for research scoring.")
    args = parser.parse_args()

    result = seed_trading_brain(
        max_arxiv_per_query=max(1, args.max_arxiv_per_query),
        since_days=max(30, args.since_days),
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
