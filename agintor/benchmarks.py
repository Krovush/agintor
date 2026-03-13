from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping

from pydantic import BaseModel

from .pydantic_compat import model_copy, model_validate
from .schemas import BenchmarkTask, OperationSpec


@dataclass(frozen=True)
class BenchmarkSuite:
    name: str
    train: list[BenchmarkTask]
    val: list[BenchmarkTask]
    test: list[BenchmarkTask]
    proxy: list[BenchmarkTask]

    def all_tasks(self, partition: str = "train") -> list[BenchmarkTask]:
        return [model_copy(task, deep=True) for task in getattr(self, partition)]

    def task_family_map(self, partition: str) -> dict[str, str]:
        return {task.task_id: task.family for task in self.all_tasks(partition)}

    def by_id(self, task_id: str) -> BenchmarkTask:
        for partition in ("train", "val", "test", "proxy"):
            for task in getattr(self, partition):
                if task.task_id == task_id:
                    return model_copy(task, deep=True)
        raise KeyError(task_id)

    def representative_family_tasks(self, family: str, partition: str = "train", limit: int = 4) -> list[BenchmarkTask]:
        return [task for task in self.all_tasks(partition) if task.family == family][:limit]



def build_demo_suite() -> BenchmarkSuite:
    top_train = [
        BenchmarkTask(
            task_id="top.sum_product",
            family="top",
            task_type="structured_ops",
            prompt="Given the numbers [2, 3, 5], compute the sum and product and return JSON with keys sum and product.",
            symbolic_seeds=["numbers", "sum", "product"],
            operations=[
                OperationSpec(op_id="sum", kind="builtin", output_key="sum", description="Compute sum of numbers", tool_hint="math/basic/sum_numbers", args={"numbers": [2, 3, 5]}),
                OperationSpec(op_id="product", kind="builtin", output_key="product", description="Compute product of numbers", tool_hint="math/basic/product_numbers", args={"numbers": [2, 3, 5]}),
            ],
            expected={"sum": 10, "product": 30},
            verifier_type="json_exact",
            proxy_scope_tags=["top", "tool", "ctl"],
        ),
        BenchmarkTask(
            task_id="top.extrema_median",
            family="top",
            task_type="structured_ops",
            prompt="Given the numbers [4, 9, 1, 7, 3], return JSON with max and median.",
            symbolic_seeds=["numbers", "max", "median"],
            operations=[
                OperationSpec(op_id="max", kind="builtin", output_key="max", description="Compute max number", tool_hint="math/basic/max_number", args={"numbers": [4, 9, 1, 7, 3]}),
                OperationSpec(op_id="median", kind="builtin", output_key="median", description="Compute median", tool_hint="math/basic/median_number", args={"numbers": [4, 9, 1, 7, 3]}),
            ],
            expected={"max": 9, "median": 4},
            verifier_type="json_exact",
            proxy_scope_tags=["top", "tool", "ctl"],
        ),
    ]

    mem_train = [
        BenchmarkTask(
            task_id="mem.symbol_lookup",
            family="mem",
            task_type="memory_query",
            prompt="Answer the query using the exact symbol mapping. What value does ALPHA_7 map to?",
            symbolic_seeds=["ALPHA_7"],
            context_items=[
                {"symbol": "ALPHA_1", "value": 3},
                {"symbol": "ALPHA_7", "value": 17},
                {"symbol": "ALPHA_2", "value": 5},
                {"symbol": "ALPHA_9", "value": 21},
            ],
            operations=[
                OperationSpec(op_id="lookup", kind="memory_lookup", output_key="answer", description="Lookup exact symbol value", requires_exact_symbol="ALPHA_7"),
            ],
            expected="17",
            verifier_type="string_exact",
            proxy_scope_tags=["mem", "ctl"],
        ),
        BenchmarkTask(
            task_id="mem.path_lookup",
            family="mem",
            task_type="memory_query",
            prompt="Find the owner of the file path /srv/app/config.yaml.",
            file_paths=["/srv/app/config.yaml"],
            context_items=[
                {"file_path": "/srv/app/main.py", "owner": "api"},
                {"file_path": "/srv/app/config.yaml", "owner": "platform"},
                {"file_path": "/srv/app/db.py", "owner": "data"},
            ],
            operations=[
                OperationSpec(op_id="owner", kind="memory_lookup", output_key="answer", description="Lookup exact file path owner"),
            ],
            expected="platform",
            verifier_type="string_exact",
            proxy_scope_tags=["mem", "ctl"],
        ),
    ]

    tool_train = [
        BenchmarkTask(
            task_id="tool.generated_sum_squares_mod",
            family="tool",
            task_type="tool_expression",
            prompt="Return sum of squares of [1,2,3,4] modulo 7.",
            symbolic_seeds=["numbers", "modulus"],
            operations=[
                OperationSpec(
                    op_id="expr",
                    kind="generated_expression",
                    output_key="value",
                    description="Create or reuse a tool that computes sum(x*x for x in numbers) % modulus",
                    expression="sum(x*x for x in numbers) % modulus",
                    args={"numbers": [1, 2, 3, 4], "modulus": 7},
                    externally_visible=True,
                )
            ],
            expected=2,
            verifier_type="number_exact",
            proxy_scope_tags=["tool", "ctl"],
        ),
        BenchmarkTask(
            task_id="tool.csv_stats",
            family="tool",
            task_type="structured_ops",
            prompt="Given the rows, return JSON with column total sales and column max region_id.",
            operations=[
                OperationSpec(op_id="total_sales", kind="builtin", output_key="total_sales", description="Sum column sales", tool_hint="data/csv/column_sum", args={"rows": [{"sales": 5, "region_id": 1}, {"sales": 8, "region_id": 3}], "column": "sales"}),
                OperationSpec(op_id="max_region", kind="builtin", output_key="max_region", description="Max column region_id", tool_hint="data/csv/column_max", args={"rows": [{"sales": 5, "region_id": 1}, {"sales": 8, "region_id": 3}], "column": "region_id"}),
            ],
            expected={"total_sales": 13, "max_region": 3},
            verifier_type="json_exact",
            proxy_scope_tags=["tool", "top", "ctl"],
        ),
    ]

    e2e_train = [
        BenchmarkTask(
            task_id="e2e.revenue_report",
            family="e2e",
            task_type="e2e_report",
            prompt="Using the exact fee symbol FEE_RATE, the transaction rows, and the expression for net, produce JSON with gross_total, fee_rate, net_total.",
            symbolic_seeds=["FEE_RATE"],
            context_items=[
                {"symbol": "FEE_RATE", "value": 0.10},
                {"rows": [{"amount": 10}, {"amount": 25}, {"amount": 5}]},
            ],
            operations=[
                OperationSpec(op_id="gross_total", kind="builtin", output_key="gross_total", description="Sum transaction amounts", tool_hint="data/csv/column_sum", args={"rows": [{"amount": 10}, {"amount": 25}, {"amount": 5}], "column": "amount"}),
                OperationSpec(op_id="fee_rate", kind="memory_lookup", output_key="fee_rate", description="Lookup exact fee rate", requires_exact_symbol="FEE_RATE"),
                OperationSpec(op_id="net_total", kind="generated_expression", output_key="net_total", description="Compute gross_total * (1 - fee_rate)", expression="gross_total * (1 - fee_rate)", args={}, dependencies=["gross_total", "fee_rate"]),
            ],
            expected={"gross_total": 40, "fee_rate": 0.10, "net_total": 36.0},
            verifier_type="json_numeric",
            proxy_scope_tags=["top", "mem", "tool", "ctl"],
        ),
        BenchmarkTask(
            task_id="e2e.inventory_mix",
            family="e2e",
            task_type="e2e_report",
            prompt="Use the exact symbol SCALE and the item rows to produce JSON with total_weight, scale, adjusted_weight.",
            symbolic_seeds=["SCALE"],
            context_items=[
                {"symbol": "SCALE", "value": 3},
                {"rows": [{"weight": 2}, {"weight": 4}, {"weight": 6}]},
            ],
            operations=[
                OperationSpec(op_id="total_weight", kind="builtin", output_key="total_weight", description="Sum item weights", tool_hint="data/csv/column_sum", args={"rows": [{"weight": 2}, {"weight": 4}, {"weight": 6}], "column": "weight"}),
                OperationSpec(op_id="scale", kind="memory_lookup", output_key="scale", description="Lookup exact scale", requires_exact_symbol="SCALE"),
                OperationSpec(op_id="adjusted_weight", kind="generated_expression", output_key="adjusted_weight", description="Compute total_weight * scale", expression="total_weight * scale", args={}, dependencies=["total_weight", "scale"]),
            ],
            expected={"total_weight": 12, "scale": 3, "adjusted_weight": 36},
            verifier_type="json_numeric",
            proxy_scope_tags=["top", "mem", "tool", "ctl"],
        ),
    ]

    val = [
        BenchmarkTask(
            task_id="val.top.range",
            family="top",
            task_type="structured_ops",
            prompt="Given [5, 2, 11], return JSON with min and max.",
            operations=[
                OperationSpec(op_id="min", kind="builtin", output_key="min", description="Compute min", tool_hint="math/basic/min_number", args={"numbers": [5, 2, 11]}),
                OperationSpec(op_id="max", kind="builtin", output_key="max", description="Compute max", tool_hint="math/basic/max_number", args={"numbers": [5, 2, 11]}),
            ],
            expected={"min": 2, "max": 11},
            verifier_type="json_exact",
        ),
        BenchmarkTask(
            task_id="val.mem.symbol",
            family="mem",
            task_type="memory_query",
            prompt="What value does BETA_4 map to?",
            symbolic_seeds=["BETA_4"],
            context_items=[{"symbol": "BETA_4", "value": 23}],
            operations=[OperationSpec(op_id="lookup", kind="memory_lookup", output_key="answer", description="Lookup exact symbol", requires_exact_symbol="BETA_4")],
            expected="23",
            verifier_type="string_exact",
        ),
        BenchmarkTask(
            task_id="val.tool.expression",
            family="tool",
            task_type="tool_expression",
            prompt="Return (2+3+4)^2 using a generated expression tool.",
            operations=[OperationSpec(op_id="expr", kind="generated_expression", output_key="value", description="Compute expression", expression="(a+b+c)**2", args={"a": 2, "b": 3, "c": 4})],
            expected=81,
            verifier_type="number_exact",
        ),
        BenchmarkTask(
            task_id="val.e2e.bundle",
            family="e2e",
            task_type="e2e_report",
            prompt="Use the exact symbol RATE and rows to produce total, rate, adjusted.",
            symbolic_seeds=["RATE"],
            context_items=[{"symbol": "RATE", "value": 2}, {"rows": [{"amount": 2}, {"amount": 6}]}],
            operations=[
                OperationSpec(op_id="total", kind="builtin", output_key="total", description="Sum amounts", tool_hint="data/csv/column_sum", args={"rows": [{"amount": 2}, {"amount": 6}], "column": "amount"}),
                OperationSpec(op_id="rate", kind="memory_lookup", output_key="rate", description="Lookup rate", requires_exact_symbol="RATE"),
                OperationSpec(op_id="adjusted", kind="generated_expression", output_key="adjusted", description="Adjust total", expression="total * rate", args={}, dependencies=["total", "rate"]),
            ],
            expected={"total": 8, "rate": 2, "adjusted": 16},
            verifier_type="json_numeric",
        ),
    ]

    test = [
        BenchmarkTask(
            task_id="test.top.aggregate",
            family="top",
            task_type="structured_ops",
            prompt="Given [1, 1, 2, 3], return JSON with sum and median.",
            operations=[
                OperationSpec(op_id="sum", kind="builtin", output_key="sum", description="Compute sum", tool_hint="math/basic/sum_numbers", args={"numbers": [1, 1, 2, 3]}),
                OperationSpec(op_id="median", kind="builtin", output_key="median", description="Compute median", tool_hint="math/basic/median_number", args={"numbers": [1, 1, 2, 3]}),
            ],
            expected={"sum": 7, "median": 1.5},
            verifier_type="json_numeric",
        ),
        BenchmarkTask(
            task_id="test.mem.file_owner",
            family="mem",
            task_type="memory_query",
            prompt="Find owner of /opt/service/run.sh.",
            file_paths=["/opt/service/run.sh"],
            context_items=[{"file_path": "/opt/service/run.sh", "owner": "ops"}],
            operations=[OperationSpec(op_id="owner", kind="memory_lookup", output_key="answer", description="Lookup file owner")],
            expected="ops",
            verifier_type="string_exact",
        ),
        BenchmarkTask(
            task_id="test.tool.generated",
            family="tool",
            task_type="tool_expression",
            prompt="Compute max(a,b,c)-min(a,b,c) for 9,2,7.",
            operations=[OperationSpec(op_id="expr", kind="generated_expression", output_key="value", description="Compute range", expression="max(a,b,c)-min(a,b,c)", args={"a": 9, "b": 2, "c": 7})],
            expected=7,
            verifier_type="number_exact",
        ),
        BenchmarkTask(
            task_id="test.e2e.scaled_report",
            family="e2e",
            task_type="e2e_report",
            prompt="Use symbol MULT and rows to compute total, mult, adjusted.",
            context_items=[{"symbol": "MULT", "value": 4}, {"rows": [{"amount": 3}, {"amount": 7}]}],
            operations=[
                OperationSpec(op_id="total", kind="builtin", output_key="total", description="Sum amounts", tool_hint="data/csv/column_sum", args={"rows": [{"amount": 3}, {"amount": 7}], "column": "amount"}),
                OperationSpec(op_id="mult", kind="memory_lookup", output_key="mult", description="Lookup multiplier", requires_exact_symbol="MULT"),
                OperationSpec(op_id="adjusted", kind="generated_expression", output_key="adjusted", description="Adjust total", expression="total * mult", args={}, dependencies=["total", "mult"]),
            ],
            expected={"total": 10, "mult": 4, "adjusted": 40},
            verifier_type="json_numeric",
        ),
    ]

    proxy = [
        top_train[0],
        mem_train[0],
        tool_train[0],
        e2e_train[0],
        BenchmarkTask(
            task_id="proxy.top.checkpoint_trace",
            family="top",
            task_type="trace_proxy",
            prompt="Verify that multi-child vertical execution emits checkpoint summaries.",
            operations=[
                OperationSpec(op_id="sum", kind="builtin", output_key="sum", description="Compute sum of numbers", tool_hint="math/basic/sum_numbers", args={"numbers": [2, 3, 5]}),
                OperationSpec(op_id="product", kind="builtin", output_key="product", description="Compute product of numbers", tool_hint="math/basic/product_numbers", args={"numbers": [2, 3, 5]}),
            ],
            expected={"event": "child_complete", "min": 1},
            verifier_type="trace_event_count",
            proxy_scope_tags=["top", "ctl"],
        ),
        BenchmarkTask(
            task_id="proxy.tool.generated_trace",
            family="tool",
            task_type="trace_proxy",
            prompt="Verify that generated-tool paths emit tool-operation traces.",
            operations=[
                OperationSpec(
                    op_id="expr",
                    kind="generated_expression",
                    output_key="value",
                    description="Create or reuse a tool that computes sum(x*x for x in numbers) % modulus",
                    expression="sum(x*x for x in numbers) % modulus",
                    args={"numbers": [1, 2, 3, 4], "modulus": 7},
                )
            ],
            expected=["tool_operation", "checks_requested"],
            verifier_type="trace_event",
            proxy_scope_tags=["tool", "ctl"],
        ),
        BenchmarkTask(
            task_id="proxy.tool.provider_synthesis",
            family="tool",
            task_type="trace_proxy",
            prompt="Verify that under-specified tool requests use provider-backed synthesis and still dispatch a tool deterministically.",
            operations=[
                OperationSpec(
                    op_id="expr",
                    kind="generated_expression",
                    output_key="value",
                    description="Synthesize a deterministic tool for this operation without an explicit expression.",
                    args={"value": 7},
                )
            ],
            expected=7,
            verifier_type="number_exact",
            proxy_scope_tags=["tool", "ctl"],
        ),
        BenchmarkTask(
            task_id="proxy.mem.compaction_trace",
            family="mem",
            task_type="trace_proxy",
            prompt="Verify that oversized context triggers compaction while exact symbol retrieval still succeeds.",
            symbolic_seeds=["ALPHA_7"],
            context_items=[
                {"symbol": "ALPHA_7", "value": 17},
                *[
                    {
                        "note": f"supporting-context-{idx}",
                        "text": ("audit trail and raw evidence block " * 24).strip(),
                    }
                    for idx in range(10)
                ],
            ],
            operations=[
                OperationSpec(op_id="lookup", kind="memory_lookup", output_key="answer", description="Lookup exact symbol value", requires_exact_symbol="ALPHA_7"),
            ],
            expected="compaction",
            verifier_type="trace_event",
            proxy_scope_tags=["mem", "ctl"],
        ),
    ]
    return BenchmarkSuite(name="demo", train=top_train + mem_train + tool_train + e2e_train, val=val, test=test, proxy=proxy)
def load_suite(name_or_path: str) -> BenchmarkSuite:
    if name_or_path == "demo":
        return build_demo_suite()
    path = Path(name_or_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return BenchmarkSuite(
        name=data["name"],
        train=[model_validate(BenchmarkTask, item) for item in data["train"]],
        val=[model_validate(BenchmarkTask, item) for item in data["val"]],
        test=[model_validate(BenchmarkTask, item) for item in data["test"]],
        proxy=[model_validate(BenchmarkTask, item) for item in data["proxy"]],
    )
