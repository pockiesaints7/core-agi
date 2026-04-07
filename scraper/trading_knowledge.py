"""Trading-domain external knowledge ingestion for CORE AGI.

This is a one-time seeding pipeline that pulls curated official references and
research abstracts into kb_sources/kb_articles/kb_concepts, then distills the
result into trading knowledge_base entries CORE can use immediately.
"""
from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import httpx
try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except Exception:  # pragma: no cover - optional local dependency
    pdf_extract_text = None

from core_config import sb_get, sb_post, sb_upsert
from core_trading_specialization import TRADING_DOMAIN, TRADING_META_DOMAIN
from scraper.knowledge.deduplicator import deduplicate
from scraper.knowledge.sources import arxiv as arxiv_source
from scraper.knowledge.storage import write_sources


@dataclass(frozen=True)
class CuratedSource:
    url: str
    title: str
    source_platform: str
    source_type: str
    trust_level: int
    author: str = ""
    published_at: str = ""
    consensus_level: str = "established"
    authority: float = 80.0
    topics: tuple[str, ...] = ("trading",)


CURATED_WEB_SOURCES: tuple[CuratedSource, ...] = (
    CuratedSource(
        url="https://github.com/binance/binance-skills-hub",
        title="Binance Skills Hub",
        source_platform="binance_skills_hub",
        source_type="docs",
        trust_level=5,
        author="Binance",
        authority=90.0,
        topics=("trading", "intelligence", "skills"),
    ),
    CuratedSource(
        url="https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History",
        title="Binance USD-M Futures Funding Rate History",
        source_platform="binance_derivatives_docs",
        source_type="docs",
        trust_level=5,
        author="Binance",
        authority=96.0,
        topics=("trading", "derivatives", "funding"),
    ),
    CuratedSource(
        url="https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest",
        title="Binance USD-M Futures Open Interest",
        source_platform="binance_derivatives_docs",
        source_type="docs",
        trust_level=5,
        author="Binance",
        authority=96.0,
        topics=("trading", "derivatives", "open_interest"),
    ),
    CuratedSource(
        url="https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Long-Short-Ratio",
        title="Binance USD-M Futures Global Long Short Account Ratio",
        source_platform="binance_derivatives_docs",
        source_type="docs",
        trust_level=5,
        author="Binance",
        authority=96.0,
        topics=("trading", "derivatives", "sentiment"),
    ),
    CuratedSource(
        url="https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Basis",
        title="Binance USD-M Futures Basis",
        source_platform="binance_derivatives_docs",
        source_type="docs",
        trust_level=5,
        author="Binance",
        authority=96.0,
        topics=("trading", "derivatives", "basis"),
    ),
    CuratedSource(
        url="https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price",
        title="Binance USD-M Futures Mark Price and Funding Snapshot",
        source_platform="binance_derivatives_docs",
        source_type="docs",
        trust_level=5,
        author="Binance",
        authority=96.0,
        topics=("trading", "derivatives", "mark_price"),
    ),
    CuratedSource(
        url="https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Order-Book",
        title="Binance USD-M Futures Order Book",
        source_platform="binance_derivatives_docs",
        source_type="docs",
        trust_level=5,
        author="Binance",
        authority=96.0,
        topics=("trading", "execution", "orderbook"),
    ),
    CuratedSource(
        url="https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order",
        title="Binance USD-M Futures New Order",
        source_platform="binance_derivatives_docs",
        source_type="docs",
        trust_level=5,
        author="Binance",
        authority=96.0,
        topics=("trading", "execution", "order_types"),
    ),
    CuratedSource(
        url="https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Notional-and-Leverage-Brackets",
        title="Binance USD-M Futures Notional and Leverage Brackets",
        source_platform="binance_derivatives_docs",
        source_type="docs",
        trust_level=5,
        author="Binance",
        authority=96.0,
        topics=("trading", "risk", "leverage"),
    ),
    CuratedSource(
        url="https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints",
        title="Binance Spot Market Data Endpoints",
        source_platform="binance_spot_docs",
        source_type="docs",
        trust_level=5,
        author="Binance",
        authority=94.0,
        topics=("trading", "market_data", "orderbook"),
    ),
    CuratedSource(
        url="https://developers.binance.com/docs/binance-spot-api-docs/faqs/spot_glossary",
        title="Binance Spot Glossary",
        source_platform="binance_spot_docs",
        source_type="docs",
        trust_level=5,
        author="Binance",
        authority=94.0,
        topics=("trading", "glossary", "execution"),
    ),
)

CURATED_PDF_SOURCES: tuple[CuratedSource, ...] = ()

ARXIV_QUERY_PLAN: tuple[tuple[str, str], ...] = (
    ("algorithmic trading order book market microstructure", "market_microstructure"),
    ("portfolio optimization asset allocation financial trading risk", "portfolio_risk"),
    ("transaction cost execution financial markets trading", "execution"),
    ("market regime volatility forecasting trading strategy", "regime"),
    ("reinforcement learning portfolio trading financial markets", "rl_trading"),
    ("backtesting walk forward validation trading strategy", "validation"),
)

TRADING_RELEVANCE_WEIGHTS: dict[str, int] = {
    "trading": 3,
    "financial": 3,
    "portfolio": 3,
    "market": 2,
    "futures": 3,
    "asset": 2,
    "stock": 2,
    "execution": 3,
    "transaction cost": 4,
    "order book": 4,
    "liquidity": 2,
    "volatility": 2,
    "risk": 2,
    "backtest": 4,
    "walk forward": 4,
    "alpha": 2,
    "returns": 2,
}

TRADING_CONCEPTS: dict[str, dict[str, Any]] = {
    "Perpetual Futures": {
        "keywords": ["perpetual futures", "perpetual contract", "perpetual swap", "perp"],
        "category": "trading_derivatives",
        "definition": "Perpetual futures are derivative contracts without expiry that stay close to spot through funding transfers between longs and shorts.",
        "related": ["Funding Rate", "Basis", "Mark Price", "Liquidation Risk"],
        "implementations": ["carry_monitor", "basis_filter", "funding_harvest"],
    },
    "Funding Rate": {
        "keywords": ["funding rate", "funding history", "funding payment"],
        "category": "trading_derivatives",
        "definition": "Funding rate is the periodic transfer between long and short perpetual positions that signals crowding and carry quality.",
        "related": ["Perpetual Futures", "Basis", "Long Short Ratio"],
        "implementations": ["funding_harvest", "carry_exit_rule", "crowding_guard"],
    },
    "Basis": {
        "keywords": ["futures basis", "basis spread", "carry trade", "term structure", "roll yield"],
        "category": "trading_derivatives",
        "definition": "Basis is the spread between futures and spot that reflects carry, positioning pressure, and term-structure regime.",
        "related": ["Funding Rate", "Perpetual Futures", "Open Interest"],
        "implementations": ["basis_guard", "carry_filter", "hedge_valuation"],
    },
    "Open Interest": {
        "keywords": ["open interest", "oi", "open positions"],
        "category": "trading_derivatives",
        "definition": "Open interest measures outstanding contracts and helps separate new positioning from short-covering or profit-taking.",
        "related": ["Long Short Ratio", "Liquidation Risk", "Market Regime"],
        "implementations": ["crowding_monitor", "breakout_validation", "squeeze_detector"],
    },
    "Long Short Ratio": {
        "keywords": ["long short ratio", "long-short ratio", "top trader long short"],
        "category": "trading_sentiment",
        "definition": "Long-short ratios estimate directional crowding and are useful only when paired with price, basis, and open-interest context.",
        "related": ["Funding Rate", "Open Interest", "Liquidation Risk"],
        "implementations": ["sentiment_overlay", "crowding_guard", "fade_filter"],
    },
    "Mark Price": {
        "keywords": ["mark price", "index price", "fair price"],
        "category": "trading_derivatives",
        "definition": "Mark price is the exchange fair-value reference used for liquidation and funding logic, and it matters more than last trade during stress.",
        "related": ["Liquidation Risk", "Perpetual Futures"],
        "implementations": ["liquidation_monitor", "risk_guard", "stop_validation"],
    },
    "Order Book": {
        "keywords": ["order book", "orderbook", "bid ask", "market depth"],
        "category": "trading_execution",
        "definition": "Order-book depth and imbalance reveal near-term liquidity conditions, but they are fragile without execution-cost discipline.",
        "related": ["Slippage", "Execution Cost", "Liquidity"],
        "implementations": ["execution_router", "liquidity_filter", "slippage_guard"],
    },
    "Slippage": {
        "keywords": ["slippage", "market impact", "fill quality"],
        "category": "trading_execution",
        "definition": "Slippage is the gap between expected and realized execution, and it often erases paper edge before the signal is wrong.",
        "related": ["Order Book", "Execution Cost", "Liquidity"],
        "implementations": ["execution_cost_model", "limit_order_policy", "trade_rejection_guard"],
    },
    "Execution Cost": {
        "keywords": ["transaction cost", "execution cost", "implementation shortfall"],
        "category": "trading_execution",
        "definition": "Execution cost combines spread, fees, impact, and delay; profitable research must beat this stack after realistic fills.",
        "related": ["Slippage", "Order Book", "Backtest Validation"],
        "implementations": ["tcost_model", "paper_to_live_gate", "execution_audit"],
    },
    "Liquidity": {
        "keywords": ["liquidity", "depth", "volume profile", "bid ask spread"],
        "category": "trading_execution",
        "definition": "Liquidity defines how much size can move without destabilizing price and should cap position size before conviction matters.",
        "related": ["Order Book", "Slippage", "Execution Cost"],
        "implementations": ["liquidity_filter", "sizing_cap", "market_access_guard"],
    },
    "Market Regime": {
        "keywords": ["market regime", "regime detection", "volatility regime", "trend following", "range-bound"],
        "category": "trading_regime",
        "definition": "Market regime separates trend, range, chop, and expansion states so strategies fire only where their expectancy survives.",
        "related": ["Volatility Clustering", "Drawdown Control", "Backtest Validation"],
        "implementations": ["regime_classifier", "strategy_router", "stand_down_gate"],
    },
    "Volatility Clustering": {
        "keywords": ["volatility clustering", "volatility regime", "heteroskedasticity"],
        "category": "trading_regime",
        "definition": "Volatility clusters in bursts, so stop distance, hold time, and leverage must adapt when variance regime changes.",
        "related": ["Market Regime", "Drawdown Control", "Position Sizing"],
        "implementations": ["volatility_filter", "atr_sizer", "regime_transition_guard"],
    },
    "Position Sizing": {
        "keywords": ["position sizing", "risk budget", "kelly", "atr stop"],
        "category": "trading_risk",
        "definition": "Position sizing converts an idea into bounded loss; it should be driven by risk budget, liquidity, and stop distance rather than conviction.",
        "related": ["Drawdown Control", "Liquidity", "Volatility Clustering"],
        "implementations": ["atr_sizer", "portfolio_risk_guard", "capital_allocator"],
    },
    "Drawdown Control": {
        "keywords": ["drawdown", "risk of ruin", "loss limit", "circuit breaker"],
        "category": "trading_risk",
        "definition": "Drawdown control prevents a valid edge from being destroyed by variance, correlation, or a bad operating day.",
        "related": ["Position Sizing", "Correlation Risk", "Market Regime"],
        "implementations": ["daily_circuit_breaker", "loss_streak_guard", "exposure_budget"],
    },
    "Correlation Risk": {
        "keywords": ["correlation", "cross-asset", "crowding", "portfolio exposure"],
        "category": "trading_risk",
        "definition": "Correlation risk appears when seemingly different trades are the same macro bet, causing portfolio drawdowns to stack unexpectedly.",
        "related": ["Position Sizing", "Drawdown Control", "Open Interest"],
        "implementations": ["correlation_guard", "cluster_exposure_limit", "portfolio_heatmap"],
    },
    "Backtest Validation": {
        "keywords": ["walk forward", "backtest", "overfitting", "out of sample", "validation"],
        "category": "trading_validation",
        "definition": "Backtest validation requires walk-forward evidence, realistic costs, and regime coverage to separate edge from curve-fit noise.",
        "related": ["Execution Cost", "Market Regime", "Paper-to-Live Graduation"],
        "implementations": ["walk_forward_gate", "cost_adjusted_backtest", "deployment_gate"],
    },
    "Paper-to-Live Graduation": {
        "keywords": ["paper trading", "paper-to-live", "graduation", "deployment"],
        "category": "trading_validation",
        "definition": "Paper-to-live graduation should require stable execution, sufficient sample size, and multi-regime evidence before capital is exposed.",
        "related": ["Backtest Validation", "Execution Cost", "Drawdown Control"],
        "implementations": ["graduation_policy", "promotion_gate", "owner_review_guard"],
    },
    "Liquidation Risk": {
        "keywords": ["liquidation", "liquidated", "maintenance margin"],
        "category": "trading_risk",
        "definition": "Liquidation risk grows nonlinearly when leverage, crowding, and mark-price drift stack against a position.",
        "related": ["Mark Price", "Funding Rate", "Open Interest"],
        "implementations": ["liquidation_guard", "leverage_cap", "distance_to_liq_monitor"],
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_main_html(raw: str) -> str:
    candidates: list[str] = []
    for pattern in (
        r"<article\b[^>]*>(.*?)</article>",
        r"<main\b[^>]*>(.*?)</main>",
    ):
        matches = re.findall(pattern, raw or "", flags=re.IGNORECASE | re.DOTALL)
        for match in matches:
            cleaned = re.sub(r"<(nav|aside|footer)\b[^>]*>.*?</\1>", " ", match, flags=re.IGNORECASE | re.DOTALL)
            if cleaned.strip():
                candidates.append(cleaned)
    if not candidates:
        return raw or ""
    return max(candidates, key=len)


def _title_from_html(raw: str, fallback: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", raw or "", flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return fallback
    return _clean_text(match.group(1))[:180] or fallback


def _description_from_html(raw: str) -> str:
    for pattern in (
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
    ):
        match = re.search(pattern, raw or "", flags=re.IGNORECASE)
        if match:
            return _clean_text(match.group(1))[:800]
    return ""


def _published_from_html(raw: str, fallback: str) -> str:
    for pattern in (
        r'property=["\']article:published_time["\'] content=["\']([^"\']+)["\']',
        r'name=["\']date["\'] content=["\']([^"\']+)["\']',
        r'name=["\']last-modified["\'] content=["\']([^"\']+)["\']',
    ):
        match = re.search(pattern, raw or "", flags=re.IGNORECASE)
        if match:
            return match.group(1)[:64]
    return fallback or ""


def _recency_score(published_at: str, horizon_days: int = 3650) -> float:
    try:
        normalized = (published_at or "").replace("Z", "+00:00")
        if normalized.endswith("+00:00") and "T" not in normalized:
            normalized = normalized.replace("+00:00", "T00:00:00+00:00")
        dt = datetime.fromisoformat(normalized)
        delta_days = max(0, int((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days))
        return max(15.0, 100.0 - ((delta_days / max(1, horizon_days)) * 100.0))
    except Exception:
        return 55.0


def _extract_sentences(text: str, keywords: list[str], limit: int = 3) -> list[str]:
    fragments = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    lowered = [kw.lower() for kw in keywords]
    selected: list[str] = []
    seen = set()
    for fragment in fragments:
        sentence = re.sub(r"\s+", " ", fragment).strip()
        if len(sentence) < 40 or len(sentence) > 320:
            continue
        lower = sentence.lower()
        if not any(kw in lower for kw in lowered):
            continue
        key = sentence[:180]
        if key in seen:
            continue
        seen.add(key)
        selected.append(sentence)
        if len(selected) >= limit:
            break
    return selected


def _trading_relevance_score(text: str) -> tuple[int, int]:
    lower = (text or "").lower()
    score = sum(weight for keyword, weight in TRADING_RELEVANCE_WEIGHTS.items() if keyword in lower)
    anchors = sum(1 for keyword in ("trading", "financial", "portfolio", "market", "futures", "backtest", "execution", "order book") if keyword in lower)
    return score, anchors


def _render_summary(text: str, keywords: list[str]) -> str:
    snippets = _extract_sentences(text, keywords, limit=2)
    if snippets:
        return " ".join(snippets)[:800]
    return (text or "")[:800]


async def _fetch_html_source(source: CuratedSource, client: httpx.AsyncClient) -> dict | None:
    response = await client.get(source.url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    page_title = _title_from_html(response.text, source.title)
    published_at = _published_from_html(response.text, source.published_at)
    summary_hint = _description_from_html(response.text)
    main_html = _extract_main_html(response.text)
    text = _clean_text(main_html)
    if len(text) < 300:
        return None
    recency = _recency_score(published_at)
    return {
        "url": source.url,
        "source_type": source.source_type,
        "source_platform": source.source_platform,
        "title": page_title,
        "author": source.author or source.source_platform,
        "published_at": published_at or None,
        "ingested_at": _now_iso(),
        "engagement_score": round((source.authority * 0.85) + (recency * 0.15), 2),
        "raw_engagement": {"platform_authority": source.authority, "recency": recency},
        "trust_level": source.trust_level,
        "topics": list(source.topics),
        "status": "active",
        "full_content": text[:24000],
        "summary": summary_hint or _render_summary(text, [part for part in source.title.lower().split() if len(part) > 4]),
        "key_concepts": [],
        "cited_references": [source.url],
        "questions_answered": [f"What does {page_title} contribute to trading system design?"],
        "consensus_level": source.consensus_level,
    }


async def _fetch_pdf_source(source: CuratedSource, client: httpx.AsyncClient) -> dict | None:
    if pdf_extract_text is None:
        print(f"[TRADING-SEED] pdfminer unavailable locally; skipping PDF source {source.url}")
        return None
    response = await client.get(source.url, timeout=45, follow_redirects=True)
    response.raise_for_status()
    text = pdf_extract_text(BytesIO(response.content)) or ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 600:
        return None
    published_at = source.published_at or _now_iso()
    recency = _recency_score(published_at)
    return {
        "url": source.url,
        "source_type": source.source_type,
        "source_platform": source.source_platform,
        "title": source.title,
        "author": source.author or source.source_platform,
        "published_at": published_at,
        "ingested_at": _now_iso(),
        "engagement_score": round((source.authority * 0.85) + (recency * 0.15), 2),
        "raw_engagement": {"platform_authority": source.authority, "recency": recency},
        "trust_level": source.trust_level,
        "topics": list(source.topics),
        "status": "active",
        "full_content": text[:24000],
        "summary": text[:800],
        "key_concepts": [],
        "cited_references": [source.url],
        "questions_answered": [f"What operational trading lessons are inside {source.title}?"],
        "consensus_level": source.consensus_level,
    }


async def _fetch_curated_sources() -> list[dict]:
    results: list[dict] = []
    source_plan = list(CURATED_WEB_SOURCES) + list(CURATED_PDF_SOURCES)
    async with httpx.AsyncClient(
        timeout=45,
        follow_redirects=True,
        headers={"User-Agent": "CORE-AGI-TradingSeeder/1.0 (+https://github.com/pockiesaints7/core-agi)"},
    ) as client:
        tasks = [
            _fetch_html_source(source, client)
            for source in CURATED_WEB_SOURCES
        ] + [
            _fetch_pdf_source(source, client)
            for source in CURATED_PDF_SOURCES
        ]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
    for idx, item in enumerate(gathered):
        if isinstance(item, Exception):
            source = source_plan[idx]
            print(f"[TRADING-SEED] curated source fetch failed: {source.url} :: {item}")
            continue
        if item:
            results.append(item)
    return results


async def _fetch_arxiv_sources(max_per_query: int, since_days: int) -> list[dict]:
    results: list[dict] = []
    if max_per_query <= 0:
        return results
    for query, category in ARXIV_QUERY_PLAN:
        papers = await arxiv_source.fetch(query, max_results=max_per_query, since_days=since_days)
        for paper in papers:
            relevance_score, anchors = _trading_relevance_score(
                f"{paper.get('title', '')} {paper.get('summary', '')}"
            )
            if relevance_score < 7 or anchors < 2:
                print(f"[TRADING-SEED] filtered low-relevance arXiv paper: {paper.get('title', '')[:90]}")
                continue
            paper["topics"] = ["trading", category]
            paper["questions_answered"] = [f"What does recent research say about {category.replace('_', ' ')}?"]
            paper["consensus_level"] = "research"
            results.append(paper)
    return results


def _concept_hits(sources: list[dict]) -> dict[str, dict[str, Any]]:
    hits: dict[str, dict[str, Any]] = {}
    for source in sources:
        text = " ".join([
            source.get("title", ""),
            source.get("summary", ""),
            source.get("full_content", "")[:12000],
        ])
        lower = text.lower()
        title_lower = str(source.get("title", "")).lower()
        topic_lower = " ".join(str(item).lower() for item in (source.get("topics") or []))
        for concept_name, meta in TRADING_CONCEPTS.items():
            keywords = meta["keywords"]
            concept_alias = concept_name.lower()
            concept_slug = _slug(concept_name).replace("_", " ")
            keyword_match = any(keyword.lower() in lower for keyword in keywords)
            title_or_topic_match = concept_alias in title_lower or concept_slug in topic_lower
            if not keyword_match and not title_or_topic_match:
                continue
            bucket = hits.setdefault(concept_name, {
                "count": 0,
                "total_engagement": 0.0,
                "best_source_id": None,
                "best_source_title": "",
                "best_engagement": -1.0,
                "snippets": [],
                "source_titles": [],
                "source_urls": [],
            })
            engagement = float(source.get("engagement_score") or 0.0)
            bucket["count"] += 1
            bucket["total_engagement"] += engagement
            bucket["source_titles"].append(source.get("title", ""))
            bucket["source_urls"].append(source.get("url", ""))
            for snippet in _extract_sentences(text, keywords, limit=2):
                if snippet not in bucket["snippets"]:
                    bucket["snippets"].append(snippet)
            if engagement > bucket["best_engagement"]:
                bucket["best_engagement"] = engagement
                bucket["best_source_id"] = source.get("db_id")
                bucket["best_source_title"] = source.get("title", "")
    return hits


def _concept_confidence(hit_count: int) -> str:
    if hit_count >= 4:
        return "high"
    if hit_count >= 2:
        return "medium"
    return "low"


def _upsert_concept_rows(hits: dict[str, dict[str, Any]]) -> int:
    inserted = 0
    for concept_name, stats in hits.items():
        meta = TRADING_CONCEPTS[concept_name]
        avg_eng = round(stats["total_engagement"] / max(1, stats["count"]), 2)
        row = {
            "concept_name": concept_name,
            "category": meta["category"],
            "definition": meta["definition"],
            "best_source_id": stats.get("best_source_id"),
            "source_count": stats["count"],
            "avg_engagement": avg_eng,
            "first_seen": _now_iso(),
            "trend": "rising" if stats["count"] >= 3 else "active",
            "related_concepts": meta["related"],
            "implementations": meta["implementations"],
        }
        ok = sb_upsert("kb_concepts", row, "concept_name")
        inserted += 1 if ok else 0
    return inserted


def _upsert_knowledge_entries(hits: dict[str, dict[str, Any]]) -> int:
    inserted = 0
    for concept_name, stats in hits.items():
        meta = TRADING_CONCEPTS[concept_name]
        sources = [
            title for title in stats.get("source_titles", [])
            if title
        ][:3]
        snippets = [
            snippet for snippet in stats.get("snippets", [])
            if snippet
        ][:3]
        content_parts = [
            meta["definition"],
            f"Evidence coverage: {stats['count']} external trading sources.",
            f"Key sources: {', '.join(sources) if sources else 'curated trading sources and research abstracts'}.",
        ]
        if snippets:
            content_parts.append("Observed cues: " + " | ".join(snippets))
        content_parts.append("Operational use for CORE: " + ", ".join(meta["implementations"]) + ".")
        content_parts.append("Related concepts: " + ", ".join(meta["related"]) + ".")
        content = " ".join(content_parts)[:3800]
        ok = sb_upsert(
            "knowledge_base",
            {
                "domain": TRADING_DOMAIN,
                "topic": f"trading_concept_{_slug(concept_name)}",
                "content": content,
                "instruction": content,
                "confidence": _concept_confidence(stats["count"]),
                "source": "trading_external_seed",
                "source_type": "external_seed",
                "source_ref": concept_name,
                "source_ts": _now_iso(),
                "tags": ["trading", "external_seed", meta["category"], _slug(concept_name)],
                "active": True,
            },
            on_conflict="domain,topic",
        )
        inserted += 1 if ok else 0
    return inserted


def _write_seed_overview(summary: dict, concept_names: list[str]) -> None:
    overview = (
        f"Trading external seed completed at {summary['seeded_at']}. "
        f"Sources={summary['deduped_count']} inserted={summary['records_inserted']} updated={summary['records_updated']} "
        f"concepts={summary['concepts_found']} knowledge_entries={summary['knowledge_entries']}. "
        f"Top concepts: {', '.join(concept_names[:12]) or 'none'}."
    )
    sb_upsert(
        "knowledge_base",
        {
            "domain": TRADING_META_DOMAIN,
            "topic": "trading_external_seed_overview",
            "content": overview,
            "instruction": overview,
            "confidence": "high",
            "source": "trading_external_seed",
            "source_type": "external_seed",
            "source_ref": "trading_external_seed_overview",
            "source_ts": _now_iso(),
            "tags": ["trading", "external_seed", "overview"],
            "active": True,
        },
        on_conflict="domain,topic",
    )
    reflection_key = "Trading external seed corpus refreshed"
    existing_reflections = sb_get(
        "hot_reflections",
        "select=task_summary&domain=eq.trading_meta&source=eq.external_seed&order=id.desc&limit=20",
        svc=True,
    ) or []
    if reflection_key not in {str(row.get("task_summary") or "") for row in existing_reflections}:
        sb_post("hot_reflections", {
            "task_summary": reflection_key,
            "domain": TRADING_META_DOMAIN,
            "new_patterns": concept_names[:5],
            "new_mistakes": [],
            "quality_score": 0.92,
            "gaps_identified": [],
            "reflection_text": overview,
            "processed_by_cold": 0,
            "source": "external_seed",
        })
    sb_post("sessions", {
        "summary": f"[state_update] last_trading_seed_ts: {summary['seeded_at']}",
        "actions": [f"trading external seed sources={summary['deduped_count']} concepts={summary['concepts_found']}"],
        "interface": "mcp",
    })


async def ingest_trading_knowledge(
    max_arxiv_per_query: int = 0,
    since_days: int = 3650,
) -> dict:
    curated = await _fetch_curated_sources()
    research = await _fetch_arxiv_sources(max_per_query=max(0, int(max_arxiv_per_query)), since_days=max(30, int(since_days)))
    raw_sources = curated + research
    deduped_sources = deduplicate(raw_sources)
    storage_report = await write_sources(deduped_sources, topic="trading")
    hits = _concept_hits(deduped_sources)
    concept_rows = _upsert_concept_rows(hits)
    kb_entries = _upsert_knowledge_entries(hits)
    concept_names = sorted(hits.keys())
    summary = {
        "topic": "trading",
        "seeded_at": _now_iso(),
        "raw_count": len(raw_sources),
        "deduped_count": len(deduped_sources),
        "records_inserted": storage_report.get("source_inserted", 0),
        "records_updated": storage_report.get("source_updated", 0),
        "article_rows_inserted": storage_report.get("article_inserted", 0),
        "article_rows_updated": storage_report.get("article_updated", 0),
        "article_rows_skipped": storage_report.get("article_skipped", 0),
        "storage_errors": (
            storage_report.get("source_errors", [])
            + storage_report.get("article_errors", [])
        )[:10],
        "concept_rows_upserted": concept_rows,
        "concepts_found": len(concept_names),
        "knowledge_entries": kb_entries,
        "source_breakdown": {
            "curated_docs": len(curated),
            "research_papers": len(research),
        },
        "concepts": concept_names,
    }
    _write_seed_overview(summary, concept_names)
    return summary
