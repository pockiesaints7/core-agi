"""scraper/knowledge/router.py
Dispatches a topic query to all requested source fetchers in parallel.
Returns flat list of RawSource dicts.
"""
import asyncio

from .sources import arxiv, docs, medium, reddit, hackernews, stackoverflow


async def route(topic: str, sources: list, max_per_source: int, since_days: int) -> list:
    """Dispatch to source fetchers in parallel via asyncio.gather.
    Exceptions from individual fetchers are caught and logged — never propagated.
    Returns flattened list of RawSource dicts from all successful fetchers.
    """
    # Coerce types — MCP dispatcher passes all args as strings
    max_per_source = int(max_per_source)
    since_days = int(since_days)

    task_map = {
        "arxiv":         lambda: arxiv.fetch(topic, max_per_source, since_days),
        "docs":          lambda: docs.fetch(topic, max_per_source),
        "medium":        lambda: medium.fetch(topic, max_per_source, since_days),
        "reddit":        lambda: reddit.fetch(topic, max_per_source, since_days),
        "hackernews":    lambda: hackernews.fetch(topic, max_per_source, since_days),
        "stackoverflow": lambda: stackoverflow.fetch(topic, max_per_source),
    }

    active = {k: v for k, v in task_map.items() if k in sources}
    tasks = [fn() for fn in active.values()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    flat = []
    for name, result in zip(active.keys(), results):
        if isinstance(result, Exception):
            print(f"[INGEST] {name} fetch failed: {result}")
        elif isinstance(result, list):
            flat.extend(result)

    return flat
