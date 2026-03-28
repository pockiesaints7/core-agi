"""scraper/knowledge/sources/hackernews.py
Fetch HN posts and expert comments via Algolia HN Search API.
No API key required.
"""
import httpx
from datetime import datetime, timezone, timedelta
from ..scorer import compute_engagement_score

HN_SEARCH_URL      = "https://hn.algolia.com/api/v1/search"
HN_SEARCH_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM_URL        = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
MIN_POINTS         = 50
MIN_COMMENT_POINTS = 5


def _ts_to_iso(timestamp) -> str:
    try:
        if isinstance(timestamp, str):
            return timestamp
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _recency_score(created_at_iso: str, since_days: int = 7) -> float:
    try:
        pub = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_days = max(0, (now - pub).days)
        return max(0.0, 100.0 - (age_days / max(since_days, 1)) * 100)
    except Exception:
        return 50.0


def _is_relevant(title: str, story_text: str, topic_keywords: list) -> bool:
    combined = (title + " " + (story_text or "")).lower()
    return any(kw in combined for kw in topic_keywords)


async def _fetch_comments(story_kids: list, client: httpx.AsyncClient) -> str:
    """Fetch top-level comments for a story, return concatenated expert comments."""
    top_comments = []
    for kid_id in (story_kids or [])[:20]:
        try:
            r = await client.get(HN_ITEM_URL.format(id=kid_id), timeout=5)
            if r.status_code != 200:
                continue
            item = r.json()
            if item and item.get("type") == "comment":
                score = item.get("score", 0) or 0
                text  = item.get("text", "") or ""
                if score >= MIN_COMMENT_POINTS and text and "[dead]" not in text:
                    top_comments.append(text[:600])
        except Exception:
            continue
        if len(top_comments) >= 5:
            break
    return "\n\n---\n\n".join(top_comments)


async def fetch(topic: str, max_results: int = 50, since_days: int = 7) -> list:
    """Fetch HN stories + expert comments about topic.
    Returns list of RawSource dicts.
    """
    print(f"[HN] fetch: topic={topic} max={max_results} since={since_days}d")
    topic_keywords = [kw.lower() for kw in topic.split()]
    results = []

    since_unix = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp())

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            # Primary: search by relevance with min points filter
            r = await client.get(HN_SEARCH_URL, params={
                "query":          topic,
                "tags":           "story",
                "numericFilters": f"points>{MIN_POINTS}",
                "hitsPerPage":    min(max_results, 50),
            }, timeout=10)

            if r.status_code == 200:
                hits = r.json().get("hits", [])
                print(f"[HN] Search returned {len(hits)} hits")

                for hit in hits:
                    if len(results) >= max_results:
                        break
                    title       = hit.get("title", "") or ""
                    story_text  = hit.get("story_text", "") or ""
                    if not _is_relevant(title, story_text, topic_keywords):
                        continue

                    created_at = hit.get("created_at", "")
                    recency    = _recency_score(created_at, since_days)
                    points     = hit.get("points", 0) or 0
                    num_comments = hit.get("num_comments", 0) or 0

                    # Fetch expert comments from Firebase
                    kids = hit.get("children", [])
                    comments_text = await _fetch_comments(kids, client) if kids else ""

                    url = hit.get("url", "") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
                    full_content = f"{title}\n\n{story_text}"
                    if comments_text:
                        full_content += f"\n\n== TOP HN COMMENTS ==\n\n{comments_text}"

                    raw_engagement = {
                        "points":       points,
                        "num_comments": num_comments,
                        "recency":      recency,
                    }
                    engagement_score = compute_engagement_score(raw_engagement, "hackernews")

                    results.append({
                        "url":             url,
                        "source_type":     "hackernews",
                        "source_platform": "news.ycombinator.com",
                        "title":           title,
                        "author":          hit.get("author", ""),
                        "published_at":    created_at or datetime.now(timezone.utc).isoformat(),
                        "content_hash":    str(hit.get("objectID", "")),
                        "engagement_score": engagement_score,
                        "raw_engagement":  raw_engagement,
                        "trust_level":     3,
                        "topics":          [topic],
                        "status":          "active",
                        "full_content":    full_content[:5000],
                        "summary":         (story_text or title)[:500],
                        "key_concepts":    [],
                        "questions_answered": [],
                        "consensus_level": "debated",
                    })

        except Exception as e:
            print(f"[HN] Fetch error: {e}")

    print(f"[HN] Returning {len(results)} sources")
    return results
