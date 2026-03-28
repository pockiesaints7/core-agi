"""core_public_evidence.py — CORE-native public evidence source classifier.

This module centralizes the source-family logic for public research sweeps.
It does not depend on the trading-bot runtime scripts. The trading-bot
research/seeder files are reference inputs only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any, Iterable

PUBLIC_SOURCE_FAMILIES = {
    "public_general": ["arxiv", "docs", "medium", "reddit", "hackernews", "stackoverflow"],
    "public_trading": ["docs", "reddit", "hackernews", "medium", "stackoverflow"],
    "web_fallback": ["web_search", "web_fetch"],
}


def _keyword_hits(text: str, keywords: Iterable[str]) -> int:
    lower = (text or "").lower()
    return sum(1 for kw in keywords if kw in lower)


def _code_targets(text: str) -> list[str]:
    if not text:
        return []
    candidates = set()
    for match in re.findall(r"[\w./:-]+\.(?:py|ts|js|jsx|tsx|json|md|yml|yaml|toml)", text):
        candidates.add(match.strip(" ,;:()[]{}<>"))
    for match in re.findall(r"(?:/[\w.-]+)+", text):
        if "." not in match and match.count("/") <= 1:
            continue
        candidates.add(match.strip(" ,;:()[]{}<>"))
    return sorted(candidates)[:5]


def classify_public_evidence(
    query: str,
    domain: str = "",
    request_kind: str = "",
    code_targets: list[str] | None = None,
) -> dict[str, Any]:
    """Choose the public evidence family and the source mix."""
    text = (query or "").strip()
    lower = text.lower()
    code_targets = code_targets or _code_targets(text)

    trading_terms = (
        "trade", "trading", "binance", "futures", "funding", "liquidity", "market",
        "crypto", "btc", "eth", "sol", "perp", "orderbook", "sentiment", "dominance",
        "long", "short", "leverage", "pnl", "position",
    )
    research_terms = (
        "research", "paper", "arxiv", "study", "benchmark", "academic", "scientific",
        "docs", "documentation", "official", "guide", "api", "reference",
    )
    news_terms = ("latest", "current", "today", "news", "release", "update", "announce", "trending")

    trading_hits = _keyword_hits(lower, trading_terms)
    research_hits = _keyword_hits(lower, research_terms)
    news_hits = _keyword_hits(lower, news_terms)
    web_needed = bool(news_hits or research_hits or _keyword_hits(lower, ("web", "internet", "public")))

    if any(k in lower for k in ("binance", "funding", "trading", "market", "position", "orderbook", "pnl", "crypto")):
        family = "public_trading"
    elif research_hits >= 2 or news_hits >= 1:
        family = "public_general"
    elif request_kind in {"debug", "review"} and code_targets:
        family = "web_fallback"
    elif domain.lower().startswith(("trade", "crypto", "market")):
        family = "public_trading"
    else:
        family = "public_general"

    if family == "public_trading":
        sources = PUBLIC_SOURCE_FAMILIES["public_trading"]
    else:
        sources = PUBLIC_SOURCE_FAMILIES["public_general"]

    if web_needed or request_kind in {"status", "self_assessment"}:
        sources = list(dict.fromkeys(list(sources) + PUBLIC_SOURCE_FAMILIES["web_fallback"]))
    else:
        sources = list(dict.fromkeys(sources))

    public_research_needed = bool(
        trading_hits
        or research_hits
        or news_hits
        or any(k in lower for k in ("docs", "documentation", "api", "reference", "manual", "guide", "official", "public", "internet", "web", "blog", "tutorial", "community", "forum", "reddit", "hackernews", "stackoverflow"))
    )

    return {
        "query": text,
        "domain": domain or "",
        "request_kind": request_kind or "",
        "public_family": family,
        "public_sources": sources,
        "public_research_needed": public_research_needed,
        "combine_with_internal": True,
        "web_fallback_needed": web_needed or family == "web_fallback",
        "code_targets": code_targets,
        "source_counts": {
            "trading_hits": trading_hits,
            "research_hits": research_hits,
            "news_hits": news_hits,
        },
        "notes": (
            "CORE should combine internal Supabase evidence with the best matching "
            "public source family, then use web fallback only if the sweep is still thin."
        ),
    }


def build_public_evidence_packet(
    query: str,
    domain: str = "",
    request_kind: str = "",
    code_targets: list[str] | None = None,
) -> dict[str, Any]:
    gate = classify_public_evidence(
        query=query,
        domain=domain,
        request_kind=request_kind,
        code_targets=code_targets,
    )
    return {
        "ok": True,
        "packet": {
            **gate,
            "families": {
                "public_general": PUBLIC_SOURCE_FAMILIES["public_general"],
                "public_trading": PUBLIC_SOURCE_FAMILIES["public_trading"],
                "web_fallback": PUBLIC_SOURCE_FAMILIES["web_fallback"],
            },
        },
    }


def t_public_evidence_packet(
    query: str = "",
    domain: str = "",
    request_kind: str = "",
    code_targets: str = "",
) -> dict:
    try:
        targets = [t.strip() for t in code_targets.split(",") if t.strip()] if code_targets else None
        return build_public_evidence_packet(
            query=query,
            domain=domain,
            request_kind=request_kind,
            code_targets=targets,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def t_public_evidence_status() -> dict:
    return {
        "ok": True,
        "enabled": True,
        "families": {
            "public_general": PUBLIC_SOURCE_FAMILIES["public_general"],
            "public_trading": PUBLIC_SOURCE_FAMILIES["public_trading"],
            "web_fallback": PUBLIC_SOURCE_FAMILIES["web_fallback"],
        },
    }
