"""scraper/knowledge/storage.py
Write ingested sources to kb_sources, kb_articles, kb_concepts tables.
Handles upsert-on-url-conflict for kb_sources (idempotent re-runs).
"""
import hashlib
from datetime import datetime, timezone

import httpx

from core_config import SUPABASE_URL, _sbh


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _write_article(source_id: str, article_row: dict) -> tuple[str | None, str | None]:
    """Insert or update a kb_articles row for a source_id.

    kb_articles currently has no UNIQUE constraint on source_id, so a standard
    upsert-on-conflict path is not available. We query first, then patch or
    insert explicitly so one-time seed runs remain idempotent against the live
    schema.
    """
    lookup = httpx.get(
        f"{SUPABASE_URL}/rest/v1/kb_articles",
        headers=_sbh(svc=True),
        params={"select": "id", "source_id": f"eq.{source_id}", "limit": "1"},
        timeout=15,
    )
    if lookup.status_code != 200:
        return None, f"lookup_failed:{lookup.status_code}:{lookup.text[:200]}"

    rows = lookup.json() if lookup.content else []
    if rows:
        response = httpx.patch(
            f"{SUPABASE_URL}/rest/v1/kb_articles",
            headers={**_sbh(svc=True), "Prefer": "return=minimal"},
            json=article_row,
            params={"source_id": f"eq.{source_id}"},
            timeout=15,
        )
        if response.status_code in (200, 204):
            return "updated", None
        return None, f"update_failed:{response.status_code}:{response.text[:200]}"

    response = httpx.post(
        f"{SUPABASE_URL}/rest/v1/kb_articles",
        headers={**_sbh(svc=True), "Prefer": "return=representation"},
        json={"source_id": source_id, **article_row},
        timeout=15,
    )
    if response.status_code in (200, 201):
        return "inserted", None
    return None, f"insert_failed:{response.status_code}:{response.text[:200]}"


async def write_sources(sources: list, topic: str) -> dict:
    """Upsert each RawSource into kb_sources + kb_articles.

    Returns a detailed write report so callers can distinguish source-metadata
    success from article-body success.
    """
    report = {
        "source_inserted": 0,
        "source_updated": 0,
        "source_errors": [],
        "article_inserted": 0,
        "article_updated": 0,
        "article_skipped": 0,
        "article_errors": [],
    }
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
                report["source_inserted"] += 1
            else:
                report["source_updated"] += 1

            # Write kb_articles row if content present
            if content and db_id:
                article_row = {
                    "full_content":       content[:50000],  # cap at 50k chars
                    "summary":            source.get("summary", ""),
                    "key_concepts":       source.get("key_concepts", []),
                    "code_snippets":      source.get("code_snippets", []),
                    "cited_references":   source.get("cited_references", []),
                    "questions_answered": source.get("questions_answered", []),
                    "consensus_level":    source.get("consensus_level", "opinion"),
                }
                outcome, error = _write_article(str(db_id), article_row)
                if outcome == "inserted":
                    report["article_inserted"] += 1
                elif outcome == "updated":
                    report["article_updated"] += 1
                else:
                    report["article_errors"].append({
                        "url": source.get("url", ""),
                        "error": error or "unknown_article_write_error",
                    })
            else:
                report["article_skipped"] += 1
        else:
            report["source_errors"].append({
                "url": source.get("url", ""),
                "error": f"{r.status_code}:{r.text[:200]}",
            })
            print(f"[STORAGE] write failed {r.status_code}: {r.text[:200]}")

    return report
