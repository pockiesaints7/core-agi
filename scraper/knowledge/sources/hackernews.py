"""scraper/knowledge/sources/hackernews.py
Fetch posts + expert comments from Hacker News via Algolia search API.
No API key required.

Top posts: GET https://hn.algolia.com/api/v1/search?query={topic}&tags=story&numericFilters=points>50
Weekly:    GET https://hn.algolia.com/api/v1/search_by_date?query={topic}&tags=story&numericFilters=created_at_i>{unix}
Comments (points > 5) fetched separately and stored in questions_answered field.
"""
import httpx

HN_SEARCH_URL    = "https://hn.algolia.com/api/v1/search"
HN_DATE_URL      = "https://hn.algolia.com/api/v1/search_by_date"


async def fetch(topic: str, max_results: int = 50, since_days: int = 7) -> list:
    """Fetch HN stories + top comments matching topic.
    Returns list of RawSource dicts.
    TODO (22.B5): implement Algolia HN API fetch + comment extraction.
    """
    # STUB — implementation in 22.B5
    print(f"[HN] fetch stub called: topic={topic} max={max_results} since={since_days}d")
    return []
