from __future__ import annotations

from typing import Any, Iterable, Sequence

from agintor.prompts import load_prompt_spec
from agintor.schemas import MemoryNode, SummaryRecord
from agintor.utils import cheap_embedding, clip, cosine_similarity, jaccard, lexical_overlap, sigmoid


class MemoryPolicy:
    B_HI = 0.75
    B_LO = 0.55
    THETA_E = 0.92
    THETA_L = 0.60
    THETA_PROM = 0.55
    ETA_VERIFY = 0.50
    MAX_SUMMARIES_PER_PASS = 3
    TOKEN_WINDOW = 512.0

    def select_spans_for_compaction(self, ctx, span_ids: Sequence[str], active_fraction: float) -> list[list[str]]:
        if active_fraction <= self.B_HI:
            return []
        candidates: list[tuple[float, list[str], int]] = []
        windows: list[list[str]] = [[node_id] for node_id in span_ids]
        if len(span_ids) >= 2:
            windows.extend([list(span_ids[idx : idx + 2]) for idx in range(0, len(span_ids) - 1, 2)])
        for node_group in windows:
            nodes = [ctx.shell.short_term.nodes[node_id] for node_id in node_group]
            token_saving = sum(max(1, len(str(node["content"]))) for node in nodes)
            retained_utility = 0.55 + 0.10 * any(node["type"] == "Event" for node in nodes) + 0.05 * any(node["type"] == "VerifierEvidence" for node in nodes)
            info_loss = 0.10 + 0.06 * any(node["type"] == "Event" for node in nodes) + 0.04 * any(node["type"] == "Artifact" for node in nodes)
            comp_latency = 0.03 * len(node_group)
            orphan_penalty = 0.15 * sum(
                1
                for node in nodes
                if node["metadata"].get("artifact_ref") or node["type"] in {"OpenHandle", "Artifact", "VerifierEvidence"}
            )
            score = retained_utility + 0.04 * token_saving - 0.15 * info_loss - 0.05 * comp_latency - 0.20 * orphan_penalty
            density = score / max(1, token_saving)
            if density > 0:
                candidates.append((density, list(node_group), token_saving))
        chosen = []
        claimed = set()
        remaining_fraction = active_fraction
        token_window = max(1.0, float(getattr(ctx.budget, "context_window_tokens", self.TOKEN_WINDOW)))
        for _, node_group, token_saving in sorted(candidates, key=lambda item: (-item[0], item[1][0])):
            if any(node_id in claimed for node_id in node_group):
                continue
            chosen.append(node_group)
            claimed.update(node_group)
            estimated_saved_fraction = max(0.05, 0.80 * (token_saving / token_window))
            remaining_fraction -= estimated_saved_fraction
            if remaining_fraction <= self.B_LO or len(chosen) >= self.MAX_SUMMARIES_PER_PASS:
                break
        return chosen

    def summarize_span(self, ctx, nodes: Sequence[dict[str, Any]]) -> SummaryRecord:
        spec = load_prompt_spec("memory.span_summarize.v1")
        prompt = "\n".join(f"{node['type']}: {node['content']}" for node in nodes)
        response = ctx.provider.generate(
            type("Req", (), {
                "instructions": spec.instructions,
                "prompt": prompt,
                "model_class": spec.model_class,
                "seed": ctx.seed,
                "metadata": {"mode": "summary"},
            })
        )
        ctx.consume_model_response(response, purpose="memory_summary")
        artifacts = sorted(
            {
                str(node["metadata"].get("artifact_ref", "") or node["label"])
                for node in nodes
                if node["metadata"].get("artifact_ref") or node["type"] == "Artifact"
            }
        )
        symbols = sorted(
            {
                symbol
                for node in nodes
                for symbol in node["metadata"].get("symbols", [])
            }
        )
        evidence = [response.text]
        for node in nodes:
            if node["type"] == "VerifierEvidence":
                evidence.append(f"evidence:{node['label']}={node['content']}")
            elif node["type"] == "Event":
                evidence.append(f"event:{node['label']}")
        open_handles = sorted(
            {
                str(node["content"].get("handle_id", "") or node["label"])
                for node in nodes
                if node["type"] == "OpenHandle" and isinstance(node["content"], dict)
            }
        )
        return SummaryRecord(
            objective=ctx.task.prompt,
            evidence=evidence[:6],
            artifacts=artifacts,
            unresolved=list(ctx.state.unresolved_goals),
            open_handles=sorted(set(list(ctx.state.open_handle_ids) + open_handles)),
            next_actions=["resume" if ctx.state.unresolved_goals else "stop"],
            symbols=symbols,
            verifier_state={"verified": False},
            provenance={"node_count": len(nodes), "node_types": [node["type"] for node in nodes]},
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
                recency = 1.0 / max(1.0, 1.0 + abs(node.timestamps.get("created", 0.0) - ctx.task.metadata.get("created", node.timestamps.get("created", 0.0))))
                verify = node.verifier_support
                provenance = 1.0 if node.provenance.get("source") == "task_context" else 0.5
                staleness = max(0.0, float(node.provenance.get("staleness", 0.0)))
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
            primary = bool(set(unit.file_paths) & set(node.file_paths))
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
        if action == "refine" and target_id is not None:
            existing = graph.nodes[target_id]
            refined = existing.copy(update={
                "content": unit.content,
                "embedding": unit.embedding,
                "verifier_support": max(existing.verifier_support, unit.verifier_support),
                "timestamps": {**existing.timestamps, **unit.timestamps},
                "provenance": {**existing.provenance, **unit.provenance},
            })
            graph.upsert(refined)
            return
        if action == "tombstone" and target_id is not None:
            graph.tombstone(target_id)
            return
        graph.upsert(unit)
