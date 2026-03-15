"""scraper/knowledge/sources/medium.py
Fetch articles from Medium (RSS), Dev.to (public API), Substack (RSS).
No API key required.

Medium:  RSS https://medium.com/feed/tag/{tag}
Dev.to:  GET https://dev.to/api/articles?tag=ai&per_page=50&sort=popularity
Substack: RSS https://{publication}.substack.com/feed
"""
import httpx

MEDIUM_TAGS  = ["ai-agents", "llm", "machine-learning", "artificial-intelligence"]
DEVTO_TAGS   = ["ai", "llm", "machinelearning"]


async def fetch(topic: str, max_results: int = 50, since_days: int = 7) -> list:
    """Fetch articles from Medium RSS + Dev.to API + Substack RSS matching topic.
    Returns list of RawSource dicts.
    TODO (22.B3): implement RSS parsing via feedparser + Dev.to API.
    """
    # STUB — implementation in 22.B3
    print(f"[MEDIUM] fetch stub called: topic={topic} max={max_results} since={since_days}d")
    return []
