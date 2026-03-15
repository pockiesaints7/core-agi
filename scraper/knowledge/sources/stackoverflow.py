"""scraper/knowledge/sources/stackoverflow.py
Fetch answered Q&A pairs from Stack Overflow via public Stack Exchange API.
No key required (300 req/day). STACKOVERFLOW_KEY env var raises limit to 10k/day.

API: https://api.stackexchange.com/2.3/search/advanced
     ?order=desc&sort=votes&q={topic}&site=stackoverflow&filter=withbody
     &tagged=llm;ai-agent;langchain
Fetch: question body + accepted answer + top voted answers (score > 5).
"""
import os
import httpx

SO_API      = "https://api.stackexchange.com/2.3/search/advanced"
SO_KEY      = os.environ.get("STACKOVERFLOW_KEY", "")
SO_TAGS     = ["llm", "ai-agent", "langchain", "openai-api", "huggingface"]


async def fetch(topic: str, max_results: int = 50) -> list:
    """Fetch top voted Q&A pairs from Stack Overflow matching topic.
    Returns list of RawSource dicts.
    TODO (22.B6): implement Stack Exchange API fetch + answer extraction.
    """
    # STUB — implementation in 22.B6
    print(f"[SO] fetch stub called: topic={topic} max={max_results}")
    return []
