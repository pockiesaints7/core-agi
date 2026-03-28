"""scraper/knowledge/sources/medium.py
Fetch technical articles from Medium (RSS), Dev.to (API), and Substack (RSS).
No API key required.
"""
import httpx
import feedparser
from datetime import datetime, timezone, timedelta
from ..scorer import compute_engagement_score

DEVTO_API = "https://dev.to/api/articles"
MEDIUM_RSS_TAGS = [
    "https://medium.com/feed/tag/artificial-intelligence",
    "https://medium.com/feed/tag/ai-agents",
    "https://medium.com/feed/tag/llm",
    "https://medium.com/feed/tag/machine-learning",
]
SUBSTACK_FEEDS = [
    "https://www.latent.space/feed",
    "https://newsletter.theaiedge.io/feed",
    "https://www.humanloop.com/feed",
]


def _parse_feed_date(date_str: str) -> str:
    """Parse various date formats to ISO."""
    try:
        import email.utils
        dt = email.utils.parsedate_to_datetime(date_str)
        return dt.isoformat()
    except Exception:
        try:
            from dateutil import parser as dp
            return dp.parse(date_str).isoformat()
        except Exception:
            return datetime.now(timezone.utc).isoformat()


def _recency_score(published_iso: str, since_days: int = 7) -> float:
    try:
        pub = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        age_days = max(0, (now - pub).days)
        return max(0.0, 100.0 - (age_days / max(since_days, 1)) * 100)
    except Exception:
        return 50.0


def _is_relevant(title: str, summary: str, topic_keywords: list) -> bool:
    combined = (title + " " + summary).lower()
    return any(kw in combined for kw in topic_keywords)


async def _fetch_devto(topic: str, max_results: int, since_days: int) -> list:
    """Fetch from Dev.to public API."""
    results = []
    topic_kw = topic.replace(" ", ",")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(DEVTO_API, params={
                "tag": "ai",
                "per_page": max_results,
                "sort": "popularity",
            })
            if r.status_code != 200:
                return []
            articles = r.json()
            topic_keywords = [kw.lower() for kw in topic.split()]

            for a in articles:
                if not _is_relevant(a.get("title",""), a.get("description",""), topic_keywords):
                    continue
                published = a.get("published_at", datetime.now(timezone.utc).isoformat())
                recency   = _recency_score(published, since_days)
                reactions = a.get("positive_reactions_count", 0) or 0
                comments  = a.get("comments_count", 0) or 0

                raw_engagement = {
                    "reactions": reactions,
                    "comments":  comments,
                    "recency":   recency,
                }
                engagement_score = compute_engagement_score(raw_engagement, "blog")

                results.append({
                    "url":             a.get("url", ""),
                    "source_type":     "blog",
                    "source_platform": "dev.to",
                    "title":           a.get("title", ""),
                    "author":          a.get("user", {}).get("name", ""),
                    "published_at":    published,
                    "content_hash":    str(a.get("id", "")),
                    "engagement_score": engagement_score,
                    "raw_engagement":  raw_engagement,
                    "trust_level":     3,
                    "topics":          [topic],
                    "status":          "active",
                    "full_content":    a.get("description", ""),
                    "summary":         (a.get("description", "") or "")[:500],
                    "key_concepts":    [],
                    "questions_answered": [],
                    "consensus_level": "opinion",
                })
            print(f"[MEDIUM] Dev.to: {len(results)} articles")
    except Exception as e:
        print(f"[MEDIUM] Dev.to error: {e}")
    return results


async def _fetch_rss(feed_url: str, platform: str, topic_keywords: list, since_days: int) -> list:
    """Parse an RSS feed and return matching RawSource dicts."""
    results = []
    try:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            title   = entry.get("title", "")
            summary = entry.get("summary", "") or entry.get("description", "")
            if not _is_relevant(title, summary, topic_keywords):
                continue
            published = entry.get("published", entry.get("updated", ""))
            if published:
                published = _parse_feed_date(published)
            else:
                published = datetime.now(timezone.utc).isoformat()

            recency = _recency_score(published, since_days)
            raw_engagement = {
                "reactions": 0,
                "comments":  0,
                "recency":   recency,
            }
            engagement_score = compute_engagement_score(raw_engagement, "blog")

            results.append({
                "url":             entry.get("link", ""),
                "source_type":     "blog",
                "source_platform": platform,
                "title":           title,
                "author":          entry.get("author", ""),
                "published_at":    published,
                "content_hash":    entry.get("id", entry.get("link", "")),
                "engagement_score": engagement_score,
                "raw_engagement":  raw_engagement,
                "trust_level":     3,
                "topics":          [" ".join(w.capitalize() for w in topic_keywords)],
                "status":          "active",
                "full_content":    summary,
                "summary":         summary[:500],
                "key_concepts":    [],
                "questions_answered": [],
                "consensus_level": "opinion",
            })
    except Exception as e:
        print(f"[MEDIUM] RSS {platform} error: {e}")
    return results


async def fetch(topic: str, max_results: int = 50, since_days: int = 7) -> list:
    """Fetch from Medium RSS, Dev.to API, Substack RSS.
    Returns list of RawSource dicts.
    """
    print(f"[MEDIUM] fetch: topic={topic} max={max_results} since={since_days}d")
    topic_keywords = [kw.lower() for kw in topic.split()]
    results = []

    # Dev.to
    devto = await _fetch_devto(topic, max_results, since_days)
    results.extend(devto)

    # Medium RSS feeds
    for feed_url in MEDIUM_RSS_TAGS:
        if len(results) >= max_results:
            break
        rss_results = await _fetch_rss(feed_url, "medium", topic_keywords, since_days)
        results.extend(rss_results)

    # Substack RSS feeds
    for feed_url in SUBSTACK_FEEDS:
        if len(results) >= max_results:
            break
        rss_results = await _fetch_rss(feed_url, "substack", topic_keywords, since_days)
        results.extend(rss_results)

    results = results[:max_results]
    print(f"[MEDIUM] Returning {len(results)} sources")
    return results
