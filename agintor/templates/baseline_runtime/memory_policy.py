from __future__ import annotations

from typing import Any, Iterable, Sequence

from agintor.schemas import MemoryNode, SummaryRecord
from agintor.utils import cheap_embedding, clip, cosine_similarity, jaccard, lexical_overlap, sigmoid


class MemoryPolicy:
    B_HI = 0.75
    B_LO = 0.55
    THETA_E = 0.92
    THETA_L = 0.60
    THETA_PROM = 0.55
    ETA_VERIFY = 0.50

    def select_spans_for_compaction(self, ctx, span_ids: Sequence[str], active_fraction: float) -> list[list[str]]:
        if active_fraction <= self.B_HI:
            return []
        candidates = []
        for node_id in span_ids:
            node = ctx.shell.short_term.nodes[node_id]
            token_saving = max(1, len(str(node["content"])))
            retained_utility = 0.65 + 0.05 * (node["type"] == "Event")
            info_loss = 0.20 if node["type"] == "Event" else 0.15
            comp_latency = 0.05
            orphan_penalty = 0.0
            score = retained_utility + 0.04 * token_saving - 0.15 * info_loss - 0.05 * comp_latency - 0.20 * orphan_penalty
            density = score / max(1, token_saving)
            if density > 0:
                candidates.append((density, [node_id]))
        chosen = []
        for _, node_group in sorted(candidates, key=lambda item: (-item[0], item[1][0])):
            chosen.append(node_group)
            active_fraction -= 0.05
            if active_fraction <= self.B_LO:
                break
        return chosen

    def summarize_span(self, ctx, nodes: Sequence[dict[str, Any]]) -> SummaryRecord:
        prompt = "\n".join(f"{node['type']}: {node['content']}" for node in nodes)
        response = ctx.provider.generate(
            type("Req", (), {
                "instructions": "Summarize evidence while preserving unresolved handles and artifacts.",
                "prompt": prompt,
                "model_class": "small",
                "seed": ctx.seed,
                "metadata": {"mode": "summary"},
            })
        )
        artifacts = [str(node["metadata"].get("artifact_ref", "")) for node in nodes if node["metadata"].get("artifact_ref")]
        symbols = sorted({symbol for node in nodes for symbol in node["metadata"].get("symbols", [])})
        return SummaryRecord(
            objective=ctx.task.prompt,
            evidence=[response.text],
            artifacts=artifacts,
            unresolved=list(ctx.state.unresolved_goals),
            open_handles=list(ctx.state.open_handle_ids),
            next_actions=["resume" if ctx.state.unresolved_goals else "stop"],
            symbols=symbols,
            verifier_state={"verified": False},
            provenance={"node_count": len(nodes)},
        )

    def retrieve_long_term(self, ctx, query: str, exact_symbols: Sequence[str], file_paths: Sequence[str], candidates: Sequence[MemoryNode]) -> list[MemoryNode]:
        scored = []
        for node in candidates:
            exact = bool(set(exact_symbols) & set(node.symbol_set)) or bool(set(file_paths) & set(node.file_paths))
            if exact:
                score = 1.0 + 0.25 * bool(set(file_paths) & set(node.file_paths)) + 0.20 * node.verifier_support + (0.10 if node.provenance.get("source") == "task_context" else 0.0)
            else:
                cos = cosine_similarity(node.embedding, cheap_embedding(query))
                lex = lexical_overlap(node.label + " " + node.content, query)
                type_match = 1.0 if node.type in {"Symbol", "File"} else 0.5
                path_bonus = 1.0 if file_paths and set(file_paths) & set(node.file_paths) else 0.0
                recency = 1.0
                verify = node.verifier_support
                provenance = 1.0 if node.provenance.get("source") == "task_context" else 0.5
                staleness = 0.0
                score = 0.30 * cos + 0.20 * lex + 0.15 * type_match + 0.10 * path_bonus + 0.10 * recency + 0.10 * verify + 0.05 * provenance - 0.05 * staleness
            scored.append((score, node))
        return [node for _, node in sorted(scored, key=lambda item: (-item[0], item[1].node_id))]

    def score_memory_unit(self, ctx, unit: MemoryNode, existing_nodes: Sequence[MemoryNode]) -> float:
        novelty = 1.0 - max((jaccard(unit.symbol_set, node.symbol_set) for node in existing_nodes), default=0.0)
        reuse = 1.0 if unit.type in {"Symbol", "File"} else 0.5
        centrality = 0.8 if unit.type in {"Symbol", "File"} else 0.4
        verifier = unit.verifier_support
        task_spread = 0.5
        compositional = 0.7 if unit.type == "Symbol" else 0.4
        duplicate = max((lexical_overlap(unit.content, node.content) for node in existing_nodes), default=0.0)
        write_cost = 0.1
        contradiction = 0.0
        logits = 1.2 * novelty + 0.9 * reuse + 0.8 * centrality + 1.0 * verifier + 0.5 * task_spread + 0.6 * compositional - 1.0 * duplicate - 0.4 * write_cost - 0.8 * contradiction
        return sigmoid(logits)

    def should_promote(self, ctx, unit: MemoryNode, score: float) -> bool:
        if unit.type in {"Symbol", "File"}:
            return score >= self.THETA_PROM and unit.verifier_support >= self.ETA_VERIFY
        return score >= self.THETA_PROM

    def dedup_candidates(self, ctx, unit: MemoryNode, existing_nodes: Sequence[MemoryNode]) -> tuple[str, str | None]:
        for node in existing_nodes:
            same_type = unit.type == node.type
            primary = unit.label == node.label or bool(set(unit.file_paths) & set(node.file_paths))
            exactsym = bool(set(unit.symbol_set) & set(node.symbol_set))
            namespace = bool(set(unit.file_paths) & set(node.file_paths)) or bool(set(unit.symbol_set) & set(node.symbol_set))
            emb = cosine_similarity(unit.embedding, node.embedding)
            lex = lexical_overlap(unit.content, node.content)
            if same_type and (primary or (exactsym and namespace) or (emb > self.THETA_E and lex > self.THETA_L)):
                return ("merge", node.node_id)
        return ("new", None)

    def upsert_memory(self, ctx, unit: MemoryNode, action: str, target_id: str | None) -> None:
        graph = ctx.shell.long_term
        if action == "merge" and target_id is not None:
            existing = graph.nodes[target_id]
            merged = existing.copy(update={
                "content": existing.content if len(existing.content) >= len(unit.content) else unit.content,
                "symbol_set": sorted(set(existing.symbol_set) | set(unit.symbol_set)),
                "file_paths": sorted(set(existing.file_paths) | set(unit.file_paths)),
                "verifier_support": max(existing.verifier_support, unit.verifier_support),
            })
            graph.upsert(merged)
            return
        if action == "tombstone" and target_id is not None:
            graph.tombstone(target_id)
            return
        graph.upsert(unit)
