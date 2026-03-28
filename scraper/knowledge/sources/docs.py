"""scraper/knowledge/sources/docs.py
Fetch official documentation from major AI platforms.
No API key required. Uses sitemap.xml discovery + content extraction.
"""
import httpx
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from ..scorer import compute_engagement_score

OFFICIAL_DOCS = [
    {"url": "https://docs.anthropic.com",          "platform": "anthropic",   "authority": 95},
    {"url": "https://platform.openai.com/docs",    "platform": "openai",      "authority": 95},
    {"url": "https://python.langchain.com/docs",   "platform": "langchain",   "authority": 85},
    {"url": "https://docs.llamaindex.ai",          "platform": "llamaindex",  "authority": 80},
    {"url": "https://huggingface.co/docs",         "platform": "huggingface", "authority": 85},
    {"url": "https://docs.mistral.ai",             "platform": "mistral",     "authority": 80},
    {"url": "https://ai.google.dev/docs",          "platform": "google_ai",   "authority": 85},
]

SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/docs/sitemap.xml"]


async def _discover_urls(base_url: str, client: httpx.AsyncClient, max_urls: int = 20) -> list:
    """Try common sitemap paths, return list of doc page URLs."""
    for path in SITEMAP_PATHS:
        try:
            r = await client.get(base_url.rstrip("/") + path, timeout=10, follow_redirects=True)
            if r.status_code == 200 and "xml" in r.headers.get("content-type", ""):
                root = ET.fromstring(r.text)
                # Handle both sitemap index and regular sitemap
                ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
                urls = [loc.text for loc in root.findall(".//sm:loc", ns) if loc.text]
                # Filter to likely doc pages
                doc_urls = [u for u in urls if any(
                    kw in u.lower() for kw in ["/docs", "/guide", "/api", "/reference", "/tutorial"]
                )]
                return doc_urls[:max_urls] if doc_urls else urls[:max_urls]
        except Exception:
            continue
    return []


async def _extract_page_text(url: str, client: httpx.AsyncClient) -> str:
    """Fetch a page and extract readable text content."""
    try:
        r = await client.get(url, timeout=15, follow_redirects=True)
        if r.status_code != 200:
            return ""
        # Basic HTML text extraction — strip tags
        import re
        text = re.sub(r"<script[^>]*>.*?</script>", "", r.text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>",  "", text,   flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:5000]
    except Exception:
        return ""


def _recency_score_from_header(last_modified: str) -> float:
    """Parse Last-Modified header and return recency 0-100."""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(last_modified)
        now = datetime.now(timezone.utc)
        age_days = max(0, (now - dt).days)
        return max(0.0, 100.0 - (age_days / 365) * 100)
    except Exception:
        return 50.0  # unknown = mid-score


async def fetch(topic: str, max_results: int = 20) -> list:
    """Fetch relevant documentation pages from official AI platform docs.
    Returns list of RawSource dicts.
    """
    print(f"[DOCS] fetch: topic={topic} max={max_results}")
    results = []

    # Topic keywords for relevance filtering
    topic_keywords = [kw.lower() for kw in topic.split()]

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for doc_site in OFFICIAL_DOCS:
            if len(results) >= max_results:
                break
            try:
                base_url   = doc_site["url"]
                platform   = doc_site["platform"]
                authority  = doc_site["authority"]

                print(f"[DOCS] Discovering {platform}...")
                urls = await _discover_urls(base_url, client, max_urls=10)

                # Filter URLs by topic relevance
                relevant = [u for u in urls if any(kw in u.lower() for kw in topic_keywords)]
                if not relevant:
                    relevant = urls[:3]  # fallback: take first 3

                for url in relevant[:3]:
                    try:
                        content = await _extract_page_text(url, client)
                        if not content:
                            continue

                        # Check relevance by content
                        content_lower = content.lower()
                        if not any(kw in content_lower for kw in topic_keywords):
                            continue

                        title = url.split("/")[-1].replace("-", " ").replace("_", " ").title() or platform
                        recency = 60.0  # default for docs

                        raw_engagement = {
                            "platform_authority": authority,
                            "recency": recency,
                        }
                        engagement_score = compute_engagement_score(raw_engagement, "docs")

                        results.append({
                            "url":             url,
                            "source_type":     "docs",
                            "source_platform": platform,
                            "title":           f"{platform.title()} Docs: {title}",
                            "author":          platform.title(),
                            "published_at":    datetime.now(timezone.utc).isoformat(),
                            "content_hash":    url,
                            "engagement_score": engagement_score,
                            "raw_engagement":  raw_engagement,
                            "trust_level":     5,
                            "topics":          [topic],
                            "status":          "active",
                            "full_content":    content,
                            "summary":         content[:500],
                            "key_concepts":    [],
                            "questions_answered": [],
                            "consensus_level": "established",
                        })

                        if len(results) >= max_results:
                            break

                    except Exception as e:
                        print(f"[DOCS] Page error {url}: {e}")
                        continue

            except Exception as e:
                print(f"[DOCS] Site error {doc_site['platform']}: {e}")
                continue

    print(f"[DOCS] Returning {len(results)} sources")
    return results
