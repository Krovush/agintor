from __future__ import annotations

from agintor.factory.goals import build_goal_spec
from agintor.integrations.tradingagents import tradingagents_seed_spec
from agintor.oracle.compiler import OracleCompiler


def test_trading_goal_selects_trading_outcome_family():
    package = OracleCompiler().compile(build_goal_spec("Build a stock trading agent optimizing alpha with risk limits."), tradingagents_seed_spec(symbols=["SPY"]))
    assert any(validator.family_id == "trading_outcome" for validator in package.validator_specs)
    assert any("trading" in claim.claim_id for claim in package.claim_graph.claims)
