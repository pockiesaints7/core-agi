"""core_tools_world_model.py — extracted world-model / graph / temporal tool family.

This module depends on core_tools_reasoning for the shared memory/evaluator
layer and stays independent of core_tools.py.
"""
import ast
import json
import re as _re
from datetime import datetime
from abc import ABC

from core_config import sb_get, sb_post, sb_upsert
from core_tools_reasoning import (
    t_search_memory, t_reasoning_packet, StateEvaluator, t_evaluate_state,
    DynamicRelationalGraph, t_dynamic_relational_graph,
)


def _kb_upsert_world_model(*, domain: str, topic: str, instruction: str, content: str, confidence: str, source_type: str, source_ref: str) -> dict:
    """Minimal KB upsert used by WorldModel.update_model.

    Avoid importing core_tools.py (would cause circular imports). This is a
    bounded substitute for t_kb_update for world-model experience capture.
    """
    payload = {
        "domain": domain,
        "topic": topic,
        "instruction": instruction,
        "content": content,
        "source": "world_model",
        "confidence": confidence or "medium",
        "source_type": source_type or "world_model_update",
        "source_ref": source_ref or "",
        "active": True,
    }
    ok = sb_upsert("knowledge_base", payload, on_conflict="domain,topic")
    return {"ok": bool(ok), "action": "upserted" if ok else "failed", "domain": domain, "topic": topic}


def t_domain_invariant_feature_packet(
    current_state: str = "",
    goal: str = "",
    modules: str = "",
    symbols: str = "",
    actions: str = "",
    task_context: str = "",
    hwm_levels: str = "",
    domain: str = "general",
    state_hint: str = "",
    limit: str = "10",
) -> dict:
    """Extract domain-invariant features for meta-controller and verification work.

    The packet focuses on stable cross-domain signals such as verification pressure,
    actionability, state continuity, transferability, and risk. It is intentionally
    domain-agnostic so task routing and verification can reuse the same structure
    across code, research, review, and ops work.
    """
    try:
        lim = max(1, min(int(limit or 10), 25))
    except Exception:
        lim = 10

    def _split_items(value) -> list[str]:
        if value in (None, "", []):
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        value = parsed
                    else:
                        value = raw
                except Exception:
                    value = raw
            else:
                value = raw
        if isinstance(value, (list, tuple, set)):
            out: list[str] = []
            for item in value:
                text = str(item).strip()
                if text:
                    out.append(text)
            return out
        text = str(value).strip()
        if not text:
            return []
        return [part.strip() for part in _re.split(r"[,\n|]+", text) if part.strip()]

    def _count_terms(text: str, terms: tuple[str, ...]) -> int:
        lower = (text or "").lower()
        return sum(1 for term in terms if term in lower)

    current_state = (current_state or "").strip()
    goal = (goal or "").strip()
    modules_text = (modules or "").strip()
    symbols_text = (symbols or "").strip()
    actions_text = (actions or "").strip()
    task_context = (task_context or "").strip()
    hwm_levels = (hwm_levels or "").strip()
    state_hint = (state_hint or "").strip()

    modules_list = _split_items(modules_text)
    symbols_list = _split_items(symbols_text)
    actions_list = _split_items(actions_text)
    levels_list = _split_items(hwm_levels)

    merged_text = " | ".join(
        part for part in (
            current_state,
            goal,
            task_context,
            modules_text,
            symbols_text,
            actions_text,
            hwm_levels,
            domain,
            state_hint,
        ) if part
    )
    merged_tokens = sorted(_re.findall(r"[a-z0-9_]{3,}", merged_text.lower()))
    state_tokens = set(_re.findall(r"[a-z0-9_]{3,}", current_state.lower()))
    goal_tokens = set(_re.findall(r"[a-z0-9_]{3,}", goal.lower()))
    overlap = sorted((state_tokens & goal_tokens) - {"the", "and", "for", "with", "from", "that", "this"})

    verification_terms = (
        "verify", "verification", "checkpoint", "test", "audit", "validate", "validation",
        "confirm", "close", "closed", "done", "complete", "coverage", "proof",
    )
    state_terms = (
        "state", "continuity", "session", "snapshot", "memory", "context", "checkpoint",
        "resume", "reconcile", "drift", "stale",
    )
    action_terms = (
        "action", "actions", "execute", "implement", "apply", "route", "scan", "build",
        "update", "write", "process", "claim", "queue", "inspect",
    )
    risk_terms = (
        "risk", "failure", "blocked", "stale", "drift", "missing", "duplicate", "conflict",
        "error", "bug", "rollback", "loss", "unsafe",
    )
    transfer_terms = (
        "domain", "invariant", "generalize", "generalization", "transfer", "reuse",
        "meta", "meta-controller", "meta_controller", "cross-domain", "cross domain",
        "shared", "common", "stable", "abstract",
    )

    verification_hits = _count_terms(merged_text, verification_terms)
    state_hits = _count_terms(merged_text, state_terms)
    action_hits = _count_terms(merged_text, action_terms)
    risk_hits = _count_terms(merged_text, risk_terms)
    transfer_hits = _count_terms(merged_text, transfer_terms)
    goal_alignment_hits = len(overlap)
    structural_density = min(1.0, round((len(modules_list) + len(symbols_list) + len(actions_list) + len(levels_list)) / 12.0, 3))

    verification_pressure = min(1.0, round(verification_hits / 5.0, 3))
    continuity_score = min(1.0, round((state_hits + goal_alignment_hits) / 6.0, 3))
    actionability_score = min(1.0, round((action_hits + len(actions_list)) / 6.0, 3))
    transferability_score = min(1.0, round((transfer_hits + len(set(modules_list))) / 7.0, 3))
    risk_pressure = min(1.0, round(risk_hits / 6.0, 3))
    feature_score = round(
        max(
            0.0,
            min(
                1.0,
                (verification_pressure + continuity_score + actionability_score + transferability_score + (1.0 - risk_pressure)) / 5.0,
            ),
        ),
        3,
    )

    feature_ranking = [
        ("verification_pressure", verification_pressure),
        ("continuity", continuity_score),
        ("actionability", actionability_score),
        ("transferability", transferability_score),
        ("risk_pressure", risk_pressure),
        ("goal_alignment", min(1.0, round(goal_alignment_hits / 4.0, 3))),
        ("structural_density", structural_density),
    ]
    feature_ranking.sort(key=lambda item: (item[1], item[0]), reverse=True)
    feature_labels = [name for name, score in feature_ranking if score >= 0.35][:lim]
    if not feature_labels:
        feature_labels = [name for name, _ in feature_ranking[: min(3, len(feature_ranking))]]

    feature_signature = "+".join(feature_labels[:6]) or "domain_invariant"
    summary = (
        f"domain_invariant_features=ok | score={feature_score:.2f} | "
        f"best={feature_signature} | overlap={','.join(overlap[:4]) or 'none'}"
    )
    packet = {
        "current_state": current_state[:1000],
        "goal": goal[:1000],
        "task_context": task_context[:1000],
        "modules": modules_list[:lim],
        "symbols": symbols_list[:lim],
        "actions": actions_list[:lim],
        "hwm_levels": levels_list[:lim],
        "domain": domain,
        "state_hint": state_hint,
        "merged_tokens": merged_tokens[:40],
        "token_overlap": overlap[:16],
        "feature_vector": {
            "verification_pressure": verification_pressure,
            "continuity_score": continuity_score,
            "actionability_score": actionability_score,
            "transferability_score": transferability_score,
            "risk_pressure": risk_pressure,
            "goal_alignment_score": min(1.0, round(goal_alignment_hits / 4.0, 3)),
            "structural_density": structural_density,
            "feature_score": feature_score,
        },
        "feature_labels": feature_labels,
        "feature_signature": feature_signature,
    }
    return {
        "ok": True,
        "domain": domain,
        "state_hint": state_hint,
        "feature_score": feature_score,
        "feature_labels": feature_labels,
        "feature_signature": feature_signature,
        "feature_vector": packet["feature_vector"],
        "packet": packet,
        "summary": summary,
    }


class WorldModelInterface(ABC):
    """Canonical bounded world-model interface.

    The concrete WorldModel below implements this interface and the helper
    wrappers expose the contract to core_tools.py and agentic routing.
    """

    def __init__(self, domain: str = "general", state_hint: str = ""):
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()

    def update_model(self, experience) -> dict:
        raise NotImplementedError

    def predict_future_states(self, current_state, actions, horizon: int = 3) -> dict:
        raise NotImplementedError

    def predict_with_uncertainty(self, current_state, actions, horizon: int = 3) -> dict:
        prediction = self.predict_future_states(current_state=current_state, actions=actions, horizon=horizon)
        predicted_states = prediction.get("predicted_states") or []
        scores = []
        distribution = []
        for item in predicted_states:
            state = item.get("state") if isinstance(item, dict) else {}
            confidence = 0.0
            if isinstance(state, dict):
                try:
                    confidence = float(state.get("confidence") or item.get("confidence") or 0.0)
                except Exception:
                    confidence = 0.0
            else:
                try:
                    confidence = float(item.get("confidence") or 0.0)
                except Exception:
                    confidence = 0.0
            confidence = max(0.0, min(1.0, confidence))
            scores.append(confidence)
            distribution.append({
                "step": item.get("step") if isinstance(item, dict) else None,
                "action": item.get("action") if isinstance(item, dict) else "",
                "confidence": round(confidence, 3),
                "state_preview": (item.get("state_preview") if isinstance(item, dict) else "") or "",
            })
        mean_confidence = round(sum(scores) / len(scores), 3) if scores else 0.0
        variance = 0.0
        if scores:
            variance = round(sum((s - mean_confidence) ** 2 for s in scores) / max(1, len(scores)), 3)
        uncertainty = round(max(0.0, min(1.0, 1.0 - mean_confidence + min(0.5, variance))), 3)
        return {
            "ok": bool(prediction.get("ok", True)),
            "domain": self.domain,
            "state_hint": self.state_hint,
            "prediction": prediction,
            "distribution": distribution,
            "prediction_mean": mean_confidence,
            "prediction_variance": variance,
            "uncertainty": uncertainty,
            "confidence": round(max(0.0, min(1.0, 1.0 - uncertainty)), 3),
            "summary": prediction.get("summary") or f"mean={mean_confidence} | uncertainty={uncertainty}",
        }

class CausalGraph:
    """Build a lightweight causal graph from unified reasoning packets and sequences."""

    def __init__(
        self,
        query: str,
        domain: str = "general",
        tables: list | None = None,
        limit: int = 10,
        per_table: int = 2,
        state_hint: str = "",
        sequence=None,
    ):
        self.query = (query or "").strip()
        self.domain = (domain or "general").strip()
        self.tables = tables
        self.limit = max(1, min(int(limit or 10), 50))
        self.per_table = max(1, min(int(per_table or 2), 5))
        self.state_hint = (state_hint or "").strip()
        self.sequence = sequence
        self.nodes = {}
        self.edges = []
        self._beliefs = {}

    @staticmethod
    def _normalize_sequence(sequence) -> list[str]:
        if sequence in (None, "", []):
            return []
        if isinstance(sequence, str):
            raw = sequence.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
                except Exception:
                    try:
                        parsed = ast.literal_eval(raw)
                        if isinstance(parsed, (list, tuple, set)):
                            return [str(item).strip() for item in parsed if str(item).strip()]
                    except Exception:
                        pass
            return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
        if isinstance(sequence, dict):
            sequence = [sequence]
        if isinstance(sequence, (list, tuple, set)):
            items = []
            for item in sequence:
                if isinstance(item, dict):
                    parts = []
                    for key in ("state", "current_state", "context", "summary", "value", "action", "title", "description"):
                        value = item.get(key)
                        if value:
                            parts.append(str(value))
                    text = " | ".join(parts).strip() or json.dumps(item, ensure_ascii=False, sort_keys=True)
                else:
                    text = str(item).strip()
                if text:
                    items.append(text[:1000])
            return items
        text = str(sequence).strip()
        return [text] if text else []

    @staticmethod
    def _node_id(table: str, raw_id, title: str) -> str:
        safe_title = _re.sub(r"[^a-z0-9]+", "-", (title or "item").lower()).strip("-")[:24]
        return f"{table}:{raw_id}:{safe_title or 'node'}"

    @staticmethod
    def _node_type(table: str) -> str:
        return {
            "knowledge_base": "memory",
            "behavioral_rules": "rule",
            "mistakes": "mistake",
            "hot_reflections": "reflection",
            "output_reflections": "reflection",
            "evolution_queue": "evolution",
            "conversation_episodes": "episode",
            "sessions": "session",
            "pattern_frequency": "pattern",
            "repo_components": "module",
            "repo_component_chunks": "chunk",
            "repo_component_edges": "edge",
        }.get(table, "memory")

    @staticmethod
    def _token_set(text: str) -> set[str]:
        tokens = []
        for part in _re.split(r"[^A-Za-z0-9_]+", (text or "").lower()):
            part = part.strip()
            if len(part) >= 3:
                tokens.append(part)
        return set(tokens)

    def add_edge(self, source: str, target: str, weight: float = 0.5, relation: str = "causes") -> None:
        if not source or not target or source == target:
            return
        w = max(0.05, min(float(weight or 0.5), 1.0))
        self.edges.append({
            "source": source,
            "target": target,
            "relation": relation,
            "weight": round(w, 3),
        })

    def get_children(self, node_id: str) -> list[dict]:
        children = [e for e in self.edges if e.get("source") == node_id]
        children.sort(key=lambda item: (float(item.get("weight") or 0.0), item.get("target", "")), reverse=True)
        return children

    def propagate_belief(self, start: str, belief: float = 1.0, decay: float = 0.82, max_depth: int = 3) -> dict:
        decay = max(0.1, min(float(decay or 0.82), 0.99))
        max_depth = max(1, min(int(max_depth or 3), 6))
        beliefs = {start: round(max(0.0, min(1.0, belief)), 3)}
        frontier = [(start, belief, 0)]
        while frontier:
            node, current_belief, depth = frontier.pop(0)
            if depth >= max_depth:
                continue
            for edge in self.get_children(node):
                child = edge.get("target")
                weight = float(edge.get("weight") or 0.0)
                next_belief = round(max(0.0, min(1.0, current_belief * weight * decay)), 3)
                if next_belief <= 0:
                    continue
                if next_belief > beliefs.get(child, 0.0):
                    beliefs[child] = next_belief
                    frontier.append((child, next_belief, depth + 1))
        self._beliefs = beliefs
        return beliefs

    def find_causal_path(self, start: str, target: str, max_depth: int = 6) -> list[str]:
        max_depth = max(1, min(int(max_depth or 6), 10))
        queue = [(start, [start])]
        seen = {start}
        while queue:
            node, path = queue.pop(0)
            if node == target:
                return path
            if len(path) >= max_depth:
                continue
            for edge in self.get_children(node):
                child = edge.get("target")
                if child in seen:
                    continue
                seen.add(child)
                queue.append((child, path + [child]))
        return []

    def _score_edge(self, source_text: str, target_text: str, base_score: float = 0.5) -> float:
        source_tokens = self._token_set(source_text)
        target_tokens = self._token_set(target_text)
        overlap = len(source_tokens & target_tokens)
        score = float(base_score or 0.5)
        if overlap:
            score += min(0.25, 0.05 * overlap)
        target_lower = (target_text or "").lower()
        if any(term in target_lower for term in ("cause", "causal", "because", "dependency", "impact", "risk", "failure", "integration")):
            score += 0.1
        if any(term in target_lower for term in ("critical", "urgent", "error", "blocked", "conflict")):
            score += 0.05
        return max(0.05, min(score, 1.0))

    def build(self, sequence=None) -> dict:
        from core_reasoning_packet import build_reasoning_packet

        sequence_items = self._normalize_sequence(sequence if sequence is not None else self.sequence)
        packet = build_reasoning_packet(
            self.query,
            domain=self.domain,
            tables=self.tables,
            limit=self.limit,
            per_table=self.per_table,
        )
        pkt = packet.get("packet") or {}
        hits = pkt.get("top_hits") or []
        by_table = pkt.get("memory_by_table") or {}

        nodes = [{
            "id": "query",
            "label": self.query[:120],
            "type": "query",
            "table": "query",
            "score": 1.0,
        }]
        edges = []
        seen = {"query"}
        ordering = []

        for idx, hit in enumerate(hits):
            table = hit.get("table") or "unknown"
            raw = hit.get("raw") or {}
            raw_id = raw.get("id") or hit.get("id") or hit.get("topic") or hit.get("title") or "0"
            title = hit.get("title") or hit.get("body") or str(raw_id)
            node_id = self._node_id(table, raw_id, title)
            if node_id in seen:
                continue
            seen.add(node_id)
            score = round(float(hit.get("score") or hit.get("semantic_score") or 0.0), 3)
            node = {
                "id": node_id,
                "label": str(title)[:120],
                "type": self._node_type(table),
                "table": table,
                "score": score,
                "raw_id": raw_id,
            }
            nodes.append(node)
            ordering.append(node_id)
            edges.append({
                "source": "query",
                "target": node_id,
                "relation": "retrieved_from",
                "weight": round(self._score_edge(self.query, node["label"], max(0.15, score)), 3),
            })
            if idx > 0:
                prev = ordering[idx - 1]
                prev_node = next((n for n in nodes if n.get("id") == prev), None)
                prev_text = prev_node.get("label", "") if prev_node else self.query
                edges.append({
                    "source": prev,
                    "target": node_id,
                    "relation": "causal_sequence",
                    "weight": round(self._score_edge(prev_text, node["label"], 0.35), 3),
                })

        if sequence_items:
            seq_nodes = []
            for idx, text in enumerate(sequence_items[: max(2, min(12, self.limit + 2))]):
                node_id = f"seq:{idx}:{_re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:18] or 'node'}"
                if node_id in seen:
                    continue
                seen.add(node_id)
                node = {
                    "id": node_id,
                    "label": text[:120],
                    "type": "sequence",
                    "table": "sequence",
                    "score": round(max(0.15, 1.0 - (idx * 0.08)), 3),
                    "raw_id": idx,
                }
                nodes.append(node)
                seq_nodes.append(node_id)
                edges.append({
                    "source": "query",
                    "target": node_id,
                    "relation": "sequence_anchor",
                    "weight": round(self._score_edge(self.query, text, 0.45), 3),
                })
            for left, right in zip(seq_nodes, seq_nodes[1:]):
                left_node = next((n for n in nodes if n.get("id") == left), None)
                right_node = next((n for n in nodes if n.get("id") == right), None)
                left_text = left_node.get("label", "") if left_node else ""
                right_text = right_node.get("label", "") if right_node else ""
                edges.append({
                    "source": left,
                    "target": right,
                    "relation": "sequence_flow",
                    "weight": round(self._score_edge(left_text, right_text, 0.42), 3),
                })

        self.nodes = {node["id"]: node for node in nodes}
        self.edges = edges
        beliefs = self.propagate_belief("query", belief=1.0, decay=0.84, max_depth=4)
        causal_order = sorted(
            [
                {
                    "id": node["id"],
                    "text": node["label"],
                    "table": node.get("table"),
                    "belief": round(float(beliefs.get(node["id"], node.get("score", 0.0))), 3),
                }
                for node in nodes
                if node["id"] != "query"
            ],
            key=lambda item: (item["belief"], item["text"]),
            reverse=True,
        )
        causal_summary = " | ".join(item["text"][:120] for item in causal_order[:3])
        dominant_cause = causal_order[0]["table"] if causal_order else None
        causal_paths = {}
        for item in causal_order[:5]:
            causal_paths[item["id"]] = self.find_causal_path("query", item["id"], max_depth=6)

        graph = {
            "ok": True,
            "query": self.query,
            "domain": self.domain,
            "context": pkt.get("context", ""),
            "focus": pkt.get("focus", ""),
            "memory_by_table": by_table,
            "state_hint": self.state_hint,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "nodes": nodes,
            "edges": edges,
            "beliefs": beliefs,
            "causal_order": causal_order,
            "causal_summary": causal_summary,
            "causal_paths": causal_paths,
            "dominant_cause": dominant_cause,
        }
        graph["density"] = round(len(edges) / max(1, len(nodes)), 3)
        graph["dominant_table"] = max(by_table.items(), key=lambda kv: int(kv[1] or 0))[0] if by_table else None
        return graph


def t_causal_graph(query: str = "", domain: str = "general", tables: str = "", limit: str = "10", per_table: str = "2", state_hint: str = "", sequence: str = "") -> dict:
    """Build a causal graph from unified memory context or a provided sequence."""
    try:
        if not query:
            return {"ok": False, "error": "query required"}
        try:
            lim = max(1, min(int(limit), 50))
        except Exception:
            lim = 10
        try:
            pt = max(1, min(int(per_table), 5))
        except Exception:
            pt = 2
        table_list = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
        return CausalGraph(
            query=query,
            domain=domain,
            tables=table_list,
            limit=lim,
            per_table=pt,
            state_hint=state_hint,
            sequence=sequence or None,
        ).build()
    except Exception as e:
        return {"ok": False, "error": str(e)}


class CausalGraphInference:
    """Fuse causal, relational, and state-evaluation signals into a transition model."""

    def __init__(
        self,
        query: str,
        domain: str = "general",
        tables: list | None = None,
        limit: int = 10,
        per_table: int = 2,
        state_hint: str = "",
        sequence=None,
        candidate_actions=None,
        horizon: int = 3,
    ):
        self.query = (query or "").strip()
        self.domain = (domain or "general").strip()
        self.tables = tables
        self.limit = max(1, min(int(limit or 10), 50))
        self.per_table = max(1, min(int(per_table or 2), 5))
        self.state_hint = (state_hint or "").strip()
        self.sequence = sequence
        self.candidate_actions = candidate_actions
        self.horizon = max(1, min(int(horizon or 3), 8))

    @staticmethod
    def _normalize_actions(actions) -> list[str]:
        if actions in (None, "", []):
            return []
        if isinstance(actions, str):
            raw = actions.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
                except Exception:
                    try:
                        parsed = ast.literal_eval(raw)
                        if isinstance(parsed, (list, tuple, set)):
                            return [str(item).strip() for item in parsed if str(item).strip()]
                    except Exception:
                        pass
            return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
        if isinstance(actions, dict):
            actions = [actions]
        if isinstance(actions, (list, tuple, set)):
            items = []
            for item in actions:
                if isinstance(item, dict):
                    parts = []
                    for key in ("state", "current_state", "context", "summary", "value", "action", "title", "description"):
                        value = item.get(key)
                        if value:
                            parts.append(str(value))
                    text = " | ".join(parts).strip() or json.dumps(item, ensure_ascii=False, sort_keys=True)
                else:
                    text = str(item).strip()
                if text:
                    items.append(text[:1000])
            return items
        text = str(actions).strip()
        return [text] if text else []

    @staticmethod
    def _text(item) -> str:
        if isinstance(item, dict):
            for key in ("text", "title", "action", "summary", "label"):
                value = item.get(key)
                if value:
                    return str(value).strip()
        return str(item).strip()

    def build(self) -> dict:
        sequence = CausalGraph._normalize_sequence(self.sequence)
        if not sequence and self.query:
            sequence = [self.query]
        actions = self._normalize_actions(self.candidate_actions)
        if not actions and len(sequence) > 1:
            actions = [item for item in sequence[1:] if item]
        if not actions:
            actions = ["observe", "assess", "proceed"]

        causal = CausalGraph(
            query=self.query,
            domain=self.domain,
            tables=self.tables,
            limit=self.limit,
            per_table=self.per_table,
            state_hint=self.state_hint,
            sequence=sequence or None,
        ).build(sequence=sequence or None)
        relational = DynamicRelationalGraph(
            query=self.query,
            domain=self.domain,
            tables=self.tables,
            limit=self.limit,
            per_table=self.per_table,
            state_hint=self.state_hint,
        ).build()
        evaluator = StateEvaluator(query=self.query, domain=self.domain, state_hint=self.state_hint)
        evaluation = evaluator.evaluate() if hasattr(evaluator, "evaluate") else {"ok": False}

        readiness = float(evaluation.get("readiness_score") or 0.0)
        coherence = float(evaluation.get("coherence_score") or 0.0)
        risk = float(evaluation.get("risk_score") or 0.0)
        recommendation = evaluation.get("recommendation") or "defer"
        dominant_table = relational.get("dominant_table")
        density = float(relational.get("density") or causal.get("density") or 0.0)

        causal_order = causal.get("causal_order") or []
        causal_rank = {}
        causal_support = {}
        total_order = max(1, len(causal_order))
        for idx, item in enumerate(causal_order):
            text = self._text(item)
            if not text:
                continue
            causal_rank[text] = idx
            causal_support[text] = max(0.0, 1.0 - (idx / total_order))

        ranked_transitions = []
        for idx, action in enumerate(actions):
            action_text = self._text(action)
            if not action_text:
                continue
            lower = action_text.lower()
            causal_score = causal_support.get(action_text, 0.0)
            if not causal_score:
                for key, value in causal_support.items():
                    if key.lower() in lower or lower in key.lower():
                        causal_score = max(causal_score, value)
            relational_score = min(0.35, density + (0.15 if dominant_table and dominant_table in lower else 0.0))
            if any(term in lower for term in ("observe", "inspect", "audit")):
                relational_score = min(0.35, relational_score + 0.05)
            combined = (
                (0.45 * causal_score)
                + (0.25 * readiness)
                + (0.15 * coherence)
                + (0.15 * max(0.0, 1.0 - risk))
                + (0.05 * relational_score)
            )
            combined = max(0.0, min(1.0, combined))
            rationale_bits = [
                f"causal={round(causal_score, 3)}",
                f"relational={round(relational_score, 3)}",
                f"readiness={round(readiness, 3)}",
                f"risk={round(risk, 3)}",
            ]
            if recommendation:
                rationale_bits.append(f"recommendation={recommendation}")
            ranked_transitions.append({
                "index": idx,
                "action": action_text,
                "score": round(combined, 3),
                "causal_support": round(causal_score, 3),
                "relational_support": round(relational_score, 3),
                "readiness": round(readiness, 3),
                "coherence": round(coherence, 3),
                "risk": round(risk, 3),
                "rationale": "; ".join(rationale_bits),
            })

        ranked_transitions.sort(key=lambda item: (item["score"], item["causal_support"], item["relational_support"], item["action"]), reverse=True)
        best_transition = ranked_transitions[0] if ranked_transitions else {}
        transition_summary = " | ".join([
            causal.get("causal_summary", "")[:120],
            relational.get("summary", "")[:120],
            str(recommendation)[:60],
        ]).strip(" |")
        return {
            "ok": True,
            "query": self.query,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "horizon": self.horizon,
            "causal_graph": causal,
            "relational_graph": relational,
            "state_evaluation": evaluation,
            "candidate_actions": actions,
            "ranked_transitions": ranked_transitions,
            "best_transition": best_transition,
            "best_action": best_transition.get("action"),
            "graph_density": round(density, 3),
            "dominant_table": dominant_table,
            "summary": transition_summary[:500],
        }


def t_causal_graph_inference(query: str = "", domain: str = "general", tables: str = "", limit: str = "10", per_table: str = "2", state_hint: str = "", sequence: str = "", candidate_actions: str = "", horizon: str = "3") -> dict:
    """Integrate causal graph inference with relational graph and state evaluation."""
    try:
        if not query:
            return {"ok": False, "error": "query required"}
        try:
            lim = max(1, min(int(limit), 50))
        except Exception:
            lim = 10
        try:
            pt = max(1, min(int(per_table), 5))
        except Exception:
            pt = 2
        try:
            hz = max(1, min(int(horizon), 8))
        except Exception:
            hz = 3
        table_list = [t.strip() for t in tables.split(",") if t.strip()] if tables else None
        return CausalGraphInference(
            query=query,
            domain=domain,
            tables=table_list,
            limit=lim,
            per_table=pt,
            state_hint=state_hint,
            sequence=sequence or None,
            candidate_actions=candidate_actions or None,
            horizon=hz,
        ).build()
    except Exception as e:
        return {"ok": False, "error": str(e)}


class MetaContextualRouter:
    """Route a current state toward a distribution over hierarchical world-model levels."""

    def __init__(
        self,
        current_state,
        goal,
        hwm_levels,
        domain: str = "general",
        state_hint: str = "",
        limit: int = 10,
    ):
        self.current_state = current_state
        self.goal = goal
        self.hwm_levels = hwm_levels
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()
        self.limit = max(1, min(int(limit or 10), 25))

    @staticmethod
    def _normalize_levels(hwm_levels) -> list[str]:
        if hwm_levels in (None, "", []):
            return []
        if isinstance(hwm_levels, str):
            raw = hwm_levels.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
                except Exception:
                    try:
                        parsed = ast.literal_eval(raw)
                        if isinstance(parsed, (list, tuple, set)):
                            return [str(item).strip() for item in parsed if str(item).strip()]
                    except Exception:
                        pass
            return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
        if isinstance(hwm_levels, dict):
            hwm_levels = [hwm_levels]
        if isinstance(hwm_levels, (list, tuple, set)):
            items = []
            for item in hwm_levels:
                if isinstance(item, dict):
                    for key in ("level", "name", "label", "title", "value", "text"):
                        value = item.get(key)
                        if value:
                            items.append(str(value).strip())
                            break
                    else:
                        items.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
                else:
                    text = str(item).strip()
                    if text:
                        items.append(text)
            return items
        text = str(hwm_levels).strip()
        return [text] if text else []

    @staticmethod
    def _level_text(level) -> str:
        if isinstance(level, dict):
            for key in ("level", "name", "label", "title", "value", "text"):
                value = level.get(key)
                if value:
                    return str(value).strip()
            return json.dumps(level, ensure_ascii=False, sort_keys=True)
        return str(level).strip()

    def route(self) -> dict:
        levels = self._normalize_levels(self.hwm_levels)
        if not levels:
            return {"ok": False, "error": "hwm_levels required"}

        current_text = WorldModel._textify(WorldModel._parse_blob(self.current_state)) if self.current_state else ""
        goal_text = WorldModel._textify(WorldModel._parse_blob(self.goal)) if isinstance(self.goal, (dict, str)) else str(self.goal or "")
        feature_packet = t_domain_invariant_feature_packet(
            current_state=current_text,
            goal=goal_text,
            hwm_levels=",".join(levels),
            domain=self.domain,
            state_hint=self.state_hint,
            limit=str(min(self.limit, len(levels) + 4)),
        )
        query = " | ".join(part for part in (current_text, goal_text) if part).strip() or goal_text or current_text
        packet = t_reasoning_packet(query=query, domain=self.domain, limit=str(min(self.limit, len(levels) + 4)), tables="")
        evaluation = StateEvaluator(query=query, domain=self.domain, state_hint=self.state_hint).evaluate()

        packet_context = " ".join([
            str(packet.get("focus") or ""),
            str(packet.get("context") or ""),
            str(packet.get("summary") or ""),
            str(feature_packet.get("feature_signature") or ""),
            str(feature_packet.get("summary") or ""),
        ]).lower()
        goal_lower = goal_text.lower()
        current_lower = current_text.lower()
        readiness = float(evaluation.get("readiness_score") or 0.0)
        coherence = float(evaluation.get("coherence_score") or 0.0)
        risk = float(evaluation.get("risk_score") or 0.0)
        feature_score = float(feature_packet.get("feature_score") or 0.0)
        feature_vector = feature_packet.get("feature_vector") or {}
        if feature_score >= 0.7:
            readiness = min(1.0, readiness + 0.03)
        elif feature_score <= 0.35:
            risk = min(1.0, risk + 0.03)
        if float(feature_vector.get("verification_pressure") or 0.0) >= 0.6:
            readiness = min(1.0, readiness + 0.02)
        level_scores = []
        for idx, level in enumerate(levels):
            label = self._level_text(level)
            lower = label.lower()
            lexical = 0.0
            for source in (goal_lower, current_lower, packet_context):
                if not source:
                    continue
                if lower in source:
                    lexical += 0.45
                else:
                    tokens = set(part for part in _re.split(r"[^a-z0-9_]+", lower) if part)
                    source_tokens = set(part for part in _re.split(r"[^a-z0-9_]+", source) if part)
                    lexical += min(0.2, 0.05 * len(tokens & source_tokens))
            if any(term in lower for term in ("low", "small", "narrow", "specific")):
                lexical += 0.03
            if any(term in lower for term in ("high", "broad", "global", "abstract")):
                lexical += 0.02
            if any(term in goal_lower for term in ("stabil", "safe", "risk", "review")) and any(term in lower for term in ("safe", "stable", "review", "control")):
                lexical += 0.12
            if any(term in goal_lower for term in ("explore", "discover", "learn", "search")) and any(term in lower for term in ("explore", "search", "discover", "learn")):
                lexical += 0.12
            structural = max(0.0, 1.0 - (idx / max(1, len(levels))))
            score = (0.45 * lexical) + (0.2 * readiness) + (0.15 * coherence) + (0.1 * max(0.0, 1.0 - risk)) + (0.1 * structural)
            score = max(0.0, min(1.0, score))
            level_scores.append({
                "index": idx,
                "level": label,
                "score": round(score, 3),
                "lexical_support": round(lexical, 3),
                "readiness": round(readiness, 3),
                "coherence": round(coherence, 3),
                "risk": round(risk, 3),
            })

        total = sum(max(item["score"], 0.001) for item in level_scores) or 1.0
        for item in level_scores:
            item["probability"] = round(max(item["score"], 0.001) / total, 4)
        level_scores.sort(key=lambda item: (item["score"], item["probability"], item["level"]), reverse=True)
        best = level_scores[0] if level_scores else {}
        summary = " | ".join([
            f"goal={goal_text[:120]}",
            f"best={best.get('level','')}",
            f"readiness={round(readiness, 3)}",
            f"risk={round(risk, 3)}",
            f"feature={feature_packet.get('feature_signature', '')[:80]}",
        ]).strip(" |")
        return {
            "ok": True,
            "current_state": current_text[:1000],
            "goal": goal_text[:1000],
            "domain": self.domain,
            "state_hint": self.state_hint,
            "hwm_levels": levels,
            "level_distribution": level_scores,
            "best_level": best.get("level"),
            "summary": summary[:500],
            "state_evaluation": evaluation,
            "domain_invariant_features": feature_packet,
            "feature_score": feature_score,
            "reasoning_packet": packet,
        }


def t_meta_contextual_router(current_state: str = "", goal: str = "", hwm_levels: str = "", domain: str = "general", state_hint: str = "", limit: str = "10") -> dict:
    """Route a state toward a distribution over HWM levels."""
    try:
        if not current_state and not goal:
            return {"ok": False, "error": "current_state or goal required"}
        try:
            lim = max(1, min(int(limit), 25))
        except Exception:
            lim = 10
        return MetaContextualRouter(
            current_state=current_state,
            goal=goal,
            hwm_levels=hwm_levels,
            domain=domain,
            state_hint=state_hint,
            limit=lim,
        ).route()
    except Exception as e:
        return {"ok": False, "error": str(e)}


class MonteCarloTreeSearch:
    """Flexible MCTS bridge over CORE reasoning packets.

    The bridge prefers an injected external MonteCarloTreeSearch class when a
    class_path is provided. Otherwise it falls back to a deterministic, read-only
    internal search that uses StateEvaluator + the unified reasoning packet.
    """

    def __init__(
        self,
        query: str,
        domain: str = "general",
        tables: list | None = None,
        limit: int = 10,
        per_table: int = 2,
        state_hint: str = "",
        candidate_actions: list | None = None,
        rollouts: int = 12,
        exploration_weight: float = 1.2,
        class_path: str = "",
    ):
        self.query = (query or "").strip()
        self.domain = (domain or "general").strip()
        self.tables = tables
        self.limit = max(1, min(int(limit or 10), 50))
        self.per_table = max(1, min(int(per_table or 2), 5))
        self.state_hint = (state_hint or "").strip()
        self.candidate_actions = self._normalize_actions(candidate_actions)
        self.rollouts = max(1, min(int(rollouts or 12), 64))
        try:
            self.exploration_weight = max(0.1, min(float(exploration_weight or 1.2), 4.0))
        except Exception:
            self.exploration_weight = 1.2
        self.class_path = (class_path or "").strip()

    @staticmethod
    def _normalize_actions(candidate_actions) -> list[str]:
        if candidate_actions in (None, "", []):
            return []
        if isinstance(candidate_actions, str):
            raw = candidate_actions.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
                except Exception:
                    pass
            return [part.strip() for part in raw.split(",") if part.strip()]
        if isinstance(candidate_actions, (list, tuple, set)):
            return [str(item).strip() for item in candidate_actions if str(item).strip()]
        return [str(candidate_actions).strip()] if str(candidate_actions).strip() else []

    @staticmethod
    def _call_with_fallback(fn, *attempts):
        last_error = None
        for args, kwargs in attempts:
            try:
                return fn(*args, **kwargs)
            except TypeError as e:
                last_error = e
            except Exception as e:
                last_error = e
        if last_error:
            raise last_error
        return None

    def _load_external_class(self):
        if not self.class_path:
            return None
        try:
            import importlib
            module_name, _, class_name = self.class_path.rpartition(".")
            if not module_name or not class_name:
                return None
            module = importlib.import_module(module_name)
            return getattr(module, class_name, None)
        except Exception:
            return None

    @staticmethod
    def _score_from_statecard(statecard: dict, graph: dict | None = None) -> float:
        readiness = float(statecard.get("readiness_score") or 0.0)
        confidence = float(statecard.get("confidence") or 0.0)
        coherence = float(statecard.get("coherence_score") or 0.0)
        evidence = float(statecard.get("evidence_score") or 0.0)
        risk = float(statecard.get("risk_score") or 0.0)
        density = float((graph or {}).get("density") or 0.0)
        score = (readiness * 0.48) + (confidence * 0.18) + (coherence * 0.12) + (evidence * 0.12) + (min(1.0, density) * 0.10) - (risk * 0.20)
        return round(max(0.0, min(1.0, score)), 3)

    def _evaluate_action(self, action: str, graph: dict | None = None) -> dict:
        query = f"{self.query}\nACTION: {action}"
        scorecard = StateEvaluator(
            query=query,
            domain=self.domain,
            tables=self.tables,
            limit=self.limit,
            per_table=self.per_table,
            state_hint=self.state_hint,
        ).evaluate()
        score = self._score_from_statecard(scorecard, graph=graph)
        return {
            "action": action,
            "score": score,
            "statecard": scorecard,
            "recommendation": scorecard.get("recommendation", "reassess"),
        }

    def _external_run(self, packet: dict, graph: dict) -> dict | None:
        cls = self._load_external_class()
        if not cls:
            return None
        actions = self.candidate_actions or ["proceed", "reassess", "defer"]
        payload = {
            "query": self.query,
            "packet": packet,
            "graph": graph,
            "domain": self.domain,
            "tables": self.tables,
            "candidate_actions": actions,
            "state_hint": self.state_hint,
            "rollouts": self.rollouts,
            "exploration_weight": self.exploration_weight,
        }
        instance = None
        for ctor_args, ctor_kwargs in [
            ((), payload),
            ((payload,), {}),
            ((), {}),
        ]:
            try:
                instance = cls(*ctor_args, **ctor_kwargs)
                break
            except TypeError:
                continue
            except Exception:
                continue
        if instance is None:
            return None
        for method_name in ("run", "search", "plan", "evaluate"):
            method = getattr(instance, method_name, None)
            if not callable(method):
                continue
            attempts = [
                ((), {"packet": packet, "graph": graph, "candidate_actions": actions, "state_hint": self.state_hint, "query": self.query}),
                ((packet, graph, actions), {}),
                ((packet,), {}),
                ((), {}),
            ]
            try:
                result = self._call_with_fallback(method, *attempts)
                if isinstance(result, dict):
                    result.setdefault("external_integration", True)
                    result.setdefault("class_path", self.class_path)
                    return result
                return {
                    "ok": True,
                    "external_integration": True,
                    "class_path": self.class_path,
                    "result": result,
                    "actions": actions,
                }
            except Exception:
                continue
        return None

    def _internal_run(self, packet: dict, graph: dict) -> dict:
        actions = self.candidate_actions or ["proceed", "reassess", "defer"]
        nodes = {
            action: {
                "action": action,
                "visits": 0,
                "value": 0.0,
                "avg_value": 0.0,
                "last_score": 0.0,
                "recommendation": "",
                "statecard": {},
            }
            for action in actions
        }
        trace = []
        for rollout in range(self.rollouts):
            total_visits = sum(node["visits"] for node in nodes.values())
            if rollout < len(actions):
                action = actions[rollout]
            else:
                def _uct_score(node: dict) -> float:
                    if node["visits"] <= 0:
                        return 10.0
                    exploitation = node["avg_value"]
                    exploration = self.exploration_weight * ((__import__("math").log(total_visits + 1) / node["visits"]) ** 0.5)
                    return exploitation + exploration

                action = max(actions, key=lambda act: _uct_score(nodes[act]))
            evaluated = self._evaluate_action(action, graph=graph)
            node = nodes[action]
            node["visits"] += 1
            node["value"] += float(evaluated["score"] or 0.0)
            node["avg_value"] = round(node["value"] / max(1, node["visits"]), 3)
            node["last_score"] = float(evaluated["score"] or 0.0)
            node["recommendation"] = evaluated.get("recommendation", "")
            node["statecard"] = evaluated.get("statecard") or {}
            trace.append({
                "rollout": rollout + 1,
                "action": action,
                "score": evaluated["score"],
                "recommendation": evaluated.get("recommendation", ""),
            })
        best = max(nodes.values(), key=lambda n: (n["avg_value"], n["visits"], n["last_score"]))
        return {
            "ok": True,
            "query": self.query,
            "domain": self.domain,
            "class_path": self.class_path,
            "external_integration": False,
            "candidate_actions": actions,
            "rollouts": self.rollouts,
            "exploration_weight": self.exploration_weight,
            "packet_focus": packet.get("packet", {}).get("focus", ""),
            "packet_context": packet.get("packet", {}).get("context", ""),
            "graph_density": graph.get("density", 0.0),
            "graph_dominant_table": graph.get("dominant_table"),
            "best_action": best["action"],
            "best_score": round(float(best["avg_value"] or 0.0), 3),
            "nodes": list(nodes.values()),
            "trace": trace,
        }

    def run(self) -> dict:
        if not self.query:
            return {"ok": False, "error": "query required"}
        statecard = StateEvaluator(
            query=self.query,
            domain=self.domain,
            tables=self.tables,
            limit=self.limit,
            per_table=self.per_table,
            state_hint=self.state_hint,
        ).evaluate()
        from core_reasoning_packet import build_reasoning_packet
        reasoning_packet = build_reasoning_packet(
            query=self.query,
            domain=self.domain,
            tables=self.tables,
            limit=self.limit,
            per_table=self.per_table,
        )
        graph = DynamicRelationalGraph(
            query=self.query,
            domain=self.domain,
            tables=self.tables,
            limit=self.limit,
            per_table=self.per_table,
            state_hint=self.state_hint,
        ).build()
        external = self._external_run(reasoning_packet, graph)
        if external is not None:
            if isinstance(external, dict):
                external.setdefault("query", self.query)
                external.setdefault("domain", self.domain)
                external.setdefault("class_path", self.class_path)
                external.setdefault("state_recommendation", statecard.get("recommendation", ""))
                external.setdefault("graph_density", graph.get("density", 0.0))
                external.setdefault("graph_dominant_table", graph.get("dominant_table"))
            return external
        return self._internal_run(reasoning_packet, graph)


def t_monte_carlo_tree_search(
    query: str = "",
    domain: str = "general",
    tables: str = "",
    limit: str = "10",
    per_table: str = "2",
    state_hint: str = "",
    candidate_actions: str = "",
    rollouts: str = "12",
    exploration_weight: str = "1.2",
    class_path: str = "",
) -> dict:
    """Run a flexible MCTS bridge over CORE's unified reasoning packet."""
    try:
        if not query:
            return {"ok": False, "error": "query required"}
        try:
            lim = max(1, min(int(limit), 50))
        except Exception:
            lim = 10
        try:
            pt = max(1, min(int(per_table), 5))
        except Exception:
            pt = 2
        try:
            ro = max(1, min(int(rollouts), 64))
        except Exception:
            ro = 12
        try:
            ew = max(0.1, min(float(exploration_weight), 4.0))
        except Exception:
            ew = 1.2
        actions = None
        if candidate_actions:
            raw = candidate_actions.strip()
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        actions = [str(item).strip() for item in parsed if str(item).strip()]
                except Exception:
                    actions = [part.strip() for part in raw.split(",") if part.strip()]
            else:
                actions = [part.strip() for part in raw.split(",") if part.strip()]
        return MonteCarloTreeSearch(
            query=query,
            domain=domain,
            tables=[t.strip() for t in tables.split(",") if t.strip()] if tables else None,
            limit=lim,
            per_table=pt,
            state_hint=state_hint,
            candidate_actions=actions,
            rollouts=ro,
            exploration_weight=ew,
            class_path=class_path,
        ).run()
    except Exception as e:
        return {"ok": False, "error": str(e)}


class WorldModel(WorldModelInterface):
    """Bounded world-model interface for experience updates and forward rollouts."""

    def __init__(self, domain: str = "general", state_hint: str = ""):
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()

    @staticmethod
    def _parse_blob(blob):
        if isinstance(blob, dict):
            return blob
        if blob in (None, "", []):
            return {}
        if isinstance(blob, str):
            raw = blob.strip()
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {"value": parsed}
            except Exception:
                return {"value": raw}
        return {"value": blob}

    @staticmethod
    def _textify(state: dict) -> str:
        parts = []
        for key in ("state", "current_state", "context", "summary", "value"):
            value = state.get(key)
            if value:
                parts.append(f"{key}={value}")
        if not parts:
            parts.append(str(state)[:500])
        return " | ".join(parts)[:1000]

class AdaptiveTemporalFilter:
    """Bounded temporal filter for smoothing recent context and ranking sequences."""

    def __init__(self, domain: str = "general", state_hint: str = "", window: int = 5, decay: float = 0.82):
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()
        self.window = max(1, min(int(window or 5), 25))
        self.decay = max(0.1, min(float(decay or 0.82), 0.99))

    @staticmethod
    def _normalize_items(sequence) -> list[str]:
        if sequence in (None, "", []):
            return []
        if isinstance(sequence, str):
            raw = sequence.strip()
            if not raw:
                return []
            try:
                parsed = json.loads(raw)
                sequence = parsed if isinstance(parsed, (list, tuple, set)) else [parsed]
            except Exception:
                sequence = [part.strip() for part in raw.split("\n") if part.strip()]
        if isinstance(sequence, dict):
            sequence = [sequence]
        if not isinstance(sequence, (list, tuple, set)):
            sequence = [sequence]
        items = []
        for item in sequence:
            if isinstance(item, dict):
                parts = []
                for key in ("state", "current_state", "context", "summary", "value", "action", "title", "description"):
                    value = item.get(key)
                    if value:
                        parts.append(str(value))
                text = " | ".join(parts).strip() or json.dumps(item, ensure_ascii=False, sort_keys=True)
            else:
                text = str(item).strip()
            if text:
                items.append(text[:1000])
        return items

    @staticmethod
    def _token_set(text: str) -> set[str]:
        tokens = []
        for part in _re.split(r"[^A-Za-z0-9_]+", text.lower()):
            part = part.strip()
            if len(part) >= 3:
                tokens.append(part)
        return set(tokens)

    def filter_sequence(self, sequence, horizon: int | None = None) -> dict:
        items = self._normalize_items(sequence)
        if not items:
            return {
                "ok": True,
                "domain": self.domain,
                "state_hint": self.state_hint,
                "window": self.window,
                "decay": self.decay,
                "input_count": 0,
                "selected_count": 0,
                "filtered_sequence": [],
                "summary": "",
                "dominant_terms": [],
            }
        limit = max(1, min(int(horizon or self.window), self.window))
        hint_tokens = self._token_set(f"{self.domain} {self.state_hint}")
        total = len(items)
        ranked = []
        for idx, text in enumerate(items):
            weight = self.decay ** max(0, total - idx - 1)
            text_tokens = self._token_set(text)
            overlap = len(text_tokens & hint_tokens)
            if overlap:
                weight = min(1.0, weight + min(0.25, 0.05 * overlap))
            if any(term in text.lower() for term in ("critical", "urgent", "error", "failure", "crash", "risk")):
                weight = min(1.0, weight + 0.1)
            ranked.append({"index": idx, "text": text, "weight": round(weight, 3)})
        ranked.sort(key=lambda item: (item["weight"], item["index"]), reverse=True)
        selected = ranked[:limit]
        summary = " | ".join(item["text"][:120] for item in selected[:3])
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "window": self.window,
            "decay": self.decay,
            "input_count": total,
            "selected_count": len(selected),
            "filtered_sequence": selected,
            "summary": summary[:500],
            "dominant_terms": sorted(hint_tokens)[:8],
        }


class TemporalAttention:
    """Bounded attention layer over temporal context sequences."""

    def __init__(self, domain: str = "general", state_hint: str = "", heads: int = 2, window: int = 5):
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()
        self.heads = max(1, min(int(heads or 2), 8))
        self.window = max(1, min(int(window or 5), 25))

    def attend(self, sequence, horizon: int | None = None) -> dict:
        filter_result = AdaptiveTemporalFilter(domain=self.domain, state_hint=self.state_hint, window=self.window).filter_sequence(
            sequence, horizon=horizon or self.window
        )
        attended = []
        for idx, item in enumerate(filter_result.get("filtered_sequence") or []):
            score = float(item.get("weight") or 0.0)
            score = min(1.0, score + (0.03 * min(self.heads, 4)) + (0.01 * max(0, self.window - idx - 1)))
            attended.append({
                "index": item.get("index", idx),
                "text": item.get("text", ""),
                "attention_score": round(score, 3),
                "weight": item.get("weight", 0.0),
            })
        attended.sort(key=lambda item: (item["attention_score"], item["index"]), reverse=True)
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "heads": self.heads,
            "window": self.window,
            "attention_summary": filter_result.get("summary", ""),
            "attention_terms": filter_result.get("dominant_terms", []),
            "attended_sequence": attended,
            "selected_count": len(attended),
        }


def _world_model_update_model(self, experience) -> dict:
    exp = WorldModel._parse_blob(experience)
    exp_text = WorldModel._textify(exp)
    title = (exp.get("title") or exp.get("task") or exp.get("label") or exp.get("summary") or "world_model_experience")[:180]
    temporal = AdaptiveTemporalFilter(domain=self.domain, state_hint=self.state_hint, window=5).filter_sequence([exp_text], horizon=1)
    causal = CausalGraph(domain=self.domain, state_hint=self.state_hint, query=exp_text).build(sequence=[exp_text])
    causal_inference = CausalGraphInference(
        query=exp_text,
        domain=self.domain,
        state_hint=self.state_hint,
        sequence=[exp_text],
        candidate_actions=[
            exp.get("action"),
            exp.get("next_action"),
            exp.get("transition"),
            exp.get("step"),
        ],
        horizon=3,
    ).build()
    content = json.dumps(exp, ensure_ascii=False, sort_keys=True)[:4000]
    kb_result = _kb_upsert_world_model(
        domain=f"world_model:{self.domain}",
        topic=title,
        instruction=exp_text[:1000],
        content=content,
        confidence=str(exp.get("confidence") or "medium"),
        source_type="world_model_update",
        source_ref=f"world_model:{datetime.utcnow().isoformat()}",
    )
    session_result = sb_post("sessions", {
        "summary": f"[world_model.update] {title}",
        "actions": [
            f"experience captured: {exp_text[:240]}",
            f"temporal filter: {temporal.get('summary','')[:240]}",
            f"causal graph: {causal.get('causal_summary','')[:240]}",
            f"causal inference: {causal_inference.get('summary','')[:240]}",
        ],
        "interface": "mcp",
    })
    return {
        "ok": bool(kb_result and kb_result.get("ok", True)),
        "title": title,
        "domain": self.domain,
        "kb_result": kb_result,
        "session_result": session_result,
        "temporal_filter": temporal,
        "causal_graph": causal,
        "causal_inference": causal_inference,
        "experience_preview": exp_text[:300],
    }


def _world_model_predict_future_states(self, current_state, actions, horizon: int = 3) -> dict:
    state = WorldModel._parse_blob(current_state)
    current_text = WorldModel._textify(state)
    action_list = []
    if isinstance(actions, str):
        raw = actions.strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    action_list = [str(item).strip() for item in parsed if str(item).strip()]
            except Exception:
                action_list = [part.strip() for part in raw.split(",") if part.strip()]
        else:
            action_list = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(actions, (list, tuple, set)):
        action_list = [str(item).strip() for item in actions if str(item).strip()]
    else:
        action_list = [str(actions).strip()] if str(actions).strip() else []
    try:
        steps = max(1, min(int(horizon or 3), 8))
    except Exception:
        steps = 3
    if not action_list:
        action_list = ["observe", "assess", "proceed"]

    temporal = AdaptiveTemporalFilter(domain=self.domain, state_hint=self.state_hint, window=max(3, steps + 1)).filter_sequence([current_text] + action_list, horizon=max(1, steps))
    attention = TemporalAttention(domain=self.domain, state_hint=self.state_hint, heads=3, window=max(3, steps + 1)).attend([current_text] + action_list, horizon=max(1, steps))
    causal = CausalGraph(domain=self.domain, state_hint=self.state_hint, query=current_text).build(sequence=[current_text] + action_list)
    causal_inference = CausalGraphInference(
        query=current_text,
        domain=self.domain,
        state_hint=self.state_hint,
        sequence=[current_text] + action_list,
        candidate_actions=action_list,
        horizon=steps,
    ).build()
    ranked_steps = [item["action"] for item in (causal_inference.get("ranked_transitions") or []) if item.get("action")]
    if not ranked_steps:
        action_scores = {}
        for item in (attention.get("attended_sequence") or []):
            text = (item.get("text") or "").strip()
            if text and text in action_list:
                action_scores.setdefault(text, {"text": text, "attention_score": 0.0, "causal_score": 0.0})
                action_scores[text]["attention_score"] = max(action_scores[text]["attention_score"], float(item.get("attention_score") or 0.0))
        for item in (causal.get("causal_order") or []):
            text = (item.get("text") or "").strip()
            if text and text in action_list:
                action_scores.setdefault(text, {"text": text, "attention_score": 0.0, "causal_score": 0.0})
                action_scores[text]["causal_score"] = max(action_scores[text]["causal_score"], float(item.get("belief") or 0.0))
        if not action_scores:
            for action in action_list:
                action_scores[action] = {"text": action, "attention_score": 0.0, "causal_score": 0.0}
        merged_actions = []
        for data in action_scores.values():
            data["combined_score"] = round((data["attention_score"] * 0.6) + (data["causal_score"] * 0.4), 3)
            merged_actions.append(data)
        merged_actions.sort(key=lambda item: (item["combined_score"], item["attention_score"], item["causal_score"], item["text"]), reverse=True)
        ranked_steps = [item["text"] for item in merged_actions if item.get("text")]
    if not ranked_steps:
        ranked_steps = action_list[:steps] or [current_text]

    evaluator = StateEvaluator(query=current_text, domain=self.domain, state_hint=self.state_hint)
    base_card = evaluator.evaluate()
    graph = DynamicRelationalGraph(query=current_text, domain=self.domain, state_hint=self.state_hint).build()
    meta_router = MetaContextualRouter(
        current_state=current_text,
        goal=base_card.get("recommendation") or base_card.get("summary") or "proceed",
        hwm_levels=ranked_steps,
        domain=self.domain,
        state_hint=self.state_hint,
    ).route()
    mcts = MonteCarloTreeSearch(
        query=current_text,
        domain=self.domain,
        state_hint=self.state_hint,
        candidate_actions=ranked_steps,
        rollouts=min(max(steps * len(ranked_steps), 4), 24),
    ).run()
    best_action = meta_router.get("best_level") or (causal_inference.get("best_transition") or {}).get("action") or mcts.get("best_action") or ranked_steps[0]
    predicted_states = []
    rolling_state = dict(state)
    for idx in range(steps):
        action = best_action if idx == 0 else ranked_steps[min(idx, len(ranked_steps) - 1)]
        rolling_state = {
            **rolling_state,
            "step": idx + 1,
            "last_action": action,
            "trend": "stabilize" if base_card.get("readiness_score", 0.0) >= 0.7 else "reassess",
            "expected_effect": "improve" if base_card.get("recommendation") == "proceed" else "review",
            "confidence": round(max(0.0, min(1.0, float(base_card.get("confidence") or 0.0) * (1.0 - (idx * 0.05)))), 3),
        }
        predicted_states.append({
            "step": idx + 1,
            "action": action,
            "state": rolling_state,
            "state_preview": WorldModel._textify(rolling_state)[:300],
        })
    return {
        "ok": True,
        "domain": self.domain,
        "current_state": state,
        "current_state_preview": current_text[:300],
        "horizon": steps,
        "actions": action_list,
        "temporal_filter": temporal,
        "temporal_attention": attention,
        "causal_graph": causal,
        "causal_inference": causal_inference,
        "meta_contextual_router": meta_router,
        "base_evaluation": base_card,
        "graph_density": graph.get("density"),
        "graph_dominant_table": graph.get("dominant_table"),
        "best_action": best_action,
        "predicted_states": predicted_states,
    }


WorldModel.update_model = _world_model_update_model
WorldModel.predict_future_states = _world_model_predict_future_states


class PredictiveStateRepresentation:
    """Compact predictive-state representation for bounded learning traces."""

    def __init__(self, state=None, error_signal: float = 0.0, learning_rate: float = 0.1, domain: str = "general", state_hint: str = ""):
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()
        self.learning_rate = max(0.01, min(float(learning_rate or 0.1), 1.0))
        self.state = WorldModel._parse_blob(state)
        self.error_signal = max(0.0, min(float(error_signal or 0.0), 1.0))
        self.variance = round(min(1.0, max(0.0, self.error_signal * 0.75)), 3)
        self.confidence = round(max(0.0, min(1.0, 1.0 - self.variance)), 3)

    def encode(self) -> dict:
        text = WorldModel._textify(self.state)
        token_count = len([part for part in _re.split(r"[^A-Za-z0-9_]+", text.lower()) if len(part) >= 3])
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "text": text[:500],
            "token_count": token_count,
            "error_signal": round(self.error_signal, 3),
            "variance": self.variance,
            "confidence": self.confidence,
            "feature_vector": {
                "length": len(text),
                "token_count": token_count,
                "variance": self.variance,
                "confidence": self.confidence,
            },
        }

    def update(self, error_signal: float | None = None, observation=None) -> dict:
        if error_signal is not None:
            try:
                self.error_signal = max(0.0, min(float(error_signal), 1.0))
            except Exception:
                pass
        if observation is not None:
            obs = WorldModel._parse_blob(observation)
            if obs:
                self.state.update(obs if isinstance(obs, dict) else {"observation": obs})
        self.variance = round(max(0.0, min(1.0, (self.variance * 0.7) + (self.error_signal * 0.3))), 3)
        self.confidence = round(max(0.0, min(1.0, 1.0 - self.variance)), 3)
        return self.encode()

    def predict(self, actions, horizon: int = 3) -> dict:
        action_list = []
        if isinstance(actions, str):
            action_list = [part.strip() for part in actions.replace("\n", ",").split(",") if part.strip()]
        elif isinstance(actions, (list, tuple, set)):
            action_list = [str(item).strip() for item in actions if str(item).strip()]
        elif actions not in (None, ""):
            action_list = [str(actions).strip()]
        if not action_list:
            action_list = ["observe", "assess", "proceed"]
        steps = max(1, min(int(horizon or 3), 8))
        state_text = WorldModel._textify(self.state).lower()
        scores = []
        for action in action_list:
            action_lower = action.lower()
            score = 0.25
            if any(term in action_lower for term in ("observe", "inspect", "assess", "review")):
                score += 0.15
            if any(term in action_lower for term in ("fix", "update", "improve", "apply", "verify", "train")):
                score += 0.1
            overlap = len(set(action_lower.split()) & set(state_text.split()))
            if overlap:
                score += min(0.25, 0.05 * overlap)
            score += max(0.0, 0.2 - self.variance)
            scores.append({"action": action, "score": round(min(1.0, score), 3)})
        scores.sort(key=lambda item: (item["score"], item["action"]), reverse=True)
        ranked = [item["action"] for item in scores[:steps]]
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "ranked_actions": scores,
            "best_action": ranked[0] if ranked else None,
            "summary": " | ".join(ranked[:3])[:500],
        }


class DynamicReplayBuffer:
    """Bounded replay buffer with priority and context weighting."""

    def __init__(self, capacity: int = 128, domain: str = "general", state_hint: str = ""):
        self.capacity = max(8, min(int(capacity or 128), 1024))
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()
        self.buffer = []

    @staticmethod
    def _token_set(text: str) -> set[str]:
        return {part for part in _re.split(r"[^A-Za-z0-9_]+", (text or "").lower()) if len(part) >= 3}

    def add(self, experience, priority: float = 0.5, context: str = "") -> dict:
        item = WorldModel._parse_blob(experience)
        text = WorldModel._textify(item)
        ctx_tokens = self._token_set(context or self.state_hint)
        exp_tokens = self._token_set(text)
        overlap = len(ctx_tokens & exp_tokens)
        try:
            base_priority = max(0.0, min(float(priority or 0.5), 1.0))
        except Exception:
            base_priority = 0.5
        contextual_weight = min(1.0, 0.35 + 0.1 * overlap)
        score = round(min(1.0, base_priority * 0.55 + contextual_weight * 0.35 + 0.1), 3)
        packet = {
            "experience": item,
            "text": text[:500],
            "priority": base_priority,
            "contextual_weight": round(contextual_weight, 3),
            "score": score,
            "domain": self.domain,
            "state_hint": self.state_hint,
        }
        self.buffer.append(packet)
        self.buffer = self.buffer[-self.capacity:]
        return packet

    def sample(self, limit: int = 5, context: str = "") -> dict:
        lim = max(1, min(int(limit or 5), self.capacity))
        ctx_tokens = self._token_set(context or self.state_hint)
        ranked = []
        for idx, item in enumerate(self.buffer):
            text_tokens = self._token_set(item.get("text") or "")
            overlap = len(ctx_tokens & text_tokens)
            score = float(item.get("score") or 0.0) + min(0.2, 0.05 * overlap) + max(0.0, 0.05 * (idx / max(1, len(self.buffer))))
            ranked.append({**item, "sample_score": round(min(1.0, score), 3)})
        ranked.sort(key=lambda entry: (entry["sample_score"], entry.get("priority", 0.0)), reverse=True)
        selected = ranked[:lim]
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "capacity": self.capacity,
            "buffer_count": len(self.buffer),
            "selected_count": len(selected),
            "selected": selected,
            "summary": " | ".join(item.get("text", "")[:80] for item in selected[:3])[:500],
        }


class SimulatedCritic:
    """Score a sequence of states/actions with bounded reward and risk heuristics."""

    def __init__(self, domain: str = "general", state_hint: str = ""):
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()

    @staticmethod
    def _normalize(sequence) -> list[str]:
        if sequence in (None, "", []):
            return []
        if isinstance(sequence, str):
            return [part.strip() for part in sequence.replace("\n", ",").split(",") if part.strip()]
        if isinstance(sequence, dict):
            sequence = [sequence]
        if isinstance(sequence, (list, tuple, set)):
            items = []
            for item in sequence:
                if isinstance(item, dict):
                    text = WorldModel._textify(item)
                else:
                    text = str(item).strip()
                if text:
                    items.append(text[:1000])
            return items
        text = str(sequence).strip()
        return [text] if text else []

    def score(self, sequence, reward_signal: str = "", side_effects=None) -> dict:
        items = self._normalize(sequence)
        side_effects = self._normalize(side_effects)
        reward_tokens = {part for part in _re.split(r"[^A-Za-z0-9_]+", (reward_signal or "").lower()) if len(part) >= 3}
        positive_terms = {"improve", "verify", "stabilize", "resolve", "complete", "success", "safe", "correct"}
        negative_terms = {"fail", "error", "risk", "break", "stale", "duplicate", "conflict", "uncertain"}
        score = 0.5
        notes = []
        for item in items:
            lower = item.lower()
            if any(term in lower for term in positive_terms):
                score += 0.08
                notes.append(f"positive:{item[:80]}")
            if any(term in lower for term in negative_terms):
                score -= 0.1
                notes.append(f"negative:{item[:80]}")
            if any(tok in lower for tok in reward_tokens):
                score += 0.05
        for item in side_effects:
            lower = item.lower()
            if any(term in lower for term in negative_terms):
                score -= 0.05
                notes.append(f"side_effect:{item[:80]}")
        score = max(0.0, min(1.0, score))
        risk = round(max(0.0, min(1.0, 1.0 - score + (0.05 * len([n for n in notes if n.startswith('negative')])))), 3)
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "sequence": items,
            "reward_signal": reward_signal[:160],
            "side_effects": side_effects,
            "score": round(score, 3),
            "risk": risk,
            "notes": notes[:8],
            "summary": f"score={round(score, 3)} | risk={risk}",
        }


class MetaLearner:
    """Bounded meta-learner over a predictive state representation."""

    def __init__(self, model=None, domain: str = "general", state_hint: str = "", learning_rate: float = 0.1):
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()
        self.learning_rate = max(0.01, min(float(learning_rate or 0.1), 1.0))
        self.model = model if isinstance(model, PredictiveStateRepresentation) else PredictiveStateRepresentation(domain=self.domain, state_hint=self.state_hint)

    def adapt(self, error_signal: float = 0.0, observation=None, target: str = "") -> dict:
        psr = self.model.update(error_signal=error_signal, observation=observation)
        target_text = (target or "").strip()
        if target_text:
            overlap = len(set(target_text.lower().split()) & set((psr.get("text") or "").lower().split()))
            psr["target_alignment"] = round(min(1.0, 0.2 + 0.08 * overlap), 3)
        adjustment = round(max(0.01, min(1.0, self.learning_rate * (1.0 - float(psr.get("error_signal") or 0.0)))), 3)
        psr["learning_rate"] = adjustment
        psr["meta_objective"] = "minimize_prediction_error_variance"
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "error_signal": round(max(0.0, min(float(error_signal or 0.0), 1.0)), 3),
            "learning_rate": adjustment,
            "model_packet": psr,
            "summary": f"error={round(max(0.0, min(float(error_signal or 0.0), 1.0)), 3)} | lr={adjustment}",
        }


class DynamicGatingLayer:
    """Choose weights over submodules from current state/task context."""

    def __init__(self, domain: str = "general", state_hint: str = ""):
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()

    @staticmethod
    def _normalize_modules(modules) -> list[str]:
        if modules in (None, "", []):
            return []
        if isinstance(modules, str):
            return [part.strip() for part in modules.replace("\n", ",").split(",") if part.strip()]
        if isinstance(modules, dict):
            modules = list(modules.keys())
        if isinstance(modules, (list, tuple, set)):
            out = []
            for module in modules:
                text = str(module).strip()
                if text:
                    out.append(text)
            return out
        text = str(modules).strip()
        return [text] if text else []

    @staticmethod
    def _token_set(text: str) -> set[str]:
        return {part for part in _re.split(r"[^A-Za-z0-9_]+", (text or "").lower()) if len(part) >= 3}

    def gate(self, state, task_context="", modules=None) -> dict:
        state_text = WorldModel._textify(WorldModel._parse_blob(state))
        task_text = WorldModel._textify(WorldModel._parse_blob(task_context)) if isinstance(task_context, (dict, str)) else str(task_context or "")
        module_list = self._normalize_modules(modules) or ["reasoning", "search", "verification"]
        state_tokens = self._token_set(state_text + " " + self.state_hint)
        task_tokens = self._token_set(task_text)
        weights = []
        for idx, module in enumerate(module_list):
            module_tokens = self._token_set(module)
            overlap = len((state_tokens | task_tokens) & module_tokens)
            score = 0.15 + (0.1 * overlap)
            if any(term in module.lower() for term in ("verify", "critic", "audit")):
                score += 0.08 if any(term in task_text.lower() for term in ("review", "verify", "audit", "check")) else 0.02
            if any(term in module.lower() for term in ("learn", "meta", "update", "train")):
                score += 0.05 if any(term in task_text.lower() for term in ("learn", "improve", "adapt", "update")) else 0.0
            if any(term in module.lower() for term in ("search", "retrieve", "query")):
                score += 0.05 if any(term in task_text.lower() for term in ("find", "search", "look", "inspect")) else 0.0
            score = max(0.01, min(1.0, score))
            weights.append({"module": module, "weight": round(score, 3), "index": idx, "overlap": overlap})
        weights.sort(key=lambda item: (item["weight"], -item["index"], item["module"]), reverse=True)
        total = sum(max(item["weight"], 0.001) for item in weights) or 1.0
        for item in weights:
            item["probability"] = round(max(item["weight"], 0.001) / total, 4)
        best = weights[0]["module"] if weights else None
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "modules": module_list,
            "weights": weights,
            "best_module": best,
            "summary": f"best={best or ''} | modules={len(module_list)}",
        }


class GatingNetwork:
    """Meta-trainable wrapper around the dynamic gating layer."""

    def __init__(self, modules=None, domain: str = "general", state_hint: str = "", temperature: float = 1.0):
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()
        self.modules = DynamicGatingLayer._normalize_modules(modules) if modules is not None else []
        self.temperature = max(0.2, min(float(temperature or 1.0), 3.0))
        self.bias = {module: 0.0 for module in self.modules}

    def meta_train(self, samples=None, state="", task_context="") -> dict:
        samples = samples if isinstance(samples, (list, tuple)) else [samples] if samples else []
        layer = DynamicGatingLayer(domain=self.domain, state_hint=self.state_hint)
        gate = layer.gate(state=state, task_context=task_context, modules=self.modules)
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            module = str(sample.get("module") or sample.get("best_module") or "").strip()
            outcome = str(sample.get("outcome") or sample.get("result") or "").lower()
            if module and module in self.bias:
                delta = 0.08 if any(term in outcome for term in ("success", "good", "win", "pass")) else -0.05 if any(term in outcome for term in ("fail", "error", "bad", "reject")) else 0.0
                self.bias[module] = round(max(-0.5, min(0.5, self.bias.get(module, 0.0) + delta)), 3)
        for item in gate.get("weights") or []:
            module = item.get("module")
            if module in self.bias:
                item["weight"] = round(max(0.01, min(1.0, item.get("weight", 0.0) + self.bias[module])), 3)
        gate["weights"].sort(key=lambda item: (item["weight"], item["module"]), reverse=True)
        best = gate["weights"][0]["module"] if gate.get("weights") else None
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "temperature": self.temperature,
            "bias": dict(sorted(self.bias.items())),
            "gate": gate,
            "best_module": best,
            "summary": f"best={best or ''} | modules={len(self.modules)}",
        }


class CausalMappingModule:
    """Map causal graph structures and context embeddings onto a goal translation."""

    def __init__(self, domain: str = "general", state_hint: str = ""):
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()

    @staticmethod
    def _token_set(text: str) -> set[str]:
        return {part for part in _re.split(r"[^A-Za-z0-9_]+", (text or "").lower()) if len(part) >= 3}

    def map(self, causal_graph=None, context_embedding=None, goal: str = "") -> dict:
        graph = causal_graph if isinstance(causal_graph, dict) else {}
        context_text = WorldModel._textify(WorldModel._parse_blob(context_embedding))
        goal_text = (goal or "").strip()
        goal_tokens = self._token_set(goal_text)
        context_tokens = self._token_set(context_text + " " + self.state_hint)
        raw_nodes = graph.get("nodes") or []
        nodes = []
        for node in raw_nodes:
            if isinstance(node, dict):
                nodes.append(node)
            else:
                text = str(node).strip()
                if text:
                    nodes.append({"id": text, "label": text, "type": "node"})
        ranked = []
        for node in nodes:
            label = str(node.get("label") or node.get("id") or "").strip()
            node_tokens = self._token_set(label)
            overlap = len((goal_tokens | context_tokens) & node_tokens)
            score = 0.15 + (0.1 * overlap) + (0.05 if node.get("type") in {"module", "chunk", "memory"} else 0.0)
            ranked.append({
                "node_id": node.get("id"),
                "label": label,
                "score": round(min(1.0, score), 3),
            })
        ranked.sort(key=lambda item: (item["score"], item["label"]), reverse=True)
        best = ranked[0] if ranked else {}
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "goal": goal_text[:240],
            "context_preview": context_text[:240],
            "source_graph_density": graph.get("density"),
            "source_graph_nodes": len(nodes),
            "ranked_mappings": ranked[:8],
            "best_mapping": best,
            "summary": f"goal={goal_text[:80]} | best={best.get('label') or best.get('node_id') or ''}",
        }


class PrincipleSearchModule:
    """Rank guiding principles against a current state/task context."""

    def __init__(self, domain: str = "general", state_hint: str = ""):
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()

    @staticmethod
    def _token_set(text: str) -> set[str]:
        return {part for part in _re.split(r"[^A-Za-z0-9_]+", (text or "").lower()) if len(part) >= 3}

    def search(self, principles=None, state: str = "", goal: str = "", task_context: str = "") -> dict:
        if isinstance(principles, str):
            principle_list = [part.strip() for part in principles.replace("\n", ",").split(",") if part.strip()]
        elif isinstance(principles, (list, tuple, set)):
            principle_list = [str(item).strip() for item in principles if str(item).strip()]
        elif principles:
            principle_list = [str(principles).strip()]
        else:
            principle_list = []
        if not principle_list:
            principle_list = [
                "stability_first",
                "evidence_before_action",
                "verify_before_close",
                "prefer_small_safe_changes",
            ]
        state_text = WorldModel._textify(WorldModel._parse_blob(state))
        goal_text = WorldModel._textify(WorldModel._parse_blob(goal)) if isinstance(goal, (dict, str)) else str(goal or "")
        context_text = WorldModel._textify(WorldModel._parse_blob(task_context)) if isinstance(task_context, (dict, str)) else str(task_context or "")
        combined_tokens = self._token_set(" ".join([state_text, goal_text, context_text, self.state_hint]))
        ranked = []
        for idx, principle in enumerate(principle_list):
            principle_tokens = self._token_set(principle)
            overlap = len(combined_tokens & principle_tokens)
            score = 0.2 + (0.12 * overlap)
            if any(term in principle.lower() for term in ("verify", "evidence", "safe", "stability")):
                score += 0.1
            if any(term in principle.lower() for term in ("search", "discover", "explore")):
                score += 0.04
            ranked.append({"principle": principle, "score": round(min(1.0, score), 3), "index": idx, "overlap": overlap})
        ranked.sort(key=lambda item: (item["score"], item["principle"]), reverse=True)
        best = ranked[0] if ranked else {}
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "principles": principle_list,
            "ranked_principles": ranked,
            "best_principle": best.get("principle"),
            "summary": f"best={best.get('principle') or ''} | principles={len(principle_list)}",
        }


class StateReconciliationBuffer:
    """Collect and reconcile competing state snapshots before downstream planning."""

    def __init__(self, capacity: int = 64, domain: str = "general", state_hint: str = ""):
        self.capacity = max(8, min(int(capacity or 64), 512))
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()
        self.replay = DynamicReplayBuffer(capacity=self.capacity, domain=self.domain, state_hint=self.state_hint)

    def add(self, state, priority: float = 0.5, context: str = "") -> dict:
        return self.replay.add({"state": state}, priority=priority, context=context)

    def reconcile(self, states=None, context: str = "") -> dict:
        states = states if isinstance(states, (list, tuple, set)) else [states] if states else []
        snapshots = [WorldModel._textify(WorldModel._parse_blob(state)) for state in states if state not in (None, "", {})]
        if not snapshots:
            return {
                "ok": False,
                "error": "states required",
                "domain": self.domain,
                "state_hint": self.state_hint,
            }
        for idx, snap in enumerate(snapshots):
            self.add({"snapshot": snap, "index": idx}, priority=max(0.3, 0.8 - (0.05 * idx)), context=context or self.state_hint)
        sampled = self.replay.sample(limit=min(5, len(snapshots)), context=context or self.state_hint)
        token_sets = [PrincipleSearchModule._token_set(s) for s in snapshots]
        union = set().union(*token_sets) if token_sets else set()
        intersections = set(token_sets[0]) if token_sets else set()
        for token_set in token_sets[1:]:
            intersections &= token_set
        conflicts = sorted(union - intersections)[:24]
        normalized_state = " | ".join(snapshots[:3])[:500]
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "input_count": len(snapshots),
            "normalized_state": normalized_state,
            "shared_tokens": sorted(intersections)[:24],
            "conflicting_tokens": conflicts,
            "replay_summary": sampled,
            "summary": f"snapshots={len(snapshots)} | conflicts={len(conflicts)}",
        }


class HierarchicalSearchTree:
    """Build a bounded search tree over hierarchical levels and candidate actions."""

    def __init__(self, domain: str = "general", state_hint: str = ""):
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()

    @staticmethod
    def _normalize(items) -> list[str]:
        if items in (None, "", []):
            return []
        if isinstance(items, str):
            return [part.strip() for part in items.replace("\n", ",").split(",") if part.strip()]
        if isinstance(items, dict):
            items = list(items.keys())
        if isinstance(items, (list, tuple, set)):
            return [str(item).strip() for item in items if str(item).strip()]
        text = str(items).strip()
        return [text] if text else []

    def build(self, current_state: str = "", goal: str = "", hwm_levels=None, candidate_actions=None, horizon: int = 3, rollouts: int = 12, exploration_weight: float = 1.2) -> dict:
        levels = self._normalize(hwm_levels) or ["low", "medium", "high"]
        actions = self._normalize(candidate_actions) or levels[:]
        goal_tokens = PrincipleSearchModule._token_set(goal)
        state_tokens = PrincipleSearchModule._token_set(current_state)
        combined_tokens = goal_tokens | state_tokens
        principle_packet = PrincipleSearchModule(domain=self.domain, state_hint=self.state_hint).search(
            state=current_state,
            goal=goal,
            task_context=" ".join([self.state_hint, goal]).strip(),
        )
        reconciliation = None
        if current_state or goal:
            reconciliation = StateReconciliationBuffer(domain=self.domain, state_hint=self.state_hint).reconcile(
                states=[current_state, goal],
                context=f"{self.state_hint} {goal}".strip(),
            )
        action_scores = []
        for idx, action in enumerate(actions):
            action_tokens = PrincipleSearchModule._token_set(action)
            overlap = len(combined_tokens & action_tokens)
            score = 0.2 + (0.15 * overlap)
            if any(term in action.lower() for term in ("inspect", "verify", "evidence", "review")):
                score += 0.08
            if any(term in action.lower() for term in ("implement", "apply", "fix", "close")):
                score += 0.06
            action_scores.append({"action": action, "score": round(min(1.0, score), 3), "index": idx, "overlap": overlap})
        action_scores.sort(key=lambda item: (item["score"], item["action"]), reverse=True)
        best_action = action_scores[0]["action"] if action_scores else (actions[0] if actions else None)
        tree = {
            "root": {
                "state": WorldModel._textify(WorldModel._parse_blob(current_state))[:180],
                "goal": WorldModel._textify(WorldModel._parse_blob(goal))[:180],
            },
            "levels": levels,
            "actions": actions,
            "best_level": levels[min(1, len(levels) - 1)] if levels else None,
            "best_action": best_action,
            "level_distribution": [
                {
                    "index": idx,
                    "level": level,
                    "score": round(max(0.1, 0.9 - (0.2 * idx)), 3),
                    "probability": round(max(0.1, 0.9 - (0.2 * idx)), 3),
                }
                for idx, level in enumerate(levels)
            ],
            "plan": {
                "best_level": levels[min(1, len(levels) - 1)] if levels else None,
                "best_action": best_action,
                "principle_packet": principle_packet,
                "reconciliation": reconciliation,
                "action_scores": action_scores,
            },
            "search_controller": {
                "mode": "lightweight_tree",
                "principle_best": principle_packet.get("best_principle") if isinstance(principle_packet, dict) else None,
            },
        }
        tree["node_count"] = 1 + len(levels) + len(actions)
        tree["summary"] = f"best_level={tree['best_level'] or ''} | best_action={tree['best_action'] or ''}"
        return {"ok": True, "domain": self.domain, "state_hint": self.state_hint, "tree": tree, "summary": tree["summary"]}


class HierarchicalSearchController:
    """Coordinate world-model rollouts with bounded multi-level search."""

    def __init__(
        self,
        current_state,
        goal,
        hwm_levels,
        domain: str = "general",
        state_hint: str = "",
        horizon: int = 3,
        candidate_actions=None,
        rollouts: int = 12,
        exploration_weight: float = 1.2,
    ):
        self.current_state = current_state
        self.goal = goal
        self.hwm_levels = hwm_levels
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()
        self.horizon = max(1, min(int(horizon or 3), 8))
        self.candidate_actions = candidate_actions
        self.rollouts = max(1, min(int(rollouts or 12), 32))
        try:
            self.exploration_weight = max(0.1, min(float(exploration_weight or 1.2), 4.0))
        except Exception:
            self.exploration_weight = 1.2

    @staticmethod
    def _normalize_items(items) -> list[str]:
        if items in (None, "", []):
            return []
        if isinstance(items, str):
            raw = items.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
                except Exception:
                    try:
                        parsed = ast.literal_eval(raw)
                        if isinstance(parsed, (list, tuple, set)):
                            return [str(item).strip() for item in parsed if str(item).strip()]
                    except Exception:
                        pass
            return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
        if isinstance(items, dict):
            items = [items]
        if isinstance(items, (list, tuple, set)):
            out = []
            for item in items:
                if isinstance(item, dict):
                    for key in ("level", "name", "label", "title", "value", "text", "action"):
                        value = item.get(key)
                        if value:
                            out.append(str(value).strip())
                            break
                    else:
                        out.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
                else:
                    text = str(item).strip()
                    if text:
                        out.append(text)
            return out
        text = str(items).strip()
        return [text] if text else []

    def run(self) -> dict:
        current_state_obj = WorldModel._parse_blob(self.current_state)
        current_state_text = WorldModel._textify(current_state_obj)
        goal_text = WorldModel._textify(WorldModel._parse_blob(self.goal)) if isinstance(self.goal, (dict, str)) else str(self.goal or "")
        levels = self._normalize_items(self.hwm_levels) or ["low", "medium", "high"]
        actions = self._normalize_items(self.candidate_actions)
        if not actions:
            actions = levels[:]
        wm = WorldModel(domain=self.domain, state_hint=self.state_hint).predict_future_states(
            current_state=current_state_obj,
            actions=actions,
            horizon=self.horizon,
        )
        packet = t_reasoning_packet(query=f"{current_state_text} | {goal_text}".strip(" |"), domain=self.domain, limit=str(min(16, max(8, len(actions) + 4))))
        evaluator = StateEvaluator(query=f"{current_state_text} | {goal_text}".strip(" |"), domain=self.domain, state_hint=self.state_hint)
        state_eval = evaluator.evaluate()
        mcts = MonteCarloTreeSearch(
            query=f"{current_state_text} | {goal_text}".strip(" |"),
            domain=self.domain,
            state_hint=self.state_hint,
            candidate_actions=actions,
            rollouts=self.rollouts,
            exploration_weight=self.exploration_weight,
        ).run()

        wm_best = wm.get("best_action") or ""
        mcts_best = mcts.get("best_action") or ""
        eval_readiness = float(state_eval.get("readiness_score") or 0.0)
        eval_risk = float(state_eval.get("risk_score") or 0.0)
        level_distribution = []
        for idx, level in enumerate(levels):
            lower = level.lower()
            score = 0.1 + (0.2 * (len(levels) - idx) / max(1, len(levels)))
            if lower in goal_text.lower():
                score += 0.25
            if lower in wm_best.lower() or lower in mcts_best.lower():
                score += 0.25
            if any(term in lower for term in ("low", "narrow", "specific")) and eval_readiness < 0.6:
                score += 0.08
            if any(term in lower for term in ("high", "broad", "abstract")) and eval_readiness >= 0.6:
                score += 0.08
            if any(term in goal_text.lower() for term in ("safe", "stabil", "review")) and any(term in lower for term in ("safe", "stable", "review")):
                score += 0.12
            score = max(0.0, min(1.0, score))
            level_distribution.append({
                "index": idx,
                "level": level,
                "score": round(score, 3),
                "readiness": round(eval_readiness, 3),
                "risk": round(eval_risk, 3),
                "matches_world_model": bool(wm_best and lower in wm_best.lower()),
                "matches_mcts": bool(mcts_best and lower in mcts_best.lower()),
            })
        total = sum(max(item["score"], 0.001) for item in level_distribution) or 1.0
        for item in level_distribution:
            item["probability"] = round(max(item["score"], 0.001) / total, 4)
        level_distribution.sort(key=lambda item: (item["score"], item["probability"], item["level"]), reverse=True)
        best_level = level_distribution[0]["level"] if level_distribution else None
        plan = {
            "best_level": best_level,
            "best_action": wm_best or mcts_best or (actions[0] if actions else None),
            "world_model_best_action": wm_best,
            "mcts_best_action": mcts_best,
            "state_recommendation": state_eval.get("recommendation"),
        }
        summary = " | ".join([
            f"goal={goal_text[:120]}",
            f"best_level={best_level or ''}",
            f"best_action={plan['best_action'] or ''}",
        ]).strip(" |")
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "current_state": current_state_text[:1000],
            "goal": goal_text[:1000],
            "hwm_levels": levels,
            "level_distribution": level_distribution,
            "best_level": best_level,
            "plan": plan,
            "summary": summary[:500],
            "world_model": wm,
            "state_evaluation": state_eval,
            "reasoning_packet": packet,
            "mcts": mcts,
        }


def t_hierarchical_search_controller(current_state: str = "", goal: str = "", hwm_levels: str = "", domain: str = "general", state_hint: str = "", horizon: str = "3", candidate_actions: str = "", rollouts: str = "12", exploration_weight: str = "1.2") -> dict:
    """Manage multi-level MCTS with world-model prediction and state evaluation."""
    try:
        if not current_state and not goal:
            return {"ok": False, "error": "current_state or goal required"}
        try:
            hz = max(1, min(int(horizon), 8))
        except Exception:
            hz = 3
        try:
            ro = max(1, min(int(rollouts), 32))
        except Exception:
            ro = 12
        try:
            ew = max(0.1, min(float(exploration_weight), 4.0))
        except Exception:
            ew = 1.2
        return HierarchicalSearchController(
            current_state=current_state,
            goal=goal,
            hwm_levels=hwm_levels,
            domain=domain,
            state_hint=state_hint,
            horizon=hz,
            candidate_actions=candidate_actions,
            rollouts=ro,
            exploration_weight=ew,
        ).run()
    except Exception as e:
        return {"ok": False, "error": str(e)}
class TemporalHierarchicalWorldModel:
    """Wrap hierarchical state reasoning with simple temporal sequence features."""

    def __init__(self, domain: str = "general", state_hint: str = "", temporal_window: int = 5, decay: float = 0.82):
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()
        self.temporal_window = max(1, min(int(temporal_window or 5), 25))
        self.decay = max(0.1, min(float(decay or 0.82), 0.99))
        self.world_model = WorldModel(domain=self.domain, state_hint=self.state_hint)

    @staticmethod
    def _normalize_sequence(sequence) -> list[str]:
        if sequence in (None, "", []):
            return []
        if isinstance(sequence, str):
            raw = sequence.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
                except Exception:
                    try:
                        parsed = ast.literal_eval(raw)
                        if isinstance(parsed, (list, tuple, set)):
                            return [str(item).strip() for item in parsed if str(item).strip()]
                    except Exception:
                        pass
            return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
        if isinstance(sequence, dict):
            sequence = [sequence]
        if isinstance(sequence, (list, tuple, set)):
            items = []
            for item in sequence:
                if isinstance(item, dict):
                    parts = []
                    for key in ("state", "current_state", "context", "summary", "value", "action", "title", "description"):
                        value = item.get(key)
                        if value:
                            parts.append(str(value))
                    text = " | ".join(parts).strip() or json.dumps(item, ensure_ascii=False, sort_keys=True)
                else:
                    text = str(item).strip()
                if text:
                    items.append(text[:1000])
            return items
        text = str(sequence).strip()
        return [text] if text else []

    def assess_sequence(self, sequence, horizon: int | None = None) -> dict:
        items = self._normalize_sequence(sequence)
        if not items:
            return {"ok": False, "error": "sequence required"}
        limit = max(1, min(int(horizon or self.temporal_window), self.temporal_window))
        temporal = AdaptiveTemporalFilter(domain=self.domain, state_hint=self.state_hint, window=self.temporal_window, decay=self.decay).filter_sequence(items, horizon=limit)
        attention = TemporalAttention(domain=self.domain, state_hint=self.state_hint, heads=3, window=self.temporal_window).attend(items, horizon=limit)
        current_state = items[-1] if items else ""
        actions = items[-min(len(items), limit):]
        world = self.world_model.predict_future_states(current_state=current_state, actions=actions, horizon=min(limit, 8))
        controller = HierarchicalSearchController(
            current_state=current_state,
            goal=temporal.get("summary") or attention.get("attention_summary") or "stabilize",
            hwm_levels=actions or ["observe", "assess", "proceed"],
            domain=self.domain,
            state_hint=self.state_hint,
            horizon=min(limit, 8),
            candidate_actions=actions,
            rollouts=min(max(8, len(actions) * 3), 32),
            exploration_weight=1.2,
        ).run()
        history = []
        for idx, text in enumerate(items[-self.temporal_window:]):
            history.append({
                "index": idx,
                "text": text,
                "decay_weight": round(self.decay ** max(0, len(items) - idx - 1), 3),
            })
        summary = " | ".join([
            f"sequence={items[-1][:80] if items else ''}",
            f"best_level={controller.get('best_level') or ''}",
            f"best_action={controller.get('plan', {}).get('best_action') or ''}",
        ]).strip(" |")
        return {
            "ok": True,
            "domain": self.domain,
            "state_hint": self.state_hint,
            "temporal_window": self.temporal_window,
            "decay": self.decay,
            "sequence": items,
            "temporal_filter": temporal,
            "temporal_attention": attention,
            "world_model": world,
            "hierarchical_search": controller,
            "history": history,
            "summary": summary[:500],
        }


def t_temporal_hierarchical_world_model(sequence: str = "", current_state: str = "", actions: str = "", domain: str = "general", state_hint: str = "", temporal_window: str = "5", decay: str = "0.82", horizon: str = "3") -> dict:
    """Temporal wrapper around hierarchical world-model reasoning."""
    try:
        try:
            tw = max(1, min(int(temporal_window or 5), 25))
        except Exception:
            tw = 5
        try:
            hz = max(1, min(int(horizon or 3), 8))
        except Exception:
            hz = 3
        if not sequence:
            seq = []
            if current_state:
                seq.append(current_state)
            if actions:
                seq.extend([part.strip() for part in actions.replace("\n", ",").split(",") if part.strip()])
            sequence = seq
        return TemporalHierarchicalWorldModel(
            domain=domain,
            state_hint=state_hint,
            temporal_window=tw,
            decay=decay,
        ).assess_sequence(sequence, horizon=hz)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_world_model(domain: str = "general", state_hint: str = "", experience: str = "", current_state: str = "", actions: str = "", horizon: str = "3") -> dict:
    """Update the world model from experience or predict future states from current state."""
    try:
        if experience:
            return WorldModel(domain=domain, state_hint=state_hint).update_model(experience)
        if not current_state:
            return {"ok": False, "error": "current_state or experience required"}
        try:
            hz = max(1, min(int(horizon), 8))
        except Exception:
            hz = 3
        return WorldModel(domain=domain, state_hint=state_hint).predict_future_states(current_state=current_state, actions=actions, horizon=hz)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_world_model_interface(domain: str = "general", state_hint: str = "", current_state: str = "", actions: str = "", horizon: str = "3") -> dict:
    """Return the canonical world-model interface contract and optional uncertainty summary."""
    try:
        model = WorldModel(domain=domain, state_hint=state_hint)
        contract = {
            "ok": True,
            "domain": model.domain,
            "state_hint": model.state_hint,
            "interface": "WorldModelInterface",
            "methods": ["update_model", "predict_future_states", "predict_with_uncertainty"],
            "contract": {
                "update_model": "capture experience into KB and session traces",
                "predict_future_states": "roll forward bounded actions from current state",
                "predict_with_uncertainty": "return mean/variance/distribution over future steps",
            },
        }
        if current_state:
            contract["prediction"] = model.predict_with_uncertainty(current_state=current_state, actions=actions, horizon=max(1, min(int(horizon or 3), 8)))
        return contract
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_predict_with_uncertainty(domain: str = "general", state_hint: str = "", current_state: str = "", actions: str = "", horizon: str = "3") -> dict:
    """Predict future states with mean/variance/uncertainty surfaced explicitly."""
    try:
        if not current_state and not actions:
            return {"ok": False, "error": "current_state or actions required"}
        hz = max(1, min(int(horizon or 3), 8))
    except Exception:
        hz = 3
    try:
        return WorldModel(domain=domain, state_hint=state_hint).predict_with_uncertainty(current_state=current_state, actions=actions, horizon=hz)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_predictive_state_representation(state: str = "", error_signal: str = "0", learning_rate: str = "0.1", domain: str = "general", state_hint: str = "", actions: str = "", horizon: str = "3") -> dict:
    """Build a predictive-state representation packet and optionally simulate action ranking."""
    try:
        err = max(0.0, min(float(error_signal or 0.0), 1.0))
    except Exception:
        err = 0.0
    try:
        lr = max(0.01, min(float(learning_rate or 0.1), 1.0))
    except Exception:
        lr = 0.1
    psr = PredictiveStateRepresentation(state=state, error_signal=err, learning_rate=lr, domain=domain, state_hint=state_hint)
    packet = psr.encode()
    try:
        hz = max(1, min(int(horizon or 3), 8))
    except Exception:
        hz = 3
    if actions:
        packet["prediction"] = psr.predict(actions=actions, horizon=hz)
    return packet


def t_dynamic_replay_buffer(experiences: str = "", context: str = "", limit: str = "5", capacity: str = "128", priority_floor: str = "0.3", domain: str = "general", state_hint: str = "") -> dict:
    """Rank experiences with a bounded replay buffer heuristic."""
    try:
        lim = max(1, min(int(limit or 5), 32))
    except Exception:
        lim = 5
    try:
        cap = max(8, min(int(capacity or 128), 1024))
    except Exception:
        cap = 128
    try:
        floor = max(0.0, min(float(priority_floor or 0.3), 1.0))
    except Exception:
        floor = 0.3
    buf = DynamicReplayBuffer(capacity=cap, domain=domain, state_hint=state_hint)
    items = []
    if experiences:
        raw = experiences.strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    items = parsed
            except Exception:
                items = [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
        else:
            items = [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
    for idx, exp in enumerate(items):
        text = str(exp).strip()
        if not text:
            continue
        buf.add({"experience": text, "index": idx}, priority=max(floor, 0.35 + 0.05 * idx), context=context or state_hint)
    return buf.sample(limit=lim, context=context or state_hint)


def t_simulated_critic(sequence: str = "", reward_signal: str = "", side_effects: str = "", domain: str = "general", state_hint: str = "") -> dict:
    """Score a sequence with a bounded simulated critic."""
    try:
        return SimulatedCritic(domain=domain, state_hint=state_hint).score(sequence=sequence, reward_signal=reward_signal, side_effects=side_effects)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_meta_learner(state: str = "", error_signal: str = "0", observation: str = "", target: str = "", learning_rate: str = "0.1", domain: str = "general", state_hint: str = "") -> dict:
    """Adapt a predictive-state representation from an error signal."""
    try:
        err = max(0.0, min(float(error_signal or 0.0), 1.0))
    except Exception:
        err = 0.0
    try:
        lr = max(0.01, min(float(learning_rate or 0.1), 1.0))
    except Exception:
        lr = 0.1
    model = PredictiveStateRepresentation(state=state, error_signal=err, learning_rate=lr, domain=domain, state_hint=state_hint)
    return MetaLearner(model=model, domain=domain, state_hint=state_hint, learning_rate=lr).adapt(error_signal=err, observation=observation, target=target)


def t_dynamic_gating_layer(state: str = "", task_context: str = "", modules: str = "", domain: str = "general", state_hint: str = "") -> dict:
    """Gate submodules from a current state and task context."""
    try:
        return DynamicGatingLayer(domain=domain, state_hint=state_hint).gate(state=state, task_context=task_context, modules=modules)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_gating_network(state: str = "", task_context: str = "", modules: str = "", samples: str = "", temperature: str = "1.0", domain: str = "general", state_hint: str = "") -> dict:
    """Meta-train a gating network over submodules from bounded samples."""
    try:
        temp = max(0.2, min(float(temperature or 1.0), 3.0))
    except Exception:
        temp = 1.0
    module_list = [part.strip() for part in modules.replace("\n", ",").split(",") if part.strip()] if modules else []
    gate = GatingNetwork(modules=module_list, domain=domain, state_hint=state_hint, temperature=temp)
    sample_list = []
    if samples:
        raw = samples.strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    sample_list = parsed
            except Exception:
                pass
        if not sample_list:
            sample_list = [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
    return gate.meta_train(samples=sample_list, state=state, task_context=task_context)


def t_causal_mapping_module(causal_graph: str = "", context_embedding: str = "", goal: str = "", domain: str = "general", state_hint: str = "") -> dict:
    """Map causal graph structure and context embedding onto a goal translation."""
    try:
        graph = {}
        if causal_graph:
            try:
                graph = json.loads(causal_graph) if isinstance(causal_graph, str) else causal_graph
            except Exception:
                graph = {"text": str(causal_graph)}
        return CausalMappingModule(domain=domain, state_hint=state_hint).map(causal_graph=graph, context_embedding=context_embedding, goal=goal)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_causal_graph_data_generator(
    context: str = "",
    goal: str = "",
    modules: str = "",
    symbols: str = "",
    actions: str = "",
    domain: str = "general",
    state_hint: str = "",
    limit: str = "8",
) -> dict:
    """Generate a bounded causal graph packet from task context and module hints."""
    try:
        try:
            lim = max(1, min(int(limit or 8), 16))
        except Exception:
            lim = 8
        context_text = " ".join([part.strip() for part in [context, goal, symbols, actions, state_hint] if str(part or "").strip()])
        graph = DynamicRelationalGraph(query=context_text or goal or context or symbols, domain=domain, state_hint=state_hint).build()
        normalized_nodes = []
        for node in (graph.get("nodes") or [])[:lim]:
            if isinstance(node, dict):
                normalized_nodes.append({
                    "id": node.get("id") or node.get("label") or node.get("name") or "",
                    "label": node.get("label") or node.get("name") or node.get("id") or "",
                    "type": node.get("type") or "node",
                    "weight": node.get("weight") or node.get("score") or node.get("belief") or 0.0,
                })
            else:
                text = str(node).strip()
                if text:
                    normalized_nodes.append({"id": text, "label": text, "type": "node", "weight": 0.0})
        if not normalized_nodes:
            for item in [modules, symbols, actions]:
                for token in [part.strip() for part in str(item or "").replace("\n", ",").split(",") if part.strip()]:
                    normalized_nodes.append({"id": token, "label": token, "type": "node", "weight": 0.0})
        edges = []
        for edge in (graph.get("edges") or [])[:lim]:
            if isinstance(edge, dict):
                edges.append(edge)
            elif isinstance(edge, (list, tuple)) and len(edge) >= 2:
                edges.append({"source": edge[0], "target": edge[1], "relation": "related_to"})
        mapping = CausalMappingModule(domain=domain, state_hint=state_hint).map(
            causal_graph={"nodes": normalized_nodes, "edges": edges},
            context_embedding=context_text or goal,
            goal=goal or context_text,
        )
        summary = (
            f"causal_graph_data_generator=ok | nodes={len(normalized_nodes)} | "
            f"edges={len(edges)} | best={((mapping.get('best_mapping') or {}).get('label') or (mapping.get('best_mapping') or {}).get('node_id') or 'none')}"
        )
        return {
            "ok": True,
            "domain": domain,
            "state_hint": state_hint,
            "context": context_text[:300],
            "goal": goal[:200],
            "modules": modules,
            "symbols": symbols,
            "actions": actions,
            "nodes": normalized_nodes,
            "edges": edges,
            "graph": graph,
            "mapping": mapping,
            "summary": summary,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "summary": f"causal_graph_data_generator=error | {exc}"}


def t_principle_search_module(principles: str = "", state: str = "", goal: str = "", task_context: str = "", domain: str = "general", state_hint: str = "") -> dict:
    """Rank guiding principles against the current state and task context."""
    try:
        return PrincipleSearchModule(domain=domain, state_hint=state_hint).search(principles=principles, state=state, goal=goal, task_context=task_context)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_state_reconciliation_buffer(states: str = "", context: str = "", limit: str = "5", capacity: str = "64", domain: str = "general", state_hint: str = "") -> dict:
    """Reconcile competing state snapshots before downstream planning."""
    try:
        cap = max(8, min(int(capacity or 64), 512))
    except Exception:
        cap = 64
    try:
        lim = max(1, min(int(limit or 5), 32))
    except Exception:
        lim = 5
    buffer = StateReconciliationBuffer(capacity=cap, domain=domain, state_hint=state_hint)
    items = []
    if states:
        raw = states.strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    items = parsed
            except Exception:
                items = [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
        else:
            items = [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
    result = buffer.reconcile(states=items, context=context or state_hint)
    if isinstance(result, dict):
        result["limit"] = lim
    return result


def t_hierarchical_search_tree(current_state: str = "", goal: str = "", hwm_levels: str = "", candidate_actions: str = "", horizon: str = "3", rollouts: str = "12", exploration_weight: str = "1.2", domain: str = "general", state_hint: str = "") -> dict:
    """Build a bounded hierarchical search tree and return the best branch."""
    try:
        hz = max(1, min(int(horizon or 3), 8))
    except Exception:
        hz = 3
    try:
        ro = max(1, min(int(rollouts or 12), 32))
    except Exception:
        ro = 12
    try:
        ew = max(0.1, min(float(exploration_weight or 1.2), 4.0))
    except Exception:
        ew = 1.2
    try:
        return HierarchicalSearchTree(domain=domain, state_hint=state_hint).build(
            current_state=current_state,
            goal=goal,
            hwm_levels=hwm_levels,
            candidate_actions=candidate_actions,
            horizon=hz,
            rollouts=ro,
            exploration_weight=ew,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_module_assessment_packet(
    module_name: str = "",
    module_description: str = "",
    task_context: str = "",
    goal: str = "",
    evidence: str = "",
    domain: str = "general",
    state_hint: str = "",
    learning_rate: str = "0.1",
) -> dict:
    """Assess a new module's cost, overfit risk, and readiness before promotion."""
    try:
        module_name = (module_name or "").strip()
        module_description = (module_description or "").strip()
        task_context = (task_context or "").strip()
        goal = (goal or "").strip()
        evidence = (evidence or "").strip()
        module_blob = " | ".join(part for part in (module_name, module_description, task_context, goal, evidence) if part)
        lower = module_blob.lower()

        complexity_markers = {
            "complex", "cost", "latency", "memory", "compute", "compute_cost", "induction",
            "grammar", "hierarchical", "counterfactual", "causal", "meta-learning", "meta_learning",
            "long-horizon", "long_horizon", "overfit", "robust", "robustness", "generalization",
            "generalisation", "train", "training", "inference", "module", "integration",
        }
        risk_markers = {
            "overfit", "fragile", "brittle", "unstable", "risk", "cost", "latency", "memory",
            "compute", "slow", "expensive", "complex", "conflict", "failure", "drift",
        }
        positive_markers = {
            "verified", "robust", "stable", "efficient", "scalable", "generalization", "safe",
            "bounded", "lightweight", "reusable", "integrated",
        }
        complexity_hits = sum(1 for marker in complexity_markers if marker in lower)
        risk_hits = sum(1 for marker in risk_markers if marker in lower)
        positive_hits = sum(1 for marker in positive_markers if marker in lower)

        complexity_score = max(0.0, min(1.0, round(0.18 + 0.08 * complexity_hits + 0.02 * len(module_blob.split()), 3)))
        risk_score = max(0.0, min(1.0, round(0.15 + 0.1 * risk_hits + 0.02 * max(0, complexity_hits - positive_hits), 3)))
        readiness_score = max(0.0, min(1.0, round(1.0 - (risk_score * 0.55) - (complexity_score * 0.25) + (0.06 * positive_hits), 3)))

        critic = t_simulated_critic(
            sequence=module_blob or goal or module_name,
            reward_signal="robust generalization verify before merge",
            side_effects="overfit cost latency memory risk",
            domain=domain,
            state_hint=state_hint,
        )
        principle_search = t_principle_search_module(
            principles="measure_cost_before_scale,avoid_overfit,prefer_small_safe_changes,verify_before_close",
            state=module_blob or goal or module_name,
            goal=goal or module_name or "assess module readiness",
            task_context=task_context or evidence,
            domain=domain,
            state_hint=state_hint,
        )
        meta = MetaLearner(
            model=PredictiveStateRepresentation(state=module_blob or goal or module_name, domain=domain, state_hint=state_hint),
            domain=domain,
            state_hint=state_hint,
            learning_rate=max(0.01, min(float(learning_rate or 0.1), 1.0)) if str(learning_rate).strip() else 0.1,
        ).adapt(
            error_signal=max(0.0, min(1.0, risk_score)),
            observation=module_blob or evidence or task_context,
            target=goal or module_name,
        )
        gating = DynamicGatingLayer(domain=domain, state_hint=state_hint).gate(
            state=module_blob or goal or module_name,
            task_context=task_context or goal or module_name,
            modules="assess_cost,assess_risk,assess_overfit,verify_integration",
        )

        passed = []
        warnings = []
        failed = []
        if readiness_score >= 0.75:
            passed.append("module_ready")
        elif readiness_score >= 0.6:
            warnings.append("module_needs_more_verification")
        else:
            failed.append("module_not_ready")

        if complexity_score >= 0.65:
            warnings.append("high_complexity")
        if risk_score >= 0.55:
            warnings.append("high_risk")
        if not gating.get("ok", True):
            warnings.append("gating_failed")
        if critic.get("risk") is not None and float(critic.get("risk") or 0.0) >= 0.6:
            warnings.append("critic_high_risk")

        blocked = bool(readiness_score < 0.55 or failed)
        recommendation = "proceed" if readiness_score >= 0.75 else ("verify_more" if readiness_score >= 0.6 else "split_or_delay")
        summary = (
            f"module_assessment={'blocked' if blocked else 'ok'} | "
            f"readiness={readiness_score:.2f} | complexity={complexity_score:.2f} | "
            f"risk={risk_score:.2f} | recommendation={recommendation}"
        )
        return {
            "ok": True,
            "domain": domain,
            "state_hint": state_hint,
            "module_name": module_name,
            "module_description": module_description,
            "task_context": task_context,
            "goal": goal,
            "complexity_score": complexity_score,
            "risk_score": risk_score,
            "readiness_score": readiness_score,
            "recommendation": recommendation,
            "blocked": blocked,
            "passed_checks": passed,
            "failed_checks": failed,
            "warnings": warnings,
            "critic": critic,
            "principle_search": principle_search,
            "meta_update": meta,
            "gating": gating,
            "summary": summary,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "blocked": True}


def t_hierarchical_gated_neuro_symbolic_world_model(
    current_state: str = "",
    goal: str = "",
    modules: str = "",
    symbols: str = "",
    actions: str = "",
    hwm_levels: str = "low,medium,high",
    horizon: str = "3",
    rollouts: str = "8",
    exploration_weight: str = "1.1",
    principles: str = "",
    task_context: str = "",
    causal_graph: str = "",
    domain: str = "general",
    state_hint: str = "",
    full_rollout: str = "false",
) -> dict:
    """Combine gating, symbolic memory, causal mapping, and hierarchical search into one packet."""
    try:
        current_state = (current_state or "").strip()
        goal = (goal or "").strip()
        modules_text = (modules or "").strip() or "neural,symbolic,memory,causal,verification"
        symbols_text = (symbols or "").strip()
        actions_text = (actions or "").strip() or "inspect,gate,verify,store"
        task_context = (task_context or "").strip()
        hwm_levels = (hwm_levels or "low,medium,high").strip()

        try:
            hz = max(1, min(int(horizon or 3), 8))
        except Exception:
            hz = 3
        try:
            ro = max(1, min(int(rollouts or 8), 24))
        except Exception:
            ro = 8
        try:
            ew = max(0.1, min(float(exploration_weight or 1.1), 4.0))
        except Exception:
            ew = 1.1
        full_mode = str(full_rollout or "").strip().lower() in {"1", "true", "yes", "on"}

        reconciler = StateReconciliationBuffer(capacity=64, domain=domain, state_hint=state_hint)
        reconciliation = reconciler.reconcile(
            states=[state for state in (current_state, symbols_text, task_context, goal) if state],
            context=task_context or goal or state_hint,
        )
        reconciled_state = reconciliation.get("normalized_state") or current_state or task_context or goal
        feature_packet = t_domain_invariant_feature_packet(
            current_state=reconciled_state,
            goal=goal or task_context or "",
            modules=modules_text,
            symbols=symbols_text,
            actions=actions_text,
            task_context=task_context,
            hwm_levels=hwm_levels,
            domain=domain,
            state_hint=state_hint,
            limit="8",
        )

        gating = DynamicGatingLayer(domain=domain, state_hint=state_hint).gate(
            state=reconciled_state,
            task_context=task_context or goal or symbols_text,
            modules=modules_text,
        )
        causal_input = {}
        if causal_graph:
            try:
                causal_input = json.loads(causal_graph) if isinstance(causal_graph, str) else causal_graph
            except Exception:
                causal_input = {"text": str(causal_graph)}
        elif modules_text or symbols_text:
            causal_input = {
                "nodes": [
                    {"id": mod.strip(), "label": mod.strip(), "type": "module"}
                    for mod in (modules_text.replace("\n", ",").split(","))
                    if mod.strip()
                ],
                "edges": [],
            }
        causal = t_causal_mapping_module(
            causal_graph=json.dumps(causal_input, default=str) if isinstance(causal_input, dict) else str(causal_input),
            context_embedding=reconciled_state,
            goal=goal or task_context or "hierarchical neuro-symbolic alignment",
            domain=domain,
            state_hint=state_hint,
        )
        principles_packet = t_principle_search_module(
            principles=principles or "hierarchical_gating,verify_before_close,prefer_small_safe_changes,causal_memory_access",
            state=reconciled_state,
            goal=goal or task_context or "hierarchical gating",
            task_context=task_context or symbols_text or actions_text,
            domain=domain,
            state_hint=state_hint,
        )
        tree = t_hierarchical_search_tree(
            current_state=reconciled_state,
            goal=goal or task_context or "hierarchical gating",
            hwm_levels=hwm_levels,
            candidate_actions=actions_text,
            horizon=hz,
            rollouts=ro,
            exploration_weight=ew,
            domain=domain,
            state_hint=state_hint,
        )
        if full_mode:
            prediction = WorldModel(domain=domain, state_hint=state_hint).predict_future_states(
                current_state=reconciled_state,
                actions=actions_text,
                horizon=hz,
            )
        else:
            prediction = {
                "ok": True,
                "domain": domain,
                "state_hint": state_hint,
                "current_state_preview": reconciled_state[:240],
                "horizon": hz,
                "actions": [part.strip() for part in actions_text.replace("\n", ",").split(",") if part.strip()][:8],
                "best_action": tree.get("best_action") or gating.get("best_module") or "observe",
                "predicted_states": [],
                "summary": f"lite_prediction | best={(tree.get('best_action') or gating.get('best_module') or 'observe')}",
                "confidence": round(min(1.0, max(0.2, float((gating.get('gating') or {}).get('best_score') or 0.5))), 3),
            }
        critic = SimulatedCritic(domain=domain, state_hint=state_hint).score(
            sequence=[current_state, goal, symbols_text, actions_text],
            reward_signal="hierarchical gating neural symbolic memory access",
            side_effects="overfit cost latency integration risk",
        )
        meta = MetaLearner(
            model=PredictiveStateRepresentation(state=reconciled_state, domain=domain, state_hint=state_hint),
            domain=domain,
            state_hint=state_hint,
            learning_rate=0.12,
        ).adapt(
            error_signal=max(0.0, min(1.0, 1.0 - float(critic.get("score") or 0.0))),
            observation=f"{modules_text} | {symbols_text} | {actions_text}",
            target=goal or task_context or "hierarchical gating",
        )

        gate_packet = gating.get("gating") if isinstance(gating, dict) and isinstance(gating.get("gating"), dict) else gating
        gate_best = ""
        gate_score = 0.0
        try:
            gate_best = str((gate_packet or {}).get("best_module") or (gate_packet or {}).get("best_action") or "")
            gate_weights = (gate_packet or {}).get("weights") or []
            if gate_weights:
                gate_score = float(gate_weights[0].get("weight") or gate_weights[0].get("probability") or 0.0)
        except Exception:
            gate_best = ""
            gate_score = 0.0
        readiness = max(0.0, min(1.0, round(
            0.35 * float(gate_score or 0.0)
            + 0.25 * float((tree.get("search_tree") or {}).get("summary") and 0.5 or 0.0)
            + 0.2 * float(prediction.get("confidence") or prediction.get("prediction_mean") or 0.0)
            + 0.2 * float(critic.get("score") or 0.0),
            3
        )))

        passed = []
        warnings = []
        failed = []
        if gating.get("ok", True):
            passed.append("gating_ok")
        else:
            failed.append("gating_blocked")
        if reconciliation.get("ok", True):
            passed.append("state_reconciled")
        if causal.get("ok", True):
            passed.append("causal_mapping_ok")
        else:
            warnings.append("causal_mapping_unavailable")
        if principles_packet.get("best_principle"):
            passed.append(f"principle:{principles_packet.get('best_principle')}")
        if critic.get("score") is not None and float(critic.get("score") or 0.0) >= 0.55:
            passed.append("critic_supportive")
        else:
            warnings.append("critic_low_confidence")
        if float((prediction.get("confidence") or prediction.get("prediction_mean") or 0.0)) < 0.45:
            warnings.append("prediction_uncertain")
        feature_score = float(feature_packet.get("feature_score") or 0.0)
        feature_vector = feature_packet.get("feature_vector") or {}
        if feature_score >= 0.7:
            passed.append("feature_packet_strong")
            readiness = min(1.0, readiness + 0.03)
        elif feature_score <= 0.35:
            warnings.append("feature_packet_low_signal")
            readiness = max(0.0, readiness - 0.02)
        if float(feature_vector.get("verification_pressure") or 0.0) >= 0.6:
            passed.append("verification_pressure_detected")
            readiness = min(1.0, readiness + 0.02)

        blocked = bool(not gating.get("ok", True) or readiness < 0.55)
        summary = (
            f"hierarchical_gated_world_model={'blocked' if blocked else 'ok'} | "
            f"best_gate={gate_best or 'none'} | best_principle={principles_packet.get('best_principle') or 'none'} | "
            f"readiness={readiness:.2f} | feature={feature_packet.get('feature_signature') or 'none'}"
        )
        return {
            "ok": True,
            "domain": domain,
            "state_hint": state_hint,
            "current_state": current_state,
            "goal": goal,
            "modules": modules_text,
            "symbols": symbols_text,
            "actions": actions_text,
            "reconciliation": reconciliation,
            "gating": gating,
            "causal_mapping": causal,
            "principle_search": principles_packet,
            "search_tree": tree,
            "prediction": prediction,
            "critic": critic,
            "meta_update": meta,
            "domain_invariant_features": feature_packet,
            "feature_score": feature_score,
            "readiness_score": readiness,
            "blocked": blocked,
            "passed_checks": passed,
            "failed_checks": failed,
            "warnings": warnings,
            "summary": summary,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "blocked": True}


def t_adaptive_temporal_filter(sequence: str = "", domain: str = "general", state_hint: str = "", window: str = "5", decay: str = "0.82") -> dict:
    """Bounded temporal filter for smoothing recent context and ranking sequences."""
    try:
        win = max(1, min(int(window or 5), 25))
        dec = max(0.1, min(float(decay or 0.82), 0.99))
        return AdaptiveTemporalFilter(domain=domain, state_hint=state_hint, window=win, decay=dec).filter_sequence(sequence)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_temporal_attention(sequence: str = "", domain: str = "general", state_hint: str = "", heads: str = "2", window: str = "5") -> dict:
    """Bounded temporal attention layer for contextual ranking."""
    try:
        hd = max(1, min(int(heads or 2), 8))
        win = max(1, min(int(window or 5), 25))
        return TemporalAttention(domain=domain, state_hint=state_hint, heads=hd, window=win).attend(sequence, horizon=win)
    except Exception as e:
        return {"ok": False, "error": str(e)}


class DynamicRouter:
    """Route world-model outputs using bounded policy scoring.

    This is read-only. It can optionally consume a policy from KB and use the
    unified reasoning packet / state evaluator for additional context.
    """

    def __init__(
        self,
        predictions: dict | None,
        confidences: dict | None = None,
        candidates: list[str] | None = None,
        policy: dict | None = None,
        query: str = "",
        domain: str = "general",
        state_hint: str = "",
        use_state_evaluator: bool = False,
        exploration_weight: float = 0.0,
    ):
        self.predictions = predictions if isinstance(predictions, dict) else {}
        self.confidences = confidences if isinstance(confidences, dict) else {}
        self.candidates = [c for c in (candidates or []) if str(c).strip()]
        self.policy = policy if isinstance(policy, dict) else {}
        self.query = (query or "").strip()
        self.domain = (domain or "general").strip()
        self.state_hint = (state_hint or "").strip()
        self.use_state_evaluator = bool(use_state_evaluator)
        try:
            self.exploration_weight = max(0.0, min(float(exploration_weight or 0.0), 1.0))
        except Exception:
            self.exploration_weight = 0.0

    @staticmethod
    def _clamp01(value, default: float = 0.5) -> float:
        try:
            v = float(value)
        except Exception:
            v = default
        return max(0.0, min(1.0, v))

    @staticmethod
    def _score_from_prediction(value) -> float:
        if isinstance(value, (int, float)):
            return DynamicRouter._clamp01(value, 0.5)
        if isinstance(value, dict):
            for k in ("score", "p", "prob", "value", "expected_gain"):
                if k in value:
                    return DynamicRouter._clamp01(value.get(k), 0.5)
        if isinstance(value, str):
            try:
                return DynamicRouter._clamp01(float(value.strip()), 0.5)
            except Exception:
                return 0.5
        return 0.5

    def _policy_weight(self, candidate: str) -> float:
        if not self.policy:
            return 0.5
        c = (candidate or "").strip()
        for key in (c, c.lower()):
            if key in self.policy and isinstance(self.policy.get(key), (int, float, str)):
                return self._clamp01(self.policy.get(key), 0.5)
        for bucket_key in ("route_weights", "work_track_weights", "weights"):
            bucket = self.policy.get(bucket_key)
            if isinstance(bucket, dict):
                for key in (c, c.lower()):
                    if key in bucket:
                        return self._clamp01(bucket.get(key), 0.5)
        return 0.5

    def _evaluator_adjust(self, candidate: str) -> tuple[float, dict]:
        if not (self.use_state_evaluator and self.query):
            return 0.0, {}
        try:
            state_query = f"{self.query}\nROUTE: {candidate}"
            card = StateEvaluator(
                query=state_query,
                domain=self.domain,
                tables=None,
                limit=8,
                per_table=2,
                state_hint=self.state_hint,
            ).evaluate()
            readiness = self._clamp01(card.get("readiness_score"), 0.0)
            risk = self._clamp01(card.get("risk_score"), 0.0)
            return round((readiness * 0.15) - (risk * 0.05), 3), {
                "readiness": readiness,
                "risk": risk,
                "recommendation": card.get("recommendation", ""),
            }
        except Exception as e:
            return 0.0, {"error": str(e)}

    def route(self) -> dict:
        if not self.candidates:
            self.candidates = sorted({str(k) for k in (self.predictions or {}).keys() if str(k).strip()})[:25]
        if not self.candidates:
            return {"ok": False, "error": "candidates required (or provide predictions keys)"}

        scored = []
        for cand in self.candidates:
            pred_val = self.predictions.get(cand) if cand in self.predictions else self.predictions.get(cand.lower())
            pred_score = self._score_from_prediction(pred_val)
            conf_val = self.confidences.get(cand) if cand in self.confidences else self.confidences.get(cand.lower())
            conf_score = self._clamp01(conf_val, 0.5)
            pol_score = self._policy_weight(cand)
            eval_adj, eval_meta = self._evaluator_adjust(cand)
            base = (pred_score * 0.55) + (pol_score * 0.30) + (conf_score * 0.15)
            explore = self.exploration_weight * (0.5 - conf_score) * 0.10
            final = round(max(0.0, min(1.0, base + eval_adj + explore)), 3)
            scored.append({
                "candidate": cand,
                "score": final,
                "components": {
                    "prediction": round(pred_score, 3),
                    "policy": round(pol_score, 3),
                    "confidence": round(conf_score, 3),
                    "evaluator_adjust": eval_adj,
                    "explore": round(explore, 3),
                },
                "evaluator": eval_meta,
            })

        scored.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        best = scored[0]
        return {
            "ok": True,
            "best": best.get("candidate"),
            "best_score": best.get("score"),
            "query": self.query,
            "domain": self.domain,
            "used_policy": bool(self.policy),
            "use_state_evaluator": bool(self.use_state_evaluator and self.query),
            "candidates": scored,
        }


def t_dynamic_router(
    predictions: str = "",
    confidences: str = "",
    candidates: str = "",
    policy_topic: str = "dynamic_router_policy",
    policy_domain: str = "meta",
    policy_override: str = "",
    query: str = "",
    domain: str = "general",
    state_hint: str = "",
    use_state_evaluator: str = "false",
    exploration_weight: str = "0.0",
) -> dict:
    """Route a decision based on world-model outputs + optional learned policy."""
    try:
        def _parse_json_dict(v: str) -> dict:
            if not v:
                return {}
            if isinstance(v, dict):
                return v
            if isinstance(v, str):
                try:
                    parsed = json.loads(v)
                    return parsed if isinstance(parsed, dict) else {}
                except Exception:
                    return {}
            return {}

        pred = _parse_json_dict(predictions)
        conf = _parse_json_dict(confidences)

        cand_list: list[str] = []
        if candidates:
            raw = (candidates or "").strip()
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        cand_list = [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    cand_list = []
            if not cand_list:
                cand_list = [c.strip() for c in raw.split(",") if c.strip()]

        policy = {}
        try:
            rows = sb_get(
                "knowledge_base",
                f"select=content,instruction&domain=eq.{policy_domain}&topic=eq.{policy_topic}&limit=1",
                svc=True,
            ) or []
            if rows:
                blob = (rows[0].get("instruction") or rows[0].get("content") or "").strip()
                start = blob.find("{")
                end = blob.rfind("}")
                if start != -1 and end != -1 and end > start:
                    try:
                        maybe = json.loads(blob[start : end + 1])
                        if isinstance(maybe, dict):
                            policy = maybe
                    except Exception:
                        policy = {}
        except Exception:
            policy = {}
        try:
            override = _parse_json_dict(policy_override)
            if override:
                merged = dict(policy or {})
                merged.update(override)
                policy = merged
        except Exception:
            pass

        u = str(use_state_evaluator or "false").strip().lower() in {"1", "true", "yes", "on"}
        try:
            ew = float(exploration_weight)
        except Exception:
            ew = 0.0

        return DynamicRouter(
            predictions=pred,
            confidences=conf,
            candidates=cand_list,
            policy=policy,
            query=query,
            domain=domain,
            state_hint=state_hint,
            use_state_evaluator=u,
            exploration_weight=ew,
        ).route()
    except Exception as e:
        return {"ok": False, "error": str(e)}
