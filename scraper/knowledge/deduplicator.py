"""scraper/knowledge/deduplicator.py
Cross-source deduplication + engagement merging.
Strategy: URL exact match first, then title similarity > 0.85.
Higher engagement source wins; signals from duplicates are merged.
"""
import re
from difflib import SequenceMatcher


def _normalize_url(url: str) -> str:
    url = url.lower().rstrip("/")
    url = re.sub(r"^https?://(?:www\.)?", "", url)
    return url


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _merge_engagement(winner: dict, duplicate: dict) -> None:
    """Combine engagement signals — add numeric values, keep winner metadata."""
    we = winner.get("raw_engagement") or {}
    de = duplicate.get("raw_engagement") or {}
    for k, v in de.items():
        if isinstance(v, (int, float)) and k in we:
            we[k] = we[k] + v
    winner["raw_engagement"] = we
    # Recompute engagement_score after merge
    from .scorer import compute_engagement_score
    winner["engagement_score"] = compute_engagement_score(we, winner.get("source_type", ""))


def deduplicate(sources: list) -> list:
    """Remove duplicates from a list of RawSource dicts.
    Sorts by engagement_score descending so higher-value sources win.
    Returns deduplicated list.
    """
    sorted_sources = sorted(sources, key=lambda x: x.get("engagement_score", 0), reverse=True)
    seen_urls = {}
    seen_titles = {}
    results = []

    for source in sorted_sources:
        url_key = _normalize_url(source.get("url", ""))
        title_key = _normalize_title(source.get("title", ""))

        if url_key and url_key in seen_urls:
            _merge_engagement(seen_urls[url_key], source)
            continue

        matched_title = None
        if title_key:
            for existing_title, existing in seen_titles.items():
                if _similarity(title_key, existing_title) > 0.85:
                    matched_title = existing
                    break

        if matched_title:
            _merge_engagement(matched_title, source)
            continue

        if url_key:
            seen_urls[url_key] = source
        if title_key:
            seen_titles[title_key] = source
        results.append(source)

    return results
