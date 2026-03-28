"""core_tools_graph.py — relational + causal graph tools.

Keep this module independent of core_tools.py (the facade) to avoid cycles.
It may depend on core_tools_memory for StateEvaluator and reasoning packets.
"""

from __future__ import annotations

import ast
import json
import re as _re

from core_tools_memory import StateEvaluator


class DynamicRelationalGraph:
    """Build a lightweight relational graph from a unified reasoning packet."""

    def __init__(
        self,
        query: str,
        domain: str = "general",
        tables: list | None = None,
        limit: int = 10,
        per_table: int = 2,
        state_hint: str = "",
    ):
        self.query = (query or "").strip()
        self.domain = (domain or "general").strip()
        self.tables = tables
        self.limit = max(1, min(int(limit or 10), 50))
        self.per_table = max(1, min(int(per_table or 2), 5))
        self.state_hint = (state_hint or "").strip()

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

    def build(self) -> dict:
        from core_reasoning_packet import build_reasoning_packet

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

        nodes = [
            {
                "id": "query",
                "label": self.query[:120],
                "type": "query",
                "table": "query",
                "score": 1.0,
            }
        ]
        edges = []
        seen = {"query"}
        table_nodes = {}

        for hit in hits:
            table = hit.get("table") or "unknown"
            raw = hit.get("raw") or {}
            raw_id = raw.get("id") or hit.get("id") or hit.get("topic") or hit.get("title") or "0"
            title = hit.get("title") or hit.get("body") or str(raw_id)
            node_id = self._node_id(table, raw_id, title)
            if node_id in seen:
                continue
            seen.add(node_id)
            node = {
                "id": node_id,
                "label": str(title)[:120],
                "type": self._node_type(table),
                "table": table,
                "score": round(float(hit.get("score") or hit.get("semantic_score") or 0.0), 3),
                "raw_id": raw_id,
            }
            nodes.append(node)
            table_nodes.setdefault(table, []).append(node_id)
            edges.append(
                {
                    "from": "query",
                    "to": node_id,
                    "type": "retrieved_from",
                    "weight": node["score"],
                }
            )

        for table, node_ids in table_nodes.items():
            if len(node_ids) > 1:
                head = node_ids[0]
                for other in node_ids[1:]:
                    edges.append(
                        {
                            "from": head,
                            "to": other,
                            "type": "same_table",
                            "weight": 0.35,
                        }
                    )

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
        }
        graph["density"] = round(len(edges) / max(1, len(nodes)), 3)
        graph["dominant_table"] = (
            max(by_table.items(), key=lambda kv: int(kv[1] or 0))[0] if by_table else None
        )
        return graph


def t_dynamic_relational_graph(
    query: str = "",
    domain: str = "general",
    tables: str = "",
    limit: str = "10",
    per_table: str = "2",
    state_hint: str = "",
) -> dict:
    """Build a deterministic relational graph from unified memory context."""
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
        return DynamicRelationalGraph(
            query=query,
            domain=domain,
            tables=table_list,
            limit=lim,
            per_table=pt,
            state_hint=state_hint,
        ).build()
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
                    for key in (
                        "state",
                        "current_state",
                        "context",
                        "summary",
                        "value",
                        "action",
                        "title",
                        "description",
                    ):
                        value = item.get(key)
                        if value:
                            parts.append(str(value))
                    text = " | ".join(parts).strip() or json.dumps(
                        item, ensure_ascii=False, sort_keys=True
                    )
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
        self.edges.append(
            {
                "source": source,
                "target": target,
                "relation": relation,
                "weight": round(w, 3),
            }
        )

    def get_children(self, node_id: str) -> list[dict]:
        children = [e for e in self.edges if e.get("source") == node_id]
        children.sort(
            key=lambda item: (float(item.get("weight") or 0.0), item.get("target", "")),
            reverse=True,
        )
        return children

    def propagate_belief(
        self, start: str, belief: float = 1.0, decay: float = 0.82, max_depth: int = 3
    ) -> dict:
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
                next_belief = round(
                    max(0.0, min(1.0, current_belief * weight * decay)), 3
                )
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
        if any(
            term in target_lower
            for term in (
                "cause",
                "causal",
                "because",
                "dependency",
                "impact",
                "risk",
                "failure",
                "integration",
            )
        ):
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

        nodes = [
            {
                "id": "query",
                "label": self.query[:120],
                "type": "query",
                "table": "query",
                "score": 1.0,
            }
        ]
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
            edges.append(
                {
                    "source": "query",
                    "target": node_id,
                    "relation": "retrieved_from",
                    "weight": round(self._score_edge(self.query, node["label"], max(0.15, score)), 3),
                }
            )
            if idx > 0:
                prev = ordering[idx - 1]
                prev_node = next((n for n in nodes if n.get("id") == prev), None)
                prev_text = prev_node.get("label", "") if prev_node else self.query
                edges.append(
                    {
                        "source": prev,
                        "target": node_id,
                        "relation": "causal_sequence",
                        "weight": round(self._score_edge(prev_text, node["label"], 0.35), 3),
                    }
                )

        if sequence_items:
            seq_nodes = []
            for idx, text in enumerate(sequence_items[: max(2, min(12, self.limit + 2))]):
                node_id = (
                    f"seq:{idx}:{_re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:18] or 'node'}"
                )
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
                edges.append(
                    {
                        "source": "query",
                        "target": node_id,
                        "relation": "sequence_anchor",
                        "weight": round(self._score_edge(self.query, text, 0.45), 3),
                    }
                )
            for left, right in zip(seq_nodes, seq_nodes[1:]):
                left_node = next((n for n in nodes if n.get("id") == left), None)
                right_node = next((n for n in nodes if n.get("id") == right), None)
                left_text = left_node.get("label", "") if left_node else ""
                right_text = right_node.get("label", "") if right_node else ""
                edges.append(
                    {
                        "source": left,
                        "target": right,
                        "relation": "sequence_flow",
                        "weight": round(self._score_edge(left_text, right_text, 0.42), 3),
                    }
                )

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
        graph["dominant_table"] = (
            max(by_table.items(), key=lambda kv: int(kv[1] or 0))[0] if by_table else None
        )
        return graph


def t_causal_graph(
    query: str = "",
    domain: str = "general",
    tables: str = "",
    limit: str = "10",
    per_table: str = "2",
    state_hint: str = "",
    sequence: str = "",
) -> dict:
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
                    for key in (
                        "state",
                        "current_state",
                        "context",
                        "summary",
                        "value",
                        "action",
                        "title",
                        "description",
                    ):
                        value = item.get(key)
                        if value:
                            parts.append(str(value))
                    text = " | ".join(parts).strip() or json.dumps(
                        item, ensure_ascii=False, sort_keys=True
                    )
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
            relational_score = min(
                0.35, density + (0.15 if dominant_table and dominant_table in lower else 0.0)
            )
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
            ranked_transitions.append(
                {
                    "index": idx,
                    "action": action_text,
                    "score": round(combined, 3),
                    "causal_support": round(causal_score, 3),
                    "relational_support": round(relational_score, 3),
                    "readiness": round(readiness, 3),
                    "coherence": round(coherence, 3),
                    "risk": round(risk, 3),
                    "rationale": "; ".join(rationale_bits),
                }
            )

        ranked_transitions.sort(
            key=lambda item: (
                item["score"],
                item["causal_support"],
                item["relational_support"],
                item["action"],
            ),
            reverse=True,
        )
        best_transition = ranked_transitions[0] if ranked_transitions else {}
        transition_summary = " | ".join(
            [
                causal.get("causal_summary", "")[:120],
                relational.get("summary", "")[:120],
                str(recommendation)[:60],
            ]
        ).strip(" |")
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


def t_causal_graph_inference(
    query: str = "",
    domain: str = "general",
    tables: str = "",
    limit: str = "10",
    per_table: str = "2",
    state_hint: str = "",
    sequence: str = "",
    candidate_actions: str = "",
    horizon: str = "3",
) -> dict:
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
