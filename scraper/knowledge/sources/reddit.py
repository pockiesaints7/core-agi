"""scraper/knowledge/sources/reddit.py
Fetch high-quality discussions from AI subreddits via Reddit public JSON API.
No API key required (read-only public API).

API: https://www.reddit.com/r/{subreddit}/top.json?t=week&limit=50
Filter: posts score > 50, comments score > 10
"""

SUBREDDITS = [
    "LocalLLaMA",
    "MachineLearning",
    "artificial",
    "singularity",
    "AIAssistants",
]


async def fetch(topic: str, max_results: int = 50, since_days: int = 7) -> list:
    """Fetch top posts + top comments from AI subreddits matching topic.
    Returns list of RawSource dicts.
    TODO (22.B4): implement Reddit JSON API fetch + comment extraction.
    """
    # STUB — implementation in 22.B4
    print(f"[REDDIT] fetch stub called: topic={topic} max={max_results} since={since_days}d")
    return []
