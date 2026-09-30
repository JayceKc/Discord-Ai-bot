"""Day 24 固定公式的範圍、權重、穩定性與可解釋輸出測試。"""

import unittest

from services.quality_evaluator import QualityEvaluationError, evaluate_review
from services.quality_evaluator import PRIORITY_WEIGHTS


def review_with_scores(scores: dict[str, int]) -> dict[str, object]:
    return {
        "checklist": {
            key: {"score": score, "reason": f"{key} 固定測試理由"}
            for key, score in scores.items()
        }
    }


class QualityEvaluatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.review = review_with_scores({
            "completeness": 4,
            "creativity": 5,
            "credibility": 3,
            "feasibility": 2,
        })

    def test_growth_weights_produce_explainable_result(self) -> None:
        result = evaluate_review(self.review, "growth")

        self.assertEqual(result["formula_version"], "day24-v1")
        self.assertEqual(result["inputs"]["weights"], {
            "completeness": 0.30, "creativity": 0.25,
            "credibility": 0.15, "feasibility": 0.30,
        })
        self.assertEqual(result["outputs"], {
            "weighted_score": 3.5, "quality_index": 70.0, "grade": "需改善",
        })
        self.assertEqual(
            [item["dimension"] for item in result["risks"]],
            ["credibility", "feasibility"],
        )
        self.assertIn("3.5/5", result["explanation"])

    def test_priority_changes_only_fixed_weights_and_result(self) -> None:
        result = evaluate_review(self.review, "innovation")
        self.assertEqual(result["inputs"]["weights"]["creativity"], 0.45)
        self.assertEqual(result["outputs"]["weighted_score"], 3.9)
        self.assertEqual(result["outputs"]["quality_index"], 78.0)

    def test_same_input_returns_identical_output(self) -> None:
        self.assertEqual(
            evaluate_review(self.review, "cost_efficiency"),
            evaluate_review(self.review, "cost_efficiency"),
        )

    def test_score_outside_one_to_five_is_rejected(self) -> None:
        for invalid in (0, 6, True, 3.5):
            with self.subTest(invalid=invalid):
                review = review_with_scores({
                    "completeness": invalid,
                    "creativity": 4,
                    "credibility": 4,
                    "feasibility": 4,
                })
                with self.assertRaisesRegex(QualityEvaluationError, "1～5"):
                    evaluate_review(review, "growth")

    def test_unknown_priority_is_rejected(self) -> None:
        with self.assertRaisesRegex(QualityEvaluationError, "優先目標"):
            evaluate_review(self.review, "fastest")

    def test_every_priority_weight_set_sums_to_one(self) -> None:
        for priority, weights in PRIORITY_WEIGHTS.items():
            with self.subTest(priority=priority.value):
                self.assertEqual(set(weights), {
                    "completeness", "creativity", "credibility", "feasibility",
                })
                self.assertAlmostEqual(sum(weights.values()), 1.0)


if __name__ == "__main__":
    unittest.main()
