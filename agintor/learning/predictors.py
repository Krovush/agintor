from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence

import numpy as np
from ..contracts import (
    PredictorEnsembleSnapshot,
    PredictorLogLinearHuberSnapshot,
    PredictorLogisticRegressorSnapshot,
    PredictorRankingMixerSnapshot,
    PredictorSnapshot,
)
from ..utils import EPS, clip, isotonic_predict, monotonic_isotonic_fit, sigmoid


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


def _snapshot_logistic_model(model: BootstrapLogisticRegressor) -> PredictorLogisticRegressorSnapshot:
    return PredictorLogisticRegressorSnapshot(
        weights=model.weights.astype(float).tolist(),
        x_points=[float(value) for value in model.x_points],
        y_points=[float(value) for value in model.y_points],
        p_min=float(model.p_min),
        p_max=float(model.p_max),
    )


def _restore_logistic_model(snapshot: PredictorLogisticRegressorSnapshot) -> BootstrapLogisticRegressor:
    return BootstrapLogisticRegressor(
        weights=np.array(snapshot.weights, dtype=float),
        x_points=[float(value) for value in snapshot.x_points],
        y_points=[float(value) for value in snapshot.y_points],
        p_min=float(snapshot.p_min),
        p_max=float(snapshot.p_max),
    )


def _snapshot_huber_model(model: BootstrapLogLinearHuber) -> PredictorLogLinearHuberSnapshot:
    return PredictorLogLinearHuberSnapshot(weights=model.weights.astype(float).tolist())


def _restore_huber_model(snapshot: PredictorLogLinearHuberSnapshot) -> BootstrapLogLinearHuber:
    return BootstrapLogLinearHuber(weights=np.array(snapshot.weights, dtype=float))


def _snapshot_ensemble(ensemble: Ensemble) -> PredictorEnsembleSnapshot:
    return PredictorEnsembleSnapshot(
        probability_models=[
            _snapshot_logistic_model(model)
            for model in ensemble.probability_models
        ],
        positive_models=[
            _snapshot_huber_model(model)
            for model in ensemble.positive_models
        ],
    )


def _restore_ensemble(snapshot: PredictorEnsembleSnapshot) -> Ensemble:
    return Ensemble(
        probability_models=[
            _restore_logistic_model(model)
            for model in snapshot.probability_models
        ],
        positive_models=[
            _restore_huber_model(model)
            for model in snapshot.positive_models
        ],
    )


def _normalize_observation(payload: Mapping[str, Any]) -> dict[str, object]:
    return {
        "x": [float(value) for value in payload.get("x", [])],
        "p": None if payload.get("p") is None else float(payload["p"]),
        "q": None if payload.get("q") is None else float(payload["q"]),
        "metadata": dict(payload.get("metadata", {})),
    }


class DecisionFamilyModelBank:
    def __init__(self, ensemble_size: int = 5, max_observations_per_family: int = 200) -> None:
        self.ensemble_size = ensemble_size
        self.max_observations_per_family = max_observations_per_family
        self._observations: Dict[str, list[dict[str, object]]] = {}
        self._models: Dict[str, Ensemble] = {}
        self._ranking_weights: Dict[str, RankingMixer] = {}
        self._frozen = False

    def freeze(self) -> None:
        self._frozen = True

    def unfreeze(self) -> None:
        self._frozen = False

    def snapshot(self) -> PredictorSnapshot:
        return PredictorSnapshot(
            ensemble_size=int(self.ensemble_size),
            max_observations_per_family=int(self.max_observations_per_family),
            frozen=bool(self._frozen),
            observations={
                str(family): [
                    _normalize_observation(observation)
                    for observation in observations
                ]
                for family, observations in self._observations.items()
            },
            models={
                str(family): _snapshot_ensemble(model)
                for family, model in self._models.items()
            },
            ranking_weights={
                str(family): PredictorRankingMixerSnapshot(alpha=mixer.alpha.astype(float).tolist())
                for family, mixer in self._ranking_weights.items()
            },
        )

    def restore(self, snapshot: Mapping[str, Any] | PredictorSnapshot) -> None:
        predictor_snapshot = (
            snapshot
            if isinstance(snapshot, PredictorSnapshot)
            else (PredictorSnapshot).model_validate(snapshot)
        )
        self.ensemble_size = int(predictor_snapshot.ensemble_size)
        self.max_observations_per_family = int(predictor_snapshot.max_observations_per_family)
        self._frozen = bool(predictor_snapshot.frozen)
        self._observations = {
            str(family): [
                _normalize_observation(observation)
                for observation in observations
            ][-self.max_observations_per_family :]
            for family, observations in predictor_snapshot.observations.items()
        }
        self._models = {
            str(family): _restore_ensemble(model_snapshot)
            for family, model_snapshot in predictor_snapshot.models.items()
        }
        self._ranking_weights = {
            str(family): RankingMixer(alpha=np.array(weight_snapshot.alpha, dtype=float))
            for family, weight_snapshot in predictor_snapshot.ranking_weights.items()
        }

    @classmethod
    def fork_from_snapshot(cls, snapshot: Mapping[str, Any] | PredictorSnapshot) -> "DecisionFamilyModelBank":
        predictor_snapshot = (
            snapshot
            if isinstance(snapshot, PredictorSnapshot)
            else (PredictorSnapshot).model_validate(snapshot)
        )
        bank = cls(
            ensemble_size=int(predictor_snapshot.ensemble_size),
            max_observations_per_family=int(predictor_snapshot.max_observations_per_family),
        )
        bank.restore(predictor_snapshot)
        return bank

    def add_observation(
        self,
        family: str,
        features: Sequence[float],
        probability_label: float | None = None,
        positive_label: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        if self._frozen:
            return
        bucket = self._observations.setdefault(family, [])
        bucket.append(
            {
                "x": list(map(float, features)),
                "p": None if probability_label is None else float(probability_label),
                "q": None if positive_label is None else float(positive_label),
                "metadata": dict(metadata or {}),
            }
        )
        if len(bucket) > self.max_observations_per_family:
            del bucket[: len(bucket) - self.max_observations_per_family]

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
        if self._frozen:
            return
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

    def summary(self) -> dict[str, object]:
        return {
            "frozen": self._frozen,
            "families": {
                family: {
                    "observations": len(observations),
                    "trained": family in self._models,
                }
                for family, observations in sorted(self._observations.items())
            },
        }



def stable_family_seed(name: str) -> int:
    value = 0
    for ch in name:
        value = (value * 131 + ord(ch)) % (2**31 - 1)
    return value or 17
