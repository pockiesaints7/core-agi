"""scraper/knowledge/scorer.py
Normalize engagement signals from all sources into a unified 0-100 score.
Each source has different scales — log normalization per source ceiling.
"""
import math

# Max realistic engagement value per source type (for log normalization)
SOURCE_CEILINGS = {
    "arxiv":         500,    # citations
    "docs":          100,    # authority score (already 0-100)
    "medium":        10000,  # claps
    "blog":          1000,   # reactions
    "reddit":        5000,   # post score
    "hackernews":    3000,   # points
    "stackoverflow": 500,    # answer score
}

# Weighted signal config per source: list of {field, weight}
SOURCE_SIGNAL_CONFIG = {
    "arxiv":         [{"field": "citations", "weight": 0.7}, {"field": "influential_citations", "weight": 0.2}, {"field": "recency", "weight": 0.1}],
    "docs":          [{"field": "platform_authority", "weight": 0.9}, {"field": "recency", "weight": 0.1}],
    "medium":        [{"field": "claps", "weight": 0.9}, {"field": "recency", "weight": 0.1}],
    "blog":          [{"field": "reactions", "weight": 0.7}, {"field": "comments", "weight": 0.2}, {"field": "recency", "weight": 0.1}],
    "reddit":        [{"field": "score", "weight": 0.6}, {"field": "upvote_ratio", "weight": 0.3}, {"field": "num_comments", "weight": 0.1}],
    "hackernews":    [{"field": "points", "weight": 0.7}, {"field": "num_comments", "weight": 0.2}, {"field": "recency", "weight": 0.1}],
    "stackoverflow": [{"field": "answer_score", "weight": 0.6}, {"field": "view_count", "weight": 0.3}, {"field": "is_accepted", "weight": 0.1}],
}


def normalize_score(raw_value: float, source_type: str) -> float:
    ceiling = SOURCE_CEILINGS.get(source_type, 1000)
    return min(100.0, (math.log1p(raw_value) / math.log1p(ceiling)) * 100)


def compute_engagement_score(raw_engagement: dict, source_type: str) -> float:
    signals = SOURCE_SIGNAL_CONFIG.get(source_type, [])
    if not signals:
        return 0.0
    total = sum(
        normalize_score(float(raw_engagement.get(sig["field"], 0)), source_type) * sig["weight"]
        for sig in signals
    )
    return round(total, 2)
