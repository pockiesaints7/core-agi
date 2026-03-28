"""scraper/knowledge/concept_extractor.py
Extract AI concepts from ingested content and upsert into kb_concepts table.
Uses keyword matching against curated AI_CONCEPTS dictionary.
"""
import httpx
from datetime import datetime, timezone
from core_config import SUPABASE_URL, _sbh

AI_CONCEPTS = {
    # Reasoning patterns
    "Chain of Thought":   ["chain of thought", "cot", "step by step reasoning"],
    "ReAct":              ["react", "reason act", "thought action observation"],
    "Tree of Thought":    ["tree of thought", "tot", "thought tree"],
    "Reflection":         ["self-reflection", "self-critique", "reflect and refine"],
    # Architecture patterns
    "RAG":                ["retrieval augmented", "rag", "retrieve and generate"],
    "Multi-agent":        ["multi-agent", "agent orchestration", "agent swarm"],
    "Tool Use":           ["tool calling", "function calling", "tool use"],
    "Memory":             ["long term memory", "agent memory", "context memory"],
    # Training / optimization
    "RLHF":               ["rlhf", "reinforcement learning from human feedback"],
    "Fine-tuning":        ["fine-tuning", "finetuning", "lora", "qlora"],
    "Prompt Engineering": ["prompt engineering", "few-shot", "zero-shot"],
    # Infrastructure
    "Embeddings":         ["embedding", "vector store", "semantic search"],
    "Agents":             ["ai agent", "autonomous agent", "agentic"],
    "Context Window":     ["context window", "context length", "long context"],
    # Advanced reasoning
    "Planning":           ["planning", "task decomposition", "goal decomposition"],
    "Self-improvement":   ["self-improvement", "self-evolution", "meta-learning"],
    "Knowledge Graphs":   ["knowledge graph", "ontology", "semantic graph"],
}

CONCEPT_CATEGORY = {
    "Chain of Thought": "reasoning", "ReAct": "reasoning", "Tree of Thought": "reasoning",
    "Reflection": "reasoning", "Planning": "reasoning", "Self-improvement": "reasoning",
    "RAG": "architecture", "Multi-agent": "architecture", "Tool Use": "architecture",
    "Memory": "architecture", "Embeddings": "architecture", "Agents": "architecture",
    "Context Window": "architecture", "Knowledge Graphs": "architecture",
    "RLHF": "training", "Fine-tuning": "training", "Prompt Engineering": "training",
}


def _find_concepts(text: str) -> list:
    text_lower = text.lower()
    found = []
    for concept, keywords in AI_CONCEPTS.items():
        if any(kw in text_lower for kw in keywords):
            found.append(concept)
    return found


async def _upsert_concept(concept: str, data: dict) -> None:
    """Upsert a kb_concepts row. Updates source_count, avg_engagement, trend on conflict."""
    avg_eng = round(data["total_engagement"] / max(data["count"], 1), 2)
    now = datetime.now(timezone.utc).isoformat()

    row = {
        "concept_name":    concept,
        "category":        CONCEPT_CATEGORY.get(concept, "general"),
        "definition":      f"AI concept: {concept}. Detected across {data['count']} sources.",
        "best_source_id":  data.get("best_source_id"),
        "source_count":    data["count"],
        "avg_engagement":  avg_eng,
        "first_seen":      now,
        "trend":           "rising" if avg_eng > 50 else "stable",
        "related_concepts": [],
        "implementations":  [],
    }

    try:
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/kb_concepts",
            headers={**_sbh(svc=True), "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=row,
            params={"on_conflict": "concept_name"},
            timeout=10,
        )
        if r.status_code not in (200, 201):
            print(f"[CONCEPT] Upsert failed {concept}: {r.status_code} {r.text[:100]}")
        else:
            print(f"[CONCEPT] Upserted: {concept} (count={data['count']} avg_eng={avg_eng})")
    except Exception as e:
        print(f"[CONCEPT] Upsert error {concept}: {e}")


async def extract_concepts(sources: list, topic: str) -> list:
    """Find concepts in all sources, upsert to kb_concepts, return list of concept names found."""
    concept_hits = {}  # concept_name -> {source_id, engagement, count}

    for source in sources:
        text = " ".join([
            source.get("title", ""),
            source.get("summary", ""),
            source.get("full_content", "")[:2000],
        ])
        found = _find_concepts(text)
        for concept in found:
            if concept not in concept_hits:
                concept_hits[concept] = {"count": 0, "total_engagement": 0.0, "best_source_id": None, "best_eng": -1}
            eng = source.get("engagement_score", 0)
            concept_hits[concept]["count"] += 1
            concept_hits[concept]["total_engagement"] += eng
            if eng > concept_hits[concept]["best_eng"]:
                concept_hits[concept]["best_eng"] = eng
                concept_hits[concept]["best_source_id"] = source.get("db_id")

    for concept, data in concept_hits.items():
        await _upsert_concept(concept, data)

    return list(concept_hits.keys())
