"""scraper/knowledge/__init__.py
Knowledge ingestion pipeline — multi-source ingestion feeding hot_reflections.
Exports: ingest_knowledge
"""
from .router import route
from .storage import write_sources
from .deduplicator import deduplicate
from .concept_extractor import extract_concepts


async def ingest_knowledge(
    topic: str,
    sources: list = None,
    max_per_source: int = 50,
    since_days: int = 7,
    full_refresh: bool = False,
) -> dict:
    """Main entry point. Fetches topic from all requested sources,
    deduplicates, scores, extracts concepts, writes to kb_* tables,
    injects hot_reflections for cold processor pickup.
    Returns summary dict: {topic, sources_used, records_inserted, records_updated, concepts_found}
    """
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
