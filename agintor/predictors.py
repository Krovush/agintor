from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Sequence

import numpy as np

from .utils import EPS, clip, isotonic_predict, monotonic_isotonic_fit, sigmoid


@dataclass
class BootstrapLogisticRegressor:
    weights: np.ndarray
    x_points: list[float] = field(default_factory=list)
    y_points: list[float] = field(default_factory=list)
    p_min: float = 0.02
    p_max: float = 0.98

    @classmethod
    def train(cls, xs: np.ndarray, ys: np.ndarray, steps: int = 300, lr: float = 0.1) -> "BootstrapLogisticRegressor":
        if xs.size == 0:
            return cls(weights=np.zeros(1, dtype=float))
        w = np.zeros(xs.shape[1], dtype=float)
        for _ in range(steps):
            logits = xs @ w
            probs = 1.0 / (1.0 + np.exp(-np.clip(logits, -30, 30)))
            grad = xs.T @ (probs - ys) / max(1, len(xs))
            w -= lr * grad
        raw_scores = [sigmoid(float(x @ w)) for x in xs]
        x_points, y_points = monotonic_isotonic_fit(raw_scores, ys.tolist())
        return cls(weights=w, x_points=x_points, y_points=y_points)

    def predict(self, x: Sequence[float]) -> float:
        w = self.weights
        xv = np.array(x, dtype=float)
        if xv.shape[0] != w.shape[0]:
            if xv.shape[0] < w.shape[0]:
                pad = np.zeros(w.shape[0] - xv.shape[0], dtype=float)
                xv = np.concatenate([xv, pad])
            else:
                xv = xv[: w.shape[0]]
        raw = sigmoid(float(xv @ w))
        calibrated = isotonic_predict(self.x_points, self.y_points, raw)
        return clip(calibrated, self.p_min, self.p_max)


@dataclass
class BootstrapLogLinearHuber:
    weights: np.ndarray

    @classmethod
    def train(cls, xs: np.ndarray, ys: np.ndarray, steps: int = 400, lr: float = 0.05, delta: float = 1.0) -> "BootstrapLogLinearHuber":
        if xs.size == 0:
            return cls(weights=np.zeros(1, dtype=float))
        ylog = np.log(np.clip(ys, EPS, None))
        w = np.zeros(xs.shape[1], dtype=float)
        for _ in range(steps):
            pred = xs @ w
            err = pred - ylog
            grad_term = np.where(np.abs(err) <= delta, err, delta * np.sign(err))
            grad = xs.T @ grad_term / max(1, len(xs))
            w -= lr * grad
        return cls(weights=w)

    def predict(self, x: Sequence[float]) -> float:
        w = self.weights
        xv = np.array(x, dtype=float)
        if xv.shape[0] != w.shape[0]:
            if xv.shape[0] < w.shape[0]:
                pad = np.zeros(w.shape[0] - xv.shape[0], dtype=float)
                xv = np.concatenate([xv, pad])
            else:
                xv = xv[: w.shape[0]]
        return float(math.exp(float(xv @ w)))


@dataclass
class RankingMixer:
    alpha: np.ndarray

    @classmethod
    def default(cls, dim: int) -> "RankingMixer":
        return cls(alpha=np.ones(dim, dtype=float) / max(1, dim))

    def score(self, normalized_features: Sequence[float]) -> float:
        xv = np.array(normalized_features, dtype=float)
        if xv.shape[0] != self.alpha.shape[0]:
            if xv.shape[0] < self.alpha.shape[0]:
                xv = np.concatenate([xv, np.zeros(self.alpha.shape[0] - xv.shape[0], dtype=float)])
            else:
                xv = xv[: self.alpha.shape[0]]
        return float(xv @ self.alpha)


@dataclass
class Ensemble:
    probability_models: List[BootstrapLogisticRegressor] = field(default_factory=list)
    positive_models: List[BootstrapLogLinearHuber] = field(default_factory=list)

    def prob_mean_std(self, x: Sequence[float]) -> tuple[float, float]:
        if not self.probability_models:
            return 0.5, 0.0
        preds = np.array([m.predict(x) for m in self.probability_models], dtype=float)
        return float(np.mean(preds)), float(np.std(preds))

    def pos_mean_std(self, x: Sequence[float]) -> tuple[float, float]:
        if not self.positive_models:
            return 1.0, 0.0
        preds = np.array([m.predict(x) for m in self.positive_models], dtype=float)
        return float(np.mean(preds)), float(np.std(preds))


class DecisionFamilyModelBank:
    def __init__(self, ensemble_size: int = 5) -> None:
        self.ensemble_size = ensemble_size
        self._observations: Dict[str, list[dict[str, object]]] = {}
        self._models: Dict[str, Ensemble] = {}
        self._ranking_weights: Dict[str, RankingMixer] = {}

    def add_observation(
        self,
        family: str,
        features: Sequence[float],
        probability_label: float | None = None,
        positive_label: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        self._observations.setdefault(family, []).append(
            {
                "x": list(map(float, features)),
                "p": None if probability_label is None else float(probability_label),
                "q": None if positive_label is None else float(positive_label),
                "metadata": dict(metadata or {}),
            }
        )

    def count(self, family: str) -> int:
        return len(self._observations.get(family, []))

    def train_family(self, family: str) -> None:
        obs = self._observations.get(family, [])
        if not obs:
            return
        xs = np.array([o["x"] for o in obs], dtype=float)
        ensemble = Ensemble()
        p_indices = [i for i, o in enumerate(obs) if o["p"] is not None]
        q_indices = [i for i, o in enumerate(obs) if o["q"] is not None]
        rng = np.random.default_rng(seed=stable_family_seed(family))
        if p_indices:
            p_xs = xs[p_indices]
            p_ys = np.array([obs[i]["p"] for i in p_indices], dtype=float)
            for _ in range(self.ensemble_size):
                draw = rng.integers(0, len(p_indices), len(p_indices))
                ensemble.probability_models.append(BootstrapLogisticRegressor.train(p_xs[draw], p_ys[draw]))
        if q_indices:
            q_xs = xs[q_indices]
            q_ys = np.array([obs[i]["q"] for i in q_indices], dtype=float)
            for _ in range(self.ensemble_size):
                draw = rng.integers(0, len(q_indices), len(q_indices))
                ensemble.positive_models.append(BootstrapLogLinearHuber.train(q_xs[draw], q_ys[draw]))
        self._models[family] = ensemble
        self._ranking_weights.setdefault(family, RankingMixer.default(xs.shape[1]))

    def maybe_retrain(self, fully_evaluated_children: int, accepted_elites: int) -> None:
        if fully_evaluated_children < 50 and accepted_elites < 10:
            return
        for family in list(self._observations):
            self.train_family(family)

    def predict_probability(self, family: str, features: Sequence[float]) -> tuple[float, float]:
        ensemble = self._models.get(family)
        if not ensemble:
            return 0.5, 0.0
        return ensemble.prob_mean_std(features)

    def predict_positive(self, family: str, features: Sequence[float]) -> tuple[float, float]:
        ensemble = self._models.get(family)
        if not ensemble:
            return 1.0, 0.0
        return ensemble.pos_mean_std(features)

    def ranking_score(self, family: str, normalized_features: Sequence[float]) -> float:
        mixer = self._ranking_weights.get(family)
        if mixer is None:
            mixer = RankingMixer.default(len(normalized_features))
            self._ranking_weights[family] = mixer
        return mixer.score(normalized_features)

    def utility(
        self,
        family: str,
        features: Sequence[float],
        token_ref: float = 1.0,
        latency_ref: float = 1.0,
        beta: float = 0.0,
        lambda_t: float = 0.05,
        lambda_l: float = 0.04,
        lambda_f: float = 0.10,
        lambda_q: float = 0.03,
        aux_value: float = 0.0,
    ) -> tuple[float, float, float]:
        p_mu, p_std = self.predict_probability(family, features)
        t_mu, t_std = self.predict_positive(family + ":token", features)
        l_mu, l_std = self.predict_positive(family + ":latency", features)
        f_mu, f_std = self.predict_probability(family + ":fault", features)
        utility = p_mu - lambda_t * math.log1p(t_mu / max(1.0, token_ref)) - lambda_l * math.log1p(l_mu / max(1.0, latency_ref)) - lambda_f * f_mu + lambda_q * aux_value
        sigma = math.sqrt(max(EPS, p_std**2 + t_std**2 + l_std**2 + f_std**2))
        conservative = utility - beta * sigma
        optimistic = utility + beta * sigma
        return float(utility), float(conservative), float(optimistic)



def stable_family_seed(name: str) -> int:
    value = 0
    for ch in name:
        value = (value * 131 + ord(ch)) % (2**31 - 1)
    return value or 17
