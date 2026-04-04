"""Knowledge ingestion pipeline entry point.

Imports are intentionally lazy so optional source adapters do not break callers
that only need shared storage/helpers.
"""
from __future__ import annotations


async def ingest_knowledge(
    topic: str,
    sources: list | None = None,
    max_per_source: int = 50,
    since_days: int = 7,
    full_refresh: bool = False,
) -> dict:
    """Fetch a topic from public sources and persist the distilled artifacts."""
    from .concept_extractor import extract_concepts
    from .deduplicator import deduplicate
    from .router import route
    from .storage import write_sources

    if sources is None:
        sources = ["arxiv", "docs", "medium", "reddit", "hackernews", "stackoverflow"]

    raw = await route(topic, sources, max_per_source, since_days)
    deduped = deduplicate(raw)
    inserted, updated = await write_sources(deduped, topic)
    concepts = await extract_concepts(deduped, topic)

    return {
        "topic": topic,
        "sources_used": sources,
        "raw_count": len(raw),
        "deduped_count": len(deduped),
        "records_inserted": inserted,
        "records_updated": updated,
        "concepts_found": len(concepts),
    }
