"""scraper/knowledge/sources/arxiv.py
Fetch papers from ArXiv public API + Semantic Scholar citation counts.
No API key required.

API: http://export.arxiv.org/api/query
Enrichment: https://api.semanticscholar.org/graph/v1/paper/arXiv:{id}
"""
import httpx
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from ..scorer import compute_engagement_score

ARXIV_API = "http://export.arxiv.org/api/query"
SS_API    = "https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}"
SS_FIELDS = "citationCount,influentialCitationCount,year"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def _parse_arxiv_id(entry_id: str) -> str:
    """Extract bare arxiv ID from full URL like http://arxiv.org/abs/2310.01234v2"""
    return entry_id.split("/abs/")[-1].split("v")[0]


def _parse_date(date_str: str) -> str:
    """Normalize arxiv date string to ISO format."""
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return dt.isoformat() + "+00:00"
    except Exception:
        return datetime.now(timezone.utc).isoformat()


async def _fetch_citations(arxiv_id: str, client: httpx.AsyncClient) -> dict:
    """Fetch citation count from Semantic Scholar. Returns {citations, influential_citations}."""
    try:
        url = SS_API.format(arxiv_id=arxiv_id) + f"?fields={SS_FIELDS}"
        r = await client.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return {
                "citations": data.get("citationCount", 0) or 0,
                "influential_citations": data.get("influentialCitationCount", 0) or 0,
            }
    except Exception:
        pass
    return {"citations": 0, "influential_citations": 0}


def _recency_score(published_iso: str, since_days: int) -> float:
    """Returns 0-100 recency score. Recent = high."""
    try:
        pub = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        age_days = max(0, (now - pub).days)
        return max(0.0, 100.0 - (age_days / max(since_days, 1)) * 100)
    except Exception:
        return 0.0


async def fetch(topic: str, max_results: int = 50, since_days: int = 7) -> list:
    """Fetch papers matching topic from ArXiv, enrich with citation counts.
    Returns list of RawSource dicts.
    """
    print(f"[ARXIV] fetch: topic={topic} max={max_results} since={since_days}d")

    params = {
        "search_query": f"ti:{topic} OR abs:{topic}",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": max_results,
    }

    results = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(ARXIV_API, params=params)
            r.raise_for_status()
            root = ET.fromstring(r.text)

            entries = root.findall("atom:entry", NS)
            print(f"[ARXIV] Got {len(entries)} entries from API")

            for entry in entries:
                try:
                    entry_id = entry.findtext("atom:id", default="", namespaces=NS)
                    arxiv_id = _parse_arxiv_id(entry_id)

                    title  = (entry.findtext("atom:title", default="", namespaces=NS) or "").strip().replace("\n", " ")
                    abstract = (entry.findtext("atom:summary", default="", namespaces=NS) or "").strip().replace("\n", " ")
                    published = entry.findtext("atom:published", default="", namespaces=NS) or ""

                    authors = [
                        a.findtext("atom:name", default="", namespaces=NS)
                        for a in entry.findall("atom:author", NS)
                    ]
                    author_str = ", ".join(authors[:3])
                    if len(authors) > 3:
                        author_str += " et al."

                    cite_data = await _fetch_citations(arxiv_id, client)
                    recency   = _recency_score(published, since_days)

                    raw_engagement = {
                        "citations":             cite_data["citations"],
                        "influential_citations": cite_data["influential_citations"],
                        "recency":               recency,
                    }
                    engagement_score = compute_engagement_score(raw_engagement, "arxiv")

                    source = {
                        "url":             f"https://arxiv.org/abs/{arxiv_id}",
                        "source_type":     "arxiv",
                        "source_platform": "arxiv.org",
                        "title":           title,
                        "author":          author_str,
                        "published_at":    _parse_date(published),
                        "content_hash":    arxiv_id,
                        "engagement_score": engagement_score,
                        "raw_engagement":  raw_engagement,
                        "trust_level":     4,
                        "topics":          [topic],
                        "status":          "active",
                        "full_content":    abstract,
                        "summary":         abstract[:500],
                        "key_concepts":    [],
                        "questions_answered": [],
                        "consensus_level": "established",
                    }
                    results.append(source)

                except Exception as e:
                    print(f"[ARXIV] Entry parse error: {e}")
                    continue

    except Exception as e:
        print(f"[ARXIV] Fetch error: {e}")

    print(f"[ARXIV] Returning {len(results)} sources")
    return results
