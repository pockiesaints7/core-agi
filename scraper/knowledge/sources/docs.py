"""scraper/knowledge/sources/docs.py
Fetch official AI platform documentation via sitemap.xml discovery.
No API key required.

Targets: Anthropic, OpenAI, LangChain, LlamaIndex, HuggingFace, Mistral, Google AI
Authority scores: anthropic=95, openai=95, langchain=85, hf=85, llamaindex=80, mistral=80, google=85
"""
import httpx

OFFICIAL_DOCS = [
    {"url": "https://docs.anthropic.com",          "platform": "anthropic",   "authority": 95},
    {"url": "https://platform.openai.com/docs",    "platform": "openai",      "authority": 95},
    {"url": "https://python.langchain.com/docs",   "platform": "langchain",   "authority": 85},
    {"url": "https://docs.llamaindex.ai",          "platform": "llamaindex",  "authority": 80},
    {"url": "https://huggingface.co/docs",         "platform": "huggingface", "authority": 85},
    {"url": "https://docs.mistral.ai",             "platform": "mistral",     "authority": 80},
    {"url": "https://ai.google.dev/docs",          "platform": "google",      "authority": 85},
]


async def fetch(topic: str, max_results: int = 50) -> list:
    """Discover doc pages via sitemap.xml, fetch + extract content matching topic.
    Returns list of RawSource dicts.
    TODO (22.B2): implement sitemap discovery + content extraction.
    """
    # STUB — implementation in 22.B2
    print(f"[DOCS] fetch stub called: topic={topic} max={max_results}")
    return []
