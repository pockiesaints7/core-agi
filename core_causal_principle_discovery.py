"""Causal-principle discovery tools for CORE.

This module composes the existing world-model primitives into a bounded
discovery packet that can surface causal structure, ranked principles, and
symbolic rule candidates without introducing a new runtime learner.
"""

from __future__ import annotations

import json
import re

from core_tools_world_model import (
    CausalMappingModule,
    HierarchicalSearchTree,
    MetaLearner,
    PredictiveStateRepresentation,
    PrincipleSearchModule,
    SimulatedCritic,
    StateReconciliationBuffer,
    DynamicGatingLayer,
)


def _token_set(text: str) -> set[str]:
    return {part for part in re.split(r"[^A-Za-z0-9_]+", (text or "").lower()) if len(part) >= 3}


def _parse_causal_graph(causal_graph):
    if not causal_graph:
        return {}
    if isinstance(causal_graph, dict):
        return causal_graph
    text = str(causal_graph).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"nodes": parsed}
    except Exception:
        return {"text": text}


def _normalize_nodes(graph: dict, symbols: str = "", actions: str = "") -> list[dict]:
    nodes = []
    for node in graph.get("nodes") or []:
        if isinstance(node, dict):
            nodes.append(node)
        else:
            label = str(node).strip()
            if label:
                nodes.append({"id": label, "label": label, "type": "node"})
    if nodes:
        return nodes
    for item in [symbols, actions]:
        for token in [part.strip() for part in str(item or "").replace("\n", ",").split(",") if part.strip()]:
            nodes.append({"id": token, "label": token, "type": "node"})
    return nodes


def t_causal_principle_discovery(
    causal_graph: str = "",
    context: str = "",
    goal: str = "",
    principles: str = "",
    symbols: str = "",
    actions: str = "",
    task_context: str = "",
    domain: str = "general",
    state_hint: str = "",
    depth: str = "3",
    rollouts: str = "6",
) -> dict:
    """Discover causal/principle candidates from a bounded context packet."""
    try:
        try:
            hz = max(1, min(int(depth or 3), 8))
        except Exception:
            hz = 3
        try:
            ro = max(1, min(int(rollouts or 6), 24))
        except Exception:
            ro = 6

        graph = _parse_causal_graph(causal_graph)
        nodes = _normalize_nodes(graph, symbols=symbols, actions=actions)
        context_parts = [context, goal, task_context, symbols, actions, state_hint]
        context_text = " | ".join([part.strip() for part in context_parts if str(part or "").strip()])
        recon = StateReconciliationBuffer(domain=domain, state_hint=state_hint).reconcile(
            states=[context_text, goal, symbols, actions],
            context=task_context or goal or state_hint,
        )
        causal = CausalMappingModule(domain=domain, state_hint=state_hint).map(
            causal_graph={"nodes": nodes, "edges": graph.get("edges") or []},
            context_embedding=recon.get("normalized_state") or context_text,
            goal=goal or task_context or context_text,
        )
        principle_packet = PrincipleSearchModule(domain=domain, state_hint=state_hint).search(
            principles=principles,
            state=recon.get("normalized_state") or context_text,
            goal=goal or task_context or context_text,
            task_context=context_text or task_context or goal,
        )
        tree = HierarchicalSearchTree(domain=domain, state_hint=state_hint).build(
            current_state=recon.get("normalized_state") or context_text,
            goal=goal or task_context or context_text,
            hwm_levels="low,mid,high",
            candidate_actions=actions or symbols or "inspect,verify,discover",
            horizon=hz,
            rollouts=ro,
            exploration_weight=1.0,
        )
        critic = SimulatedCritic(domain=domain, state_hint=state_hint).score(
            sequence=[context_text, goal, symbols, actions],
            reward_signal="causal principle discovery",
            side_effects="overfit, ambiguity, missing context",
        )
        meta = MetaLearner(
            model=PredictiveStateRepresentation(state=recon.get("normalized_state") or context_text, domain=domain, state_hint=state_hint),
            domain=domain,
            state_hint=state_hint,
            learning_rate=0.12,
        ).adapt(
            error_signal=max(0.0, min(1.0, 1.0 - float(critic.get("score") or 0.0))),
            observation=context_text,
            target=goal or task_context or context_text,
        )

        ranked_principles = principle_packet.get("ranked_principles") or []
        ranked_mappings = causal.get("ranked_mappings") or []
        node_tokens = _token_set(context_text)
        discovery_rules = []
        for mapping in ranked_mappings[:5]:
            label = str(mapping.get("label") or mapping.get("node_id") or "").strip()
            if not label:
                continue
            overlap = len(node_tokens & _token_set(label))
            discovery_rules.append({
                "node": label,
                "score": mapping.get("score") or 0.0,
                "rule": f"if {label} is active then inspect {goal[:80] or task_context[:80] or 'the target'}",
                "overlap": overlap,
            })

        best_principle = principle_packet.get("best_principle") or ""
        best_mapping = causal.get("best_mapping") or {}
        discovery_score = round(max(
            float(critic.get("score") or 0.0),
            float((best_mapping or {}).get("score") or 0.0),
            float((ranked_principles[0].get("score") if ranked_principles else 0.0) or 0.0),
        ), 3)
        summary = (
            f"causal_principle_discovery={'ok' if discovery_rules else 'blocked'} | "
            f"best_principle={best_principle or 'none'} | "
            f"best_node={(best_mapping or {}).get('label') or (best_mapping or {}).get('node_id') or 'none'} | "
            f"score={discovery_score:.2f}"
        )
        blocked = bool(discovery_score < 0.45 or not discovery_rules)
        return {
            "ok": True,
            "blocked": blocked,
            "domain": domain,
            "state_hint": state_hint,
            "goal": goal,
            "context": context_text,
            "causal_mapping": causal,
            "principle_search": principle_packet,
            "search_tree": tree,
            "critic": critic,
            "meta_update": meta,
            "discovery_rules": discovery_rules,
            "ranked_principles": ranked_principles,
            "discovery_score": discovery_score,
            "summary": summary,
        }
    except Exception as exc:
        return {"ok": False, "blocked": True, "error": str(exc), "summary": f"causal_principle_discovery=error | {exc}"}
