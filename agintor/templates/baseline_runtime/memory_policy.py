from __future__ import annotations

from typing import Any, Sequence

from agintor_runtime.prompts import load_prompt_spec
from agintor_runtime.schemas import MemoryNode, SummaryRecord
from agintor_runtime.utils import cheap_embedding, cosine_similarity, jaccard, lexical_overlap, sigmoid


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
        profile = ctx.profile.memory
        weights = profile.compaction_weights
        if active_fraction <= profile.b_hi:
            return []
        candidates: list[tuple[float, list[str], int]] = []
        windows: list[list[str]] = [[node_id] for node_id in span_ids]
        if len(span_ids) >= 2:
            windows.extend([list(span_ids[idx : idx + 2]) for idx in range(0, len(span_ids) - 1, 2)])
        for node_group in windows:
            nodes = [ctx.shell.short_term.nodes[node_id] for node_id in node_group]
            token_saving = sum(max(1, len(str(node["content"]))) for node in nodes)
            retained_utility = (
                weights["retained_utility"]
                + weights["event_bonus"] * any(node["type"] == "Event" for node in nodes)
                + weights["verifier_bonus"] * any(node["type"] == "VerifierEvidence" for node in nodes)
            )
            info_loss = 0.10 + 0.06 * any(node["type"] == "Event" for node in nodes) + 0.04 * any(node["type"] == "Artifact" for node in nodes)
            comp_latency = 0.03 * len(node_group)
            orphan_penalty = 0.15 * sum(
                1
                for node in nodes
                if node["metadata"].get("artifact_ref") or node["type"] in {"OpenHandle", "Artifact", "VerifierEvidence"}
            )
            score = (
                retained_utility
                + weights["token_saving"] * token_saving
                - weights["info_loss"] * info_loss
                - weights["latency"] * comp_latency
                - weights["orphan"] * orphan_penalty
            )
            density = score / max(1, token_saving)
            if density > 0:
                candidates.append((density, list(node_group), token_saving))
        chosen = []
        claimed = set()
        remaining_fraction = active_fraction
        token_window = max(1.0, float(getattr(ctx.budget, "context_window_tokens", profile.token_window)))
        for _, node_group, token_saving in sorted(candidates, key=lambda item: (-item[0], item[1][0])):
            if any(node_id in claimed for node_id in node_group):
                continue
            chosen.append(node_group)
            claimed.update(node_group)
            estimated_saved_fraction = max(0.05, 0.80 * (token_saving / token_window))
            remaining_fraction -= estimated_saved_fraction
            if remaining_fraction <= profile.b_lo or len(chosen) >= profile.max_summaries_per_pass:
                break
        return chosen

    def summarize_span(self, ctx, nodes: Sequence[dict[str, Any]]) -> SummaryRecord:
        spec = load_prompt_spec(ctx.profile.prompts.memory_summary)
        prompt = "\n".join(f"{node['type']}: {node['content']}" for node in nodes)
        response = ctx.run_model_request(
            instructions=spec.instructions,
            prompt=prompt,
            model_class=spec.model_class,
            purpose="summary",
            payload={"node_count": len(nodes)},
            trace_context=ctx.derive_trace_context(frame_role="memory", op_id="memory_summary"),
        )
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
        weights = ctx.profile.memory.retrieval_weights
        scored = []
        for node in candidates:
            exact = bool(set(exact_symbols) & set(node.symbol_set)) or bool(set(file_paths) & set(node.file_paths))
            if exact:
                score = (
                    1.0
                    + weights["exact_path_bonus"] * bool(set(file_paths) & set(node.file_paths))
                    + weights["exact_verify_bonus"] * node.verifier_support
                    + (weights["exact_task_context_bonus"] if node.provenance.get("source") == "task_context" else 0.0)
                )
            else:
                cos = cosine_similarity(node.embedding, cheap_embedding(query))
                lex = lexical_overlap(node.label + " " + node.content, query)
                type_match = 1.0 if node.type in {"Symbol", "File"} else 0.5
                path_bonus = 1.0 if file_paths and set(file_paths) & set(node.file_paths) else 0.0
                recency = 1.0 / max(
                    1.0,
                    1.0 + abs(node.timestamps.get("created", 0.0) - ctx.task.metadata.get("created", node.timestamps.get("created", 0.0))),
                )
                verify = node.verifier_support
                provenance = 1.0 if node.provenance.get("source") == "task_context" else 0.5
                staleness = max(0.0, float(node.provenance.get("staleness", 0.0)))
                score = (
                    weights["cos"] * cos
                    + weights["lex"] * lex
                    + weights["type"] * type_match
                    + weights["path"] * path_bonus
                    + weights["recency"] * recency
                    + weights["verify"] * verify
                    + weights["provenance"] * provenance
                    - weights["staleness"] * staleness
                )
            scored.append((score, node))
        return [node for _, node in sorted(scored, key=lambda item: (-item[0], item[1].node_id))]

    def score_memory_unit(self, ctx, unit: MemoryNode, existing_nodes: Sequence[MemoryNode]) -> float:
        weights = ctx.profile.memory.promotion_weights
        novelty = 1.0 - max((jaccard(unit.symbol_set, node.symbol_set) for node in existing_nodes), default=0.0)
        reuse = 1.0 if unit.type in {"Symbol", "File"} else 0.5
        centrality = 0.8 if unit.type in {"Symbol", "File"} else 0.4
        verifier = unit.verifier_support
        task_spread = 0.5
        compositional = 0.7 if unit.type == "Symbol" else 0.4
        duplicate = max((lexical_overlap(unit.content, node.content) for node in existing_nodes), default=0.0)
        write_cost = 0.1
        contradiction = 0.0
        logits = (
            weights["novelty"] * novelty
            + weights["reuse"] * reuse
            + weights["centrality"] * centrality
            + weights["verifier"] * verifier
            + weights["task_spread"] * task_spread
            + weights["compositional"] * compositional
            - weights["duplicate"] * duplicate
            - weights["write_cost"] * write_cost
            - weights["contradiction"] * contradiction
        )
        return sigmoid(logits)

    def should_promote(self, ctx, unit: MemoryNode, score: float) -> bool:
        profile = ctx.profile.memory
        if unit.type in {"Symbol", "File"}:
            return score >= profile.theta_prom and unit.verifier_support >= profile.eta_verify
        return score >= profile.theta_prom

    def dedup_candidates(self, ctx, unit: MemoryNode, existing_nodes: Sequence[MemoryNode]) -> tuple[str, str | None]:
        profile = ctx.profile.memory
        for node in existing_nodes:
            same_type = unit.type == node.type
            primary = bool(set(unit.file_paths) & set(node.file_paths))
            exactsym = bool(set(unit.symbol_set) & set(node.symbol_set))
            namespace = bool(set(unit.file_paths) & set(node.file_paths)) or bool(set(unit.symbol_set) & set(node.symbol_set))
            emb = cosine_similarity(unit.embedding, node.embedding)
            lex = lexical_overlap(unit.content, node.content)
            if same_type and (primary or (exactsym and namespace) or (emb > profile.theta_e and lex > profile.theta_l)):
                return ("merge", node.node_id)
        return ("new", None)

    def upsert_memory(self, ctx, unit: MemoryNode, action: str, target_id: str | None) -> None:
        graph = ctx.shell.long_term
        if action == "merge" and target_id is not None:
            existing = graph.nodes[target_id]
            merged = existing.model_copy(
                update={
                    "content": existing.content if len(existing.content) >= len(unit.content) else unit.content,
                    "symbol_set": sorted(set(existing.symbol_set) | set(unit.symbol_set)),
                    "file_paths": sorted(set(existing.file_paths) | set(unit.file_paths)),
                    "verifier_support": max(existing.verifier_support, unit.verifier_support),
                }
            )
            graph.upsert(merged)
            return
        if action == "refine" and target_id is not None:
            existing = graph.nodes[target_id]
            refined = existing.model_copy(
                update={
                    "content": unit.content,
                    "embedding": unit.embedding,
                    "verifier_support": max(existing.verifier_support, unit.verifier_support),
                    "timestamps": {**existing.timestamps, **unit.timestamps},
                    "provenance": {**existing.provenance, **unit.provenance},
                }
            )
            graph.upsert(refined)
            return
        if action == "tombstone" and target_id is not None:
            graph.tombstone(target_id)
            return
        graph.upsert(unit)
