from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .exceptions import HardInvalidation
from .schemas import (
    EdgeType,
    LongTermEdgeRecord,
    LongTermEdgeType,
    LongTermGraphSnapshot,
    LongTermNodeType,
    LongTermWriteRecord,
    MemoryNode,
    NodeType,
    RetrievalDiagnosticRecord,
    RetrievalSignalRow,
    ShortTermGraphSnapshot,
    SummaryRecord,
)
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
        summary_id = self.add_node(NodeType.SUMMARY.value, summary.objective, (summary).model_dump())
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

    def snapshot(
        self,
        *,
        branch_publications: Sequence[Mapping[str, Any]] = (),
        side_effect_receipts: Sequence[Mapping[str, Any]] = (),
        runtime_event_refs: Sequence[str] = (),
    ) -> ShortTermGraphSnapshot:
        summary_backlinks = [
            {"summary_node_id": edge.src, "raw_node_id": edge.dst, "metadata": dict(edge.metadata)}
            for edge in self.edges
            if edge.type == EdgeType.BACKLINKS_TO.value
        ]
        artifact_lineage = [
            {
                "producer_node_id": edge.src,
                "artifact_node_id": edge.dst,
                "artifact_ref": str(
                    self.nodes.get(edge.dst, {}).get("metadata", {}).get("artifact_ref", "")
                    or self.nodes.get(edge.dst, {}).get("label", "")
                ),
                "metadata": dict(edge.metadata),
            }
            for edge in self.edges
            if edge.type == EdgeType.PRODUCES.value
        ]
        open_handle_lineage = [
            {
                "waiting_node_id": edge.src,
                "open_handle_node_id": edge.dst,
                "handle_id": str(
                    self.nodes.get(edge.dst, {}).get("content", {}).get("handle_id", "")
                    if isinstance(self.nodes.get(edge.dst, {}).get("content"), dict)
                    else ""
                ),
                "metadata": dict(edge.metadata),
            }
            for edge in self.edges
            if edge.type == EdgeType.WAITS_ON.value
        ]
        verifier_evidence_refs = sorted(
            node_id
            for node_id, node in self.nodes.items()
            if node.get("type") == NodeType.VERIFIER_EVIDENCE.value
        )
        return ShortTermGraphSnapshot(
            nodes={str(node_id): dict(payload) for node_id, payload in self.nodes.items()},
            edges=[
                {
                    "src": edge.src,
                    "dst": edge.dst,
                    "type": edge.type,
                    "metadata": dict(edge.metadata),
                }
                for edge in self.edges
            ],
            hidden_nodes=sorted(self.hidden_nodes),
            summary_backlinks=summary_backlinks,
            artifact_lineage=artifact_lineage,
            branch_publication_lineage=[dict(item) for item in branch_publications],
            open_handle_lineage=open_handle_lineage,
            verifier_evidence_refs=verifier_evidence_refs,
            receipt_refs=sorted(
                str(item.get("side_effect_id", ""))
                for item in side_effect_receipts
                if str(item.get("side_effect_id", "")).strip()
            ),
            event_refs=sorted(str(ref) for ref in runtime_event_refs if str(ref).strip()),
        )

    def restore(self, snapshot: Mapping[str, Any] | ShortTermGraphSnapshot) -> None:
        graph_snapshot = (
            snapshot
            if isinstance(snapshot, ShortTermGraphSnapshot)
            else (ShortTermGraphSnapshot).model_validate(snapshot)
        )
        self.nodes = {
            str(node_id): dict(payload)
            for node_id, payload in graph_snapshot.nodes.items()
        }
        self.edges = [
            GraphEdge(
                src=str(edge.get("src", "")),
                dst=str(edge.get("dst", "")),
                type=str(edge.get("type", "")),
                metadata=dict(edge.get("metadata", {})),
            )
            for edge in graph_snapshot.edges
        ]
        self.hidden_nodes = {str(node_id) for node_id in graph_snapshot.hidden_nodes}
        self.validate_hidden_reachability()

    @classmethod
    def fork_from_snapshot(cls, snapshot: Mapping[str, Any] | ShortTermGraphSnapshot) -> "ShortTermGraph":
        graph = cls()
        graph.restore(snapshot)
        return graph

    def to_jsonable(self) -> dict[str, Any]:
        return (self.snapshot()).model_dump()


class LongTermGraph:
    REQUIRED_TYPES = {t.value for t in LongTermNodeType}
    WRITE_ACTIONS = {"upsert", "merge", "refine", "tombstone", "conflict"}

    def __init__(self) -> None:
        self.nodes: Dict[str, MemoryNode] = {}
        self.edges: list[LongTermEdgeRecord] = []
        self.write_log: list[LongTermWriteRecord] = []
        self.retrieval_diagnostics: list[RetrievalDiagnosticRecord] = []
        self._latest_write_by_node: dict[str, str] = {}
        self._write_context_stack: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.nodes = {}
        self.edges = []
        self.write_log = []
        self.retrieval_diagnostics = []
        self._latest_write_by_node = {}
        self._write_context_stack = []

    @contextmanager
    def write_scope(
        self,
        *,
        action: str | None = None,
        target_node_id: str | None = None,
        source_task_id: str | None = None,
        source_attempt_id: str = "",
        source_checkpoint_ref: str | None = None,
        verifier_support_refs: Sequence[str] = (),
        payload_ref: str | None = None,
    ) -> Iterable[None]:
        self._write_context_stack.append(
            {
                "action": action,
                "target_node_id": target_node_id,
                "source_task_id": source_task_id,
                "source_attempt_id": source_attempt_id,
                "source_checkpoint_ref": source_checkpoint_ref,
                "verifier_support_refs": list(verifier_support_refs),
                "payload_ref": payload_ref,
            }
        )
        try:
            yield
        finally:
            self._write_context_stack.pop()

    def _active_write_context(self) -> Mapping[str, Any]:
        return self._write_context_stack[-1] if self._write_context_stack else {}

    def _validate_node(self, node: MemoryNode) -> None:
        if node.type not in self.REQUIRED_TYPES:
            raise ValueError(f"unsupported long-term node type {node.type}")

    @staticmethod
    def _normalize_action(action: str) -> str:
        normalized = "upsert" if action == "new" else str(action or "upsert")
        if normalized not in LongTermGraph.WRITE_ACTIONS:
            raise ValueError(f"unsupported long-term write action {action}")
        return normalized

    def _write_record(
        self,
        *,
        action: str,
        target_node_id: str,
        source_task_id: str | None,
        source_attempt_id: str,
        source_checkpoint_ref: str | None,
        verifier_support_refs: Sequence[str],
        prior_write_id: str | None,
        contradiction_target_write_id: str | None = None,
        payload_ref: str | None = None,
    ) -> LongTermWriteRecord:
        written_at = now_ts()
        write_id = f"ltw.{stable_hash(action, target_node_id, prior_write_id, contradiction_target_write_id, written_at)[:16]}"
        record = LongTermWriteRecord(
            write_id=write_id,
            target_node_id=target_node_id,
            action=action,
            payload_ref=payload_ref or f"state/long_term/writes/{write_id}.json",
            source_task_id=source_task_id,
            source_attempt_id=source_attempt_id,
            source_checkpoint_ref=source_checkpoint_ref,
            verifier_support_refs=sorted({str(ref) for ref in verifier_support_refs if str(ref).strip()}),
            prior_write_id=prior_write_id,
            contradiction_target_write_id=contradiction_target_write_id,
            written_at=written_at,
        )
        self.write_log.append(record)
        if action != "conflict":
            self._latest_write_by_node[target_node_id] = write_id
        return record

    def _edge_record(
        self,
        *,
        source_node_id: str,
        target_node_id: str,
        edge_type: LongTermEdgeType,
        introducing_write_id: str,
        written_at: float | None = None,
    ) -> LongTermEdgeRecord:
        timestamp = written_at if written_at is not None else now_ts()
        edge = LongTermEdgeRecord(
            edge_id=f"lte.{stable_hash(source_node_id, target_node_id, edge_type.value, introducing_write_id)[:16]}",
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            edge_type=edge_type.value,
            introducing_write_id=introducing_write_id,
            tombstoned=False,
            tombstone_write_id=None,
            written_at=timestamp,
        )
        self.edges.append(edge)
        return edge

    def _contradiction_target(self, node: MemoryNode, *, exclude_node_id: str | None = None) -> MemoryNode | None:
        explicit_target = str(node.provenance.get("contradicts", "") or "").strip()
        if explicit_target and explicit_target in self.nodes:
            candidate = self.nodes[explicit_target]
            return None if candidate.tombstoned else candidate
        node_symbols = set(node.symbol_set)
        node_paths = set(node.file_paths)
        for existing in self.nodes.values():
            if existing.tombstoned or existing.node_id == exclude_node_id or existing.node_id == node.node_id:
                continue
            same_namespace = (
                existing.type == node.type
                and (
                    bool(node_symbols & set(existing.symbol_set))
                    or bool(node_paths & set(existing.file_paths))
                    or bool(node.label and node.label == existing.label)
                )
            )
            if not same_namespace:
                continue
            if lexical_overlap(existing.content, node.content) < 0.15 and max(existing.verifier_support, node.verifier_support) >= 0.5:
                return existing
        return None

    def _emit_conflict_if_needed(
        self,
        node: MemoryNode,
        *,
        introducing_write: LongTermWriteRecord,
        source_task_id: str | None,
        source_attempt_id: str,
        source_checkpoint_ref: str | None,
        verifier_support_refs: Sequence[str],
        exclude_node_id: str | None = None,
    ) -> LongTermWriteRecord | None:
        target = self._contradiction_target(node, exclude_node_id=exclude_node_id)
        if target is None:
            return None
        conflict = self._write_record(
            action="conflict",
            target_node_id=node.node_id,
            source_task_id=source_task_id,
            source_attempt_id=source_attempt_id,
            source_checkpoint_ref=source_checkpoint_ref,
            verifier_support_refs=verifier_support_refs,
            prior_write_id=introducing_write.write_id,
            contradiction_target_write_id=self._latest_write_by_node.get(target.node_id),
        )
        self._edge_record(
            source_node_id=node.node_id,
            target_node_id=target.node_id,
            edge_type=LongTermEdgeType.CONTRADICTS,
            introducing_write_id=conflict.write_id,
            written_at=conflict.written_at,
        )
        return conflict

    def write(
        self,
        node: MemoryNode,
        *,
        action: str | None = None,
        target_node_id: str | None = None,
        source_task_id: str | None = None,
        source_attempt_id: str = "",
        source_checkpoint_ref: str | None = None,
        verifier_support_refs: Sequence[str] = (),
        payload_ref: str | None = None,
    ) -> LongTermWriteRecord:
        write_context = self._active_write_context()
        effective_action = action if action is not None else write_context.get("action")
        effective_target_node_id = target_node_id if target_node_id is not None else write_context.get("target_node_id")
        effective_source_task_id = source_task_id if source_task_id is not None else write_context.get("source_task_id")
        effective_source_attempt_id = source_attempt_id or str(write_context.get("source_attempt_id") or "")
        effective_source_checkpoint_ref = (
            source_checkpoint_ref
            if source_checkpoint_ref is not None
            else write_context.get("source_checkpoint_ref")
        )
        effective_verifier_support_refs = (
            verifier_support_refs
            if verifier_support_refs
            else write_context.get("verifier_support_refs", ())
        )
        effective_payload_ref = payload_ref or write_context.get("payload_ref")
        normalized = self._normalize_action(str(effective_action or "upsert"))
        self._validate_node(node)
        target_id = str(effective_target_node_id or node.node_id)
        prior_write_id = self._latest_write_by_node.get(target_id)

        if normalized == "tombstone":
            return self.tombstone(
                target_id,
                source_task_id=effective_source_task_id,
                source_attempt_id=effective_source_attempt_id,
                source_checkpoint_ref=effective_source_checkpoint_ref,
                verifier_support_refs=effective_verifier_support_refs,
                payload_ref=effective_payload_ref,
            )

        if normalized == "merge" and target_id in self.nodes:
            existing = self.nodes[target_id]
            merged_from = set(existing.provenance.get("merged_from", []))
            merged_from.add(node.node_id)
            merged = (existing).model_copy(update={
                    "content": existing.content if len(existing.content) >= len(node.content) else node.content,
                    "symbol_set": sorted(set(existing.symbol_set) | set(node.symbol_set)),
                    "file_paths": sorted(set(existing.file_paths) | set(node.file_paths)),
                    "verifier_support": max(existing.verifier_support, node.verifier_support),
                    "timestamps": {**existing.timestamps, **node.timestamps},
                    "provenance": {**existing.provenance, "merged_from": sorted(merged_from)},
                }, deep=True)
            self.nodes[target_id] = merged
            record = self._write_record(
                action=normalized,
                target_node_id=target_id,
                source_task_id=effective_source_task_id or node.source_task_id,
                source_attempt_id=effective_source_attempt_id,
                source_checkpoint_ref=effective_source_checkpoint_ref,
                verifier_support_refs=effective_verifier_support_refs,
                prior_write_id=prior_write_id,
                payload_ref=effective_payload_ref,
            )
            self.nodes.setdefault(node.node_id, (node).model_copy(update={"tombstoned": True}, deep=True))
            self._edge_record(
                source_node_id=target_id,
                target_node_id=node.node_id,
                edge_type=LongTermEdgeType.DERIVED_FROM,
                introducing_write_id=record.write_id,
                written_at=record.written_at,
            )
            self._emit_conflict_if_needed(
                node,
                introducing_write=record,
                source_task_id=effective_source_task_id or node.source_task_id,
                source_attempt_id=effective_source_attempt_id,
                source_checkpoint_ref=effective_source_checkpoint_ref,
                verifier_support_refs=effective_verifier_support_refs,
                exclude_node_id=target_id,
            )
            return record

        if normalized == "refine" and target_id in self.nodes:
            existing = self.nodes[target_id]
            refined = (existing).model_copy(update={
                    "content": node.content,
                    "embedding": list(node.embedding),
                    "verifier_support": max(existing.verifier_support, node.verifier_support),
                    "timestamps": {**existing.timestamps, **node.timestamps},
                    "provenance": {**existing.provenance, **node.provenance, "refined_from_write_id": prior_write_id},
                }, deep=True)
            self.nodes[target_id] = refined
            record = self._write_record(
                action=normalized,
                target_node_id=target_id,
                source_task_id=effective_source_task_id or node.source_task_id,
                source_attempt_id=effective_source_attempt_id,
                source_checkpoint_ref=effective_source_checkpoint_ref,
                verifier_support_refs=effective_verifier_support_refs,
                prior_write_id=prior_write_id,
                payload_ref=effective_payload_ref,
            )
            self._edge_record(
                source_node_id=target_id,
                target_node_id=target_id,
                edge_type=LongTermEdgeType.REFINES,
                introducing_write_id=record.write_id,
                written_at=record.written_at,
            )
            self._emit_conflict_if_needed(
                refined,
                introducing_write=record,
                source_task_id=effective_source_task_id or node.source_task_id,
                source_attempt_id=effective_source_attempt_id,
                source_checkpoint_ref=effective_source_checkpoint_ref,
                verifier_support_refs=effective_verifier_support_refs,
                exclude_node_id=target_id,
            )
            return record

        stored = (node).model_copy(deep=True)
        self.nodes[stored.node_id] = stored
        record = self._write_record(
            action=normalized,
            target_node_id=stored.node_id,
            source_task_id=effective_source_task_id or stored.source_task_id,
            source_attempt_id=effective_source_attempt_id,
            source_checkpoint_ref=effective_source_checkpoint_ref,
            verifier_support_refs=effective_verifier_support_refs,
            prior_write_id=prior_write_id,
            payload_ref=effective_payload_ref,
        )
        self._emit_conflict_if_needed(
            stored,
            introducing_write=record,
            source_task_id=effective_source_task_id or stored.source_task_id,
            source_attempt_id=effective_source_attempt_id,
            source_checkpoint_ref=effective_source_checkpoint_ref,
            verifier_support_refs=effective_verifier_support_refs,
        )
        return record

    def upsert(self, node: MemoryNode, **lineage: Any) -> LongTermWriteRecord:
        return self.write(node, action=lineage.pop("action", None), **lineage)

    def tombstone(
        self,
        node_id: str,
        *,
        source_task_id: str | None = None,
        source_attempt_id: str = "",
        source_checkpoint_ref: str | None = None,
        verifier_support_refs: Sequence[str] = (),
        payload_ref: str | None = None,
    ) -> LongTermWriteRecord:
        write_context = self._active_write_context()
        source_task_id = source_task_id if source_task_id is not None else write_context.get("source_task_id")
        source_attempt_id = source_attempt_id or str(write_context.get("source_attempt_id") or "")
        source_checkpoint_ref = (
            source_checkpoint_ref
            if source_checkpoint_ref is not None
            else write_context.get("source_checkpoint_ref")
        )
        if not verifier_support_refs:
            verifier_support_refs = write_context.get("verifier_support_refs", ())
        payload_ref = payload_ref or write_context.get("payload_ref")
        node = self.nodes[node_id]
        prior_write_id = self._latest_write_by_node.get(node_id)
        self.nodes[node_id] = (node).model_copy(update={"tombstoned": True}, deep=True)
        record = self._write_record(
            action="tombstone",
            target_node_id=node_id,
            source_task_id=source_task_id or node.source_task_id,
            source_attempt_id=source_attempt_id,
            source_checkpoint_ref=source_checkpoint_ref,
            verifier_support_refs=verifier_support_refs,
            prior_write_id=prior_write_id,
            payload_ref=payload_ref,
        )
        self.edges = [
            (edge).model_copy(update={"tombstoned": True, "tombstone_write_id": record.write_id}, deep=True)
            if edge.source_node_id == node_id or edge.target_node_id == node_id
            else edge
            for edge in self.edges
        ]
        return record

    def all_nodes(self) -> list[MemoryNode]:
        return [(node).model_copy(deep=True) for node in self.nodes.values()]

    def snapshot(
        self,
        *,
        write_log_refs: Sequence[str] = (),
        diagnostic_refs: Sequence[str] = (),
    ) -> LongTermGraphSnapshot:
        return LongTermGraphSnapshot(
            nodes=self.all_nodes(),
            edges=[(edge).model_copy(deep=True) for edge in self.edges],
            write_records=[(record).model_copy(deep=True) for record in self.write_log],
            retrieval_diagnostics=[(record).model_copy(deep=True) for record in self.retrieval_diagnostics],
            write_log_refs=list(write_log_refs),
            diagnostic_refs=list(diagnostic_refs),
        )

    def restore(self, snapshot: Mapping[str, Any] | LongTermGraphSnapshot) -> None:
        graph_snapshot = (
            snapshot
            if isinstance(snapshot, LongTermGraphSnapshot)
            else (LongTermGraphSnapshot).model_validate(snapshot)
        )
        self.nodes = {node.node_id: (node).model_copy(deep=True) for node in graph_snapshot.nodes}
        self.edges = [(edge).model_copy(deep=True) for edge in graph_snapshot.edges]
        self.write_log = [(record).model_copy(deep=True) for record in graph_snapshot.write_records]
        self.retrieval_diagnostics = [
            (record).model_copy(deep=True)
            for record in graph_snapshot.retrieval_diagnostics
        ]
        self._latest_write_by_node = {}
        for record in self.write_log:
            if record.action != "conflict":
                self._latest_write_by_node[record.target_node_id] = record.write_id

    @classmethod
    def fork_from_snapshot(cls, snapshot: Mapping[str, Any] | LongTermGraphSnapshot) -> "LongTermGraph":
        graph = cls()
        graph.restore(snapshot)
        return graph

    def _query_node(self, query: str) -> MemoryNode:
        return (MemoryNode).model_construct(content=query, label=query, embedding=[], node_id="query", type="Query", symbol_set=[], file_paths=[], source_task_id="query", verifier_support=0.0, timestamps={}, provenance={}, tombstoned=False)

    def _neighbor_nodes(self, seeds: Sequence[MemoryNode]) -> list[MemoryNode]:
        if not seeds:
            return []
        seen = {node.node_id for node in seeds}
        expanded: list[tuple[float, MemoryNode]] = []
        for edge in self.edges:
            if edge.tombstoned or edge.edge_type not in {LongTermEdgeType.DERIVED_FROM.value, LongTermEdgeType.REFINES.value}:
                continue
            for seed in seeds:
                if seed.node_id not in {edge.source_node_id, edge.target_node_id}:
                    continue
                neighbor_id = edge.target_node_id if edge.source_node_id == seed.node_id else edge.source_node_id
                node = self.nodes.get(neighbor_id)
                if node is not None and not node.tombstoned and node.node_id not in seen:
                    expanded.append((1.0, node))
                    seen.add(node.node_id)
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

    def retrieve_candidates(
        self,
        query: str,
        exact_symbols: Sequence[str],
        file_paths: Sequence[str],
        top_embedding: int = 5,
        top_lexical: int = 5,
        *,
        task_id: str | None = None,
        seed: int | None = None,
        request_id: str | None = None,
        scope_id: str | None = None,
        emit_diagnostic: bool = True,
    ) -> list[MemoryNode]:
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
        synthesized_neighbor_ids = {node.node_id for node in one_hop}
        for node in exact_sorted + one_hop + embedding_sorted + lexical_sorted:
            if node.node_id in seen:
                continue
            merged.append(node)
            seen.add(node.node_id)
        if emit_diagnostic:
            self.record_retrieval_diagnostic(
                query,
                exact_symbols,
                file_paths,
                merged,
                task_id=task_id,
                seed=seed,
                request_id=request_id,
                scope_id=scope_id,
                synthesized_neighbor_ids=synthesized_neighbor_ids,
            )
        return merged

    def record_retrieval_diagnostic(
        self,
        query: str,
        exact_symbols: Sequence[str],
        file_paths: Sequence[str],
        returned_nodes: Sequence[MemoryNode],
        *,
        task_id: str | None = None,
        seed: int | None = None,
        request_id: str | None = None,
        scope_id: str | None = None,
        synthesized_neighbor_ids: Iterable[str] = (),
    ) -> RetrievalDiagnosticRecord:
        exact_symbol_set = set(exact_symbols)
        file_path_set = set(file_paths)
        synthesized = set(synthesized_neighbor_ids)
        query_node = self._query_node(query)
        seen_non_exact = False
        exact_first_preserved = True
        signals: list[RetrievalSignalRow] = []
        for rank, node in enumerate(returned_nodes):
            exact_file_path_hit = bool(file_path_set & set(node.file_paths))
            exact_symbol_hit = bool(exact_symbol_set & set(node.symbol_set))
            node_id_match = str(query).strip() == node.node_id or node.node_id in exact_symbol_set
            is_exact = exact_file_path_hit or exact_symbol_hit or node_id_match
            if not is_exact:
                seen_non_exact = True
            elif seen_non_exact:
                exact_first_preserved = False
            signals.append(
                RetrievalSignalRow(
                    node_id=node.node_id,
                    rank=rank,
                    exact_file_path_hit=exact_file_path_hit,
                    exact_symbol_hit=exact_symbol_hit,
                    node_id_match=node_id_match,
                    verifier_support_score=float(node.verifier_support),
                    lexical_overlap_score=lexical_overlap(query, node.content + " " + node.label),
                    embedding_similarity_score=cosine_similarity(node.embedding, query_node.embedding),
                    same_task_affinity_score=1.0 if task_id and node.source_task_id == task_id else 0.0,
                    synthesized_neighbor_expansion=node.node_id in synthesized,
                )
            )
        retrieved_at = now_ts()
        diagnostic = RetrievalDiagnosticRecord(
            diagnostic_id=f"retrieval.{stable_hash(query, [node.node_id for node in returned_nodes], retrieved_at)[:16]}",
            query_hash=stable_hash(query),
            task_id=task_id,
            seed=seed,
            request_id=request_id,
            scope_id=scope_id,
            returned_node_ids=[node.node_id for node in returned_nodes],
            signals=signals,
            exact_first_preserved=exact_first_preserved,
            retrieved_at=retrieved_at,
        )
        self.retrieval_diagnostics.append(diagnostic)
        return diagnostic

    def query_retrieval_diagnostics(
        self,
        *,
        diagnostic_id: str | None = None,
        query_hash: str | None = None,
    ) -> list[RetrievalDiagnosticRecord]:
        rows = self.retrieval_diagnostics
        if diagnostic_id:
            rows = [row for row in rows if row.diagnostic_id == diagnostic_id]
        if query_hash:
            rows = [row for row in rows if row.query_hash == query_hash]
        return [(row).model_copy(deep=True) for row in rows]
