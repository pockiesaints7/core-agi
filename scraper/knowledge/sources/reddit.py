"""scraper/knowledge/sources/reddit.py
Fetch high-quality discussions from AI subreddits.
Uses Reddit public JSON API — no key required (read-only).
"""
import httpx
from datetime import datetime, timezone, timedelta
from ..scorer import compute_engagement_score

SUBREDDITS = [
    "LocalLLaMA",
    "MachineLearning",
    "artificial",
    "singularity",
    "AIAssistants",
]

REDDIT_TOP_URL = "https://www.reddit.com/r/{sub}/top.json"
REDDIT_SEARCH_URL = "https://www.reddit.com/r/{sub}/search.json"
HEADERS = {"User-Agent": "CORE-AGI-Knowledge-Bot/1.0"}
MIN_POST_SCORE   = 50
MIN_COMMENT_SCORE = 10


def _ts_to_iso(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _recency_score(created_utc: float, since_days: int = 7) -> float:
    try:
        now = datetime.now(timezone.utc).timestamp()
        age_days = max(0, (now - created_utc) / 86400)
        return max(0.0, 100.0 - (age_days / max(since_days, 1)) * 100)
    except Exception:
        return 50.0


def _is_relevant(title: str, body: str, topic_keywords: list) -> bool:
    combined = (title + " " + body).lower()
    return any(kw in combined for kw in topic_keywords)


async def _fetch_comments(subreddit: str, post_id: str, client: httpx.AsyncClient) -> str:
    """Fetch top comments for a post, return concatenated text."""
    try:
        url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json"
        r = await client.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return ""
        data = r.json()
        comments_data = data[1]["data"]["children"] if len(data) > 1 else []
        top_comments = []
        for c in comments_data:
            if c.get("kind") == "t1":
                cdata = c.get("data", {})
                if cdata.get("score", 0) >= MIN_COMMENT_SCORE:
                    body = cdata.get("body", "")
                    if body and body != "[deleted]" and body != "[removed]":
                        top_comments.append(body[:500])
        return "\n\n---\n\n".join(top_comments[:5])
    except Exception:
        return ""


async def fetch(topic: str, max_results: int = 50, since_days: int = 7) -> list:
    """Fetch top posts + expert comments from AI subreddits.
    Returns list of RawSource dicts.
    """
    print(f"[REDDIT] fetch: topic={topic} max={max_results} since={since_days}d")
    topic_keywords = [kw.lower() for kw in topic.split()]
    results = []

    async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
        for sub in SUBREDDITS:
            if len(results) >= max_results:
                break
            try:
                # Search within subreddit for topic
                r = await client.get(
                    REDDIT_SEARCH_URL.format(sub=sub),
                    params={"q": topic, "sort": "top", "t": "month", "limit": 25, "restrict_sr": "true"},
                    timeout=10,
                )
                if r.status_code != 200:
                    # Fallback to top posts
                    r = await client.get(
                        REDDIT_TOP_URL.format(sub=sub),
                        params={"t": "week", "limit": 25},
                        timeout=10,
                    )
                if r.status_code != 200:
                    continue

                posts = r.json().get("data", {}).get("children", [])
                for post in posts:
                    if len(results) >= max_results:
                        break
                    d = post.get("data", {})
                    score = d.get("score", 0) or 0
                    if score < MIN_POST_SCORE:
                        continue

                    title = d.get("title", "")
                    body  = d.get("selftext", "") or ""
                    if not _is_relevant(title, body, topic_keywords):
                        continue

                    post_id     = d.get("id", "")
                    created_utc = d.get("created_utc", 0)
                    recency     = _recency_score(created_utc, since_days)
                    num_comments = d.get("num_comments", 0) or 0
                    upvote_ratio = d.get("upvote_ratio", 0.5) or 0.5

                    comments_text = await _fetch_comments(sub, post_id, client)
                    full_content  = f"{title}\n\n{body}"
                    if comments_text:
                        full_content += f"\n\n== TOP COMMENTS ==\n\n{comments_text}"

                    raw_engagement = {
                        "score":        score,
                        "upvote_ratio": upvote_ratio * 100,  # normalize to 0-100
                        "num_comments": num_comments,
                    }
                    engagement_score = compute_engagement_score(raw_engagement, "reddit")

                    results.append({
                        "url":             f"https://reddit.com{d.get('permalink', '')}",
                        "source_type":     "reddit",
                        "source_platform": f"r/{sub}",
                        "title":           title,
                        "author":          d.get("author", ""),
                        "published_at":    _ts_to_iso(created_utc),
                        "content_hash":    post_id,
                        "engagement_score": engagement_score,
                        "raw_engagement":  raw_engagement,
                        "trust_level":     2,
                        "topics":          [topic],
                        "status":          "active",
                        "full_content":    full_content[:5000],
                        "summary":         (body or title)[:500],
                        "key_concepts":    [],
                        "questions_answered": [],
                        "consensus_level": "debated",
                    })

            except Exception as e:
                print(f"[REDDIT] r/{sub} error: {e}")
                continue

    print(f"[REDDIT] Returning {len(results)} sources")
    return results
