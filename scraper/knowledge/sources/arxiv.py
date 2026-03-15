"""scraper/knowledge/sources/arxiv.py
Fetch papers from ArXiv public API + Semantic Scholar citation counts.
No API key required.

API: http://export.arxiv.org/api/query
Enrichment: https://api.semanticscholar.org/graph/v1/paper/arXiv:{id}
"""
import httpx
from datetime import datetime, timezone, timedelta
from ..scorer import compute_engagement_score

ARXIV_API = "http://export.arxiv.org/api/query"
SS_API    = "https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}"


async def fetch(topic: str, max_results: int = 50, since_days: int = 7) -> list:
    """Fetch papers matching topic from ArXiv, enrich with citation counts.
    Returns list of RawSource dicts.
    TODO (22.B1): implement full fetch + Semantic Scholar enrichment.
    """
    # STUB — implementation in 22.B1
    print(f"[ARXIV] fetch stub called: topic={topic} max={max_results} since={since_days}d")
    return []
