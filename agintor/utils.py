from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import random
import statistics
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np


EPS = 1e-9


def stable_json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)



def stable_hash(*parts: Any) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            payload = part
        elif isinstance(part, Path):
            payload = str(part).encode("utf-8")
        elif isinstance(part, (dict, list, tuple, set)):
            payload = stable_json_dumps(part).encode("utf-8")
        else:
            payload = str(part).encode("utf-8")
        hasher.update(payload)
        hasher.update(b"\0")
    return hasher.hexdigest()



def seeded_rng(seed: int) -> random.Random:
    rng = random.Random()
    rng.seed(seed)
    return rng



def softmax(values: Sequence[float], beta: float = 1.0) -> list[float]:
    if not values:
        return []
    arr = np.array(values, dtype=float)
    arr = beta * (arr - np.max(arr))
    exp = np.exp(arr)
    denom = float(np.sum(exp))
    if denom <= 0:
        return [1.0 / len(values)] * len(values)
    return (exp / denom).tolist()



def clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))



def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)



def cosine_similarity(a: Sequence[float] | np.ndarray, b: Sequence[float] | np.ndarray) -> float:
    av = np.array(a, dtype=float)
    bv = np.array(b, dtype=float)
    an = float(np.linalg.norm(av))
    bn = float(np.linalg.norm(bv))
    if an <= EPS or bn <= EPS:
        return 0.0
    return float(np.dot(av, bv) / (an * bn))



def jaccard(a: Iterable[Any], b: Iterable[Any]) -> float:
    sa = set(a)
    sb = set(b)
    if not sa and not sb:
        return 1.0
    denom = len(sa | sb)
    return len(sa & sb) / denom if denom else 0.0



def lexical_tokens(text: str) -> set[str]:
    return {token.strip().lower() for token in text.replace("/", " ").replace("_", " ").split() if token.strip()}



def lexical_overlap(a: str, b: str) -> float:
    return jaccard(lexical_tokens(a), lexical_tokens(b))



def cheap_embedding(text: str, dim: int = 16) -> list[float]:
    tokens = sorted(lexical_tokens(text))
    vec = np.zeros(dim, dtype=float)
    for token in tokens:
        h = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
        for i in range(dim):
            bit = (h >> (i * 4)) & 0xF
            vec[i] += (bit / 15.0) - 0.5
    norm = float(np.linalg.norm(vec))
    if norm > EPS:
        vec /= norm
    return vec.tolist()



def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(statistics.median(values))



def mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(sum(values) / len(values))



def variance(values: Sequence[float]) -> float:
    if len(values) <= 1:
        return 0.0
    return float(statistics.pvariance(values))



def std_error(values: Sequence[float]) -> float:
    if len(values) <= 1:
        return 0.0
    return math.sqrt(variance(values) / len(values))



def lower_cvar(values: Sequence[float], alpha: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(1, math.ceil(alpha * len(ordered)))
    return mean(ordered[:k])



def safe_log1p_ratio(value: float, denom: float) -> float:
    denom = max(1.0, float(denom))
    return math.log1p(max(0.0, float(value)) / denom)



def count_tokens_rough(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))



def monotonic_isotonic_fit(scores: Sequence[float], labels: Sequence[float]) -> tuple[list[float], list[float]]:
    pairs = sorted(zip(scores, labels), key=lambda x: x[0])
    if not pairs:
        return [], []
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    blocks = [[x, x, y, 1] for x, y in zip(xs, ys)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][2] > blocks[i + 1][2]:
            total_w = blocks[i][3] + blocks[i + 1][3]
            avg = (blocks[i][2] * blocks[i][3] + blocks[i + 1][2] * blocks[i + 1][3]) / total_w
            merged = [blocks[i][0], blocks[i + 1][1], avg, total_w]
            blocks[i : i + 2] = [merged]
            i = max(0, i - 1)
        else:
            i += 1
    x_points = [b[1] for b in blocks]
    y_points = [b[2] for b in blocks]
    return x_points, y_points



def isotonic_predict(x_points: Sequence[float], y_points: Sequence[float], score: float) -> float:
    if not x_points:
        return clip(score, 0.0, 1.0)
    for x, y in zip(x_points, y_points):
        if score <= x:
            return float(y)
    return float(y_points[-1])



def normalize01(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if abs(hi - lo) <= EPS:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]



def ast_node_count(source: str) -> int:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    return sum(1 for _ in ast.walk(tree))



def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()



def changed_loc(original: str, updated: str) -> int:
    old_lines = original.splitlines()
    new_lines = updated.splitlines()
    total = 0
    for i in range(max(len(old_lines), len(new_lines))):
        old = old_lines[i] if i < len(old_lines) else None
        new = new_lines[i] if i < len(new_lines) else None
        if old != new:
            total += 1
    return total



def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path



def now_ts() -> float:
    return time.time()



def deterministic_choice(items: Sequence[Any], scores: Sequence[float], seed: int) -> Any:
    if len(items) != len(scores):
        raise ValueError("items and scores length mismatch")
    probs = softmax(scores)
    rng = seeded_rng(seed)
    draw = rng.random()
    cumulative = 0.0
    for item, prob in zip(items, probs):
        cumulative += prob
        if draw <= cumulative:
            return item
    return items[-1]



def unique_search_match(source: str, needle: str) -> int:
    first = source.find(needle)
    if first < 0:
        return -1
    second = source.find(needle, first + 1)
    if second >= 0:
        return -2
    return first



def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")



def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
