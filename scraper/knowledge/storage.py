"""scraper/knowledge/storage.py
Write ingested sources to kb_sources, kb_articles, kb_concepts tables.
Handles upsert-on-url-conflict for kb_sources (idempotent re-runs).
"""
import hashlib
import json
from datetime import datetime, timezone

import httpx
from core_config import SUPABASE_URL, _sbh


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


async def write_sources(sources: list, topic: str) -> tuple:
    """Upsert each RawSource into kb_sources + kb_articles.
    Returns (inserted_count, updated_count).
    """
    inserted = 0
    updated = 0
    now = datetime.now(timezone.utc).isoformat()

    for source in sources:
        content = source.get("full_content", "") or ""
        chash = _content_hash(content)
        source_topics = [
            str(item).strip()
            for item in (source.get("topics") or [topic])
            if str(item).strip()
        ] or [topic]

        # Upsert kb_sources on url conflict
        kb_source_row = {
            "url":              source.get("url", ""),
            "source_type":      source.get("source_type", ""),
            "source_platform":  source.get("source_platform", ""),
            "title":            source.get("title", ""),
            "author":           source.get("author", ""),
            "published_at":     source.get("published_at"),
            "ingested_at":      source.get("ingested_at") or now,
            "last_refreshed":   now,
            "content_hash":     chash,
            "engagement_score": source.get("engagement_score", 0),
            "raw_engagement":   source.get("raw_engagement", {}),
            "trust_level":      source.get("trust_level", 1),
            "topics":           list(dict.fromkeys(source_topics)),
            "status":           "active",
        }

        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/kb_sources",
            headers={**_sbh(svc=True), "Prefer": "resolution=merge-duplicates,return=representation"},
            json=kb_source_row,
            params={"on_conflict": "url"},
            timeout=15,
        )

        if r.status_code in (200, 201):
            rows = r.json()
            db_id = rows[0]["id"] if rows else None
            source["db_id"] = db_id
            if r.status_code == 201:
                inserted += 1
            else:
                updated += 1

            # Write kb_articles row if content present
            if content and db_id:
                article_row = {
                    "source_id":          db_id,
                    "full_content":       content[:50000],  # cap at 50k chars
                    "summary":            source.get("summary", ""),
                    "key_concepts":       source.get("key_concepts", []),
                    "code_snippets":      source.get("code_snippets", []),
                    "cited_references":   source.get("cited_references", []),
                    "questions_answered": source.get("questions_answered", []),
                    "consensus_level":    source.get("consensus_level", "opinion"),
                }
                httpx.post(
                    f"{SUPABASE_URL}/rest/v1/kb_articles",
                    headers={**_sbh(svc=True), "Prefer": "resolution=merge-duplicates,return=minimal"},
                    json=article_row,
                    params={"on_conflict": "source_id"},
                    timeout=15,
                )
        else:
            print(f"[STORAGE] write failed {r.status_code}: {r.text[:200]}")

    return inserted, updated
