from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .exceptions import HardInvalidation
from .pydantic_compat import model_construct, model_dump
from .schemas import EdgeType, LongTermNodeType, MemoryNode, NodeType, SummaryRecord
from .utils import cosine_similarity, lexical_overlap, now_ts, stable_hash


@dataclass
class GraphEdge:
    src: str
    dst: str
    type: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ShortTermGraph:
    REQUIRED_NODE_TYPES = {t.value for t in NodeType}
    REQUIRED_EDGE_TYPES = {t.value for t in EdgeType}

    def __init__(self) -> None:
        self.nodes: Dict[str, dict[str, Any]] = {}
        self.edges: list[GraphEdge] = []
        self.hidden_nodes: set[str] = set()

    def add_node(self, node_type: str, label: str, content: Any, **metadata: Any) -> str:
        if node_type not in self.REQUIRED_NODE_TYPES:
            raise ValueError(f"unsupported short-term node type {node_type}")
        node_id = stable_hash(node_type, label, len(self.nodes), now_ts())[:16]
        self.nodes[node_id] = {
            "node_id": node_id,
            "type": node_type,
            "label": label,
            "content": content,
            "metadata": metadata,
            "created_at": now_ts(),
        }
        return node_id

    def add_edge(self, src: str, dst: str, edge_type: str, **metadata: Any) -> None:
        if edge_type not in self.REQUIRED_EDGE_TYPES:
            raise ValueError(f"unsupported short-term edge type {edge_type}")
        if src not in self.nodes or dst not in self.nodes:
            raise KeyError("unknown node in edge")
        self.edges.append(GraphEdge(src=src, dst=dst, type=edge_type, metadata=metadata))

    def summary_replace(self, raw_node_ids: Sequence[str], summary: SummaryRecord) -> str:
        missing = [node_id for node_id in raw_node_ids if node_id not in self.nodes]
        if missing:
            raise KeyError(f"unknown raw nodes: {missing}")
        summary_id = self.add_node(NodeType.SUMMARY.value, summary.objective, model_dump(summary))
        for raw_id in raw_node_ids:
            self.add_edge(summary_id, raw_id, EdgeType.BACKLINKS_TO.value)
            self.hidden_nodes.add(raw_id)
        self._validate_raw_reachability(summary_id, raw_node_ids)
        return summary_id

    def _validate_raw_reachability(self, summary_id: str, raw_node_ids: Sequence[str]) -> None:
        reachable = set()
        queue: deque[str] = deque([summary_id])
        adjacency: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges:
            adjacency[edge.src].append(edge.dst)
        while queue:
            current = queue.popleft()
            if current in reachable:
                continue
            reachable.add(current)
            for nxt in adjacency.get(current, []):
                queue.append(nxt)
        if not set(raw_node_ids).issubset(reachable):
            raise HardInvalidation("short-term compaction destroyed raw-output reachability")

    def validate_hidden_reachability(self) -> None:
        if not self.hidden_nodes:
            return
        summary_ids = [node_id for node_id, node in self.nodes.items() if node["type"] == NodeType.SUMMARY.value]
        if not summary_ids:
            raise HardInvalidation("short-term compaction destroyed raw-output reachability")
        adjacency: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges:
            adjacency[edge.src].append(edge.dst)
        reachable = set()
        queue: deque[str] = deque(summary_ids)
        while queue:
            current = queue.popleft()
            if current in reachable:
                continue
            reachable.add(current)
            for nxt in adjacency.get(current, []):
                queue.append(nxt)
        if not self.hidden_nodes.issubset(reachable):
            raise HardInvalidation("short-term compaction destroyed raw-output reachability")

    def to_jsonable(self) -> dict[str, Any]:
        return {"nodes": self.nodes, "edges": [edge.__dict__ for edge in self.edges], "hidden_nodes": sorted(self.hidden_nodes)}


class LongTermGraph:
    REQUIRED_TYPES = {t.value for t in LongTermNodeType}

    def __init__(self) -> None:
        self.nodes: Dict[str, MemoryNode] = {}

    def reset(self) -> None:
        self.nodes = {}

    def upsert(self, node: MemoryNode) -> None:
        if node.type not in self.REQUIRED_TYPES:
            raise ValueError(f"unsupported long-term node type {node.type}")
        self.nodes[node.node_id] = node

    def tombstone(self, node_id: str) -> None:
        node = self.nodes[node_id]
        node.tombstoned = True
        self.nodes[node_id] = node

    def all_nodes(self) -> list[MemoryNode]:
        return list(self.nodes.values())

    def _query_node(self, query: str) -> MemoryNode:
        return model_construct(
            MemoryNode,
            content=query,
            label=query,
            embedding=[],
            node_id="query",
            type="Query",
            symbol_set=[],
            file_paths=[],
            source_task_id="query",
            verifier_support=0.0,
            timestamps={},
            provenance={},
            tombstoned=False,
        )

    def _neighbor_nodes(self, seeds: Sequence[MemoryNode]) -> list[MemoryNode]:
        if not seeds:
            return []
        seen = {node.node_id for node in seeds}
        expanded: list[tuple[float, MemoryNode]] = []
        for node in self.nodes.values():
            if node.tombstoned or node.node_id in seen:
                continue
            best = 0.0
            for seed in seeds:
                shared_symbols = len(set(node.symbol_set) & set(seed.symbol_set))
                shared_paths = len(set(node.file_paths) & set(seed.file_paths))
                same_task = float(node.source_task_id == seed.source_task_id and node.source_task_id not in {"", "query"})
                overlap = 0.35 * shared_symbols + 0.30 * shared_paths + 0.15 * same_task
                overlap += 0.10 * lexical_overlap(node.label + " " + node.content, seed.label + " " + seed.content)
                overlap += 0.10 * cosine_similarity(node.embedding, seed.embedding)
                best = max(best, overlap)
            if best > 0:
                expanded.append((best, node))
        return [node for _, node in sorted(expanded, key=lambda item: (-item[0], item[1].node_id))]

    def retrieve_candidates(self, query: str, exact_symbols: Sequence[str], file_paths: Sequence[str], top_embedding: int = 5, top_lexical: int = 5) -> list[MemoryNode]:
        exact: list[MemoryNode] = []
        embedding_ranked: list[tuple[float, MemoryNode]] = []
        lexical_ranked: list[tuple[float, MemoryNode]] = []
        exact_symbol_set = set(exact_symbols)
        file_path_set = set(file_paths)
        query_node = self._query_node(query)
        for node in self.nodes.values():
            if node.tombstoned:
                continue
            if exact_symbol_set & set(node.symbol_set) or file_path_set & set(node.file_paths):
                exact.append(node)
                continue
            embedding_ranked.append((cosine_similarity(node.embedding, query_node.embedding), node))
            lexical_ranked.append((lexical_overlap(query, node.content + " " + node.label), node))
        exact_sorted = sorted(
            exact,
            key=lambda node: (
                0 if file_path_set & set(node.file_paths) else 1,
                0 if exact_symbol_set & set(node.symbol_set) else 1,
                -node.verifier_support,
                node.node_id,
            ),
        )
        one_hop = self._neighbor_nodes(exact_sorted)[: max(top_embedding, top_lexical)]
        embedding_sorted = [node for _, node in sorted(embedding_ranked, key=lambda item: item[0], reverse=True)[:top_embedding]]
        lexical_sorted = [node for _, node in sorted(lexical_ranked, key=lambda item: item[0], reverse=True)[:top_lexical]]
        merged = []
        seen = set()
        for node in exact_sorted + one_hop + embedding_sorted + lexical_sorted:
            if node.node_id in seen:
                continue
            merged.append(node)
            seen.add(node.node_id)
        return merged
