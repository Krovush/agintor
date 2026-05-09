from __future__ import annotations

from agintor.evaluation.pairwise_comparator import PairwiseArtifactComparator, decoded_winner


def test_pairwise_comparator_order_flip_decodes_to_same_winner() -> None:
    comparator = PairwiseArtifactComparator()

    verdict_ab = comparator.compare(axis_id="answer_actionability", artifact_a={"score": 0.4}, artifact_b={"score": 0.8})
    verdict_ba = comparator.compare(axis_id="answer_actionability", artifact_a={"score": 0.8}, artifact_b={"score": 0.4})

    assert decoded_winner(verdict_ab, artifact_a_id="parent", artifact_b_id="child") == "child"
    assert decoded_winner(verdict_ba, artifact_a_id="child", artifact_b_id="parent") == "child"
