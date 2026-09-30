"""Day 24：以固定公式將 Review 四項評分轉成可解釋品質指標。"""

from __future__ import annotations

from collections.abc import Mapping

from models.workspace import ProjectPriority


class QualityEvaluationError(ValueError):
    """Review 評分或優先目標無法安全計算時使用。"""


DIMENSIONS = ("completeness", "creativity", "credibility", "feasibility")
DIMENSION_LABELS = {
    "completeness": "完整度",
    "creativity": "創意",
    "credibility": "可信度",
    "feasibility": "可行性",
}
PRIORITY_WEIGHTS: dict[ProjectPriority, dict[str, float]] = {
    ProjectPriority.GROWTH: {
        "completeness": 0.30,
        "creativity": 0.25,
        "credibility": 0.15,
        "feasibility": 0.30,
    },
    ProjectPriority.COST_EFFICIENCY: {
        "completeness": 0.20,
        "creativity": 0.10,
        "credibility": 0.25,
        "feasibility": 0.45,
    },
    ProjectPriority.BRAND_IMPACT: {
        "completeness": 0.25,
        "creativity": 0.20,
        "credibility": 0.40,
        "feasibility": 0.15,
    },
    ProjectPriority.INNOVATION: {
        "completeness": 0.20,
        "creativity": 0.45,
        "credibility": 0.15,
        "feasibility": 0.20,
    },
}
IMPROVEMENTS = {
    "completeness": "補齊需求、執行步驟、驗收條件與例外情境。",
    "creativity": "增加具體差異化做法，並說明如何小規模驗證。",
    "credibility": "補充資料來源、假設依據與待查證事項。",
    "feasibility": "補充資源、成本、時程、技術限制與風險對策。",
}


def evaluate_review(
    review: Mapping[str, object],
    priority_value: str | None,
) -> dict[str, object]:
    """驗證四項 1～5 分，套用固定權重並保留完整計算軌跡。"""

    try:
        priority = ProjectPriority(priority_value or ProjectPriority.GROWTH.value)
    except ValueError as error:
        raise QualityEvaluationError("專案優先目標不在允許範圍內。") from error
    checklist = review.get("checklist")
    if not isinstance(checklist, Mapping):
        raise QualityEvaluationError("Review checklist 必須是物件。")

    scores: dict[str, int] = {}
    reasons: dict[str, str] = {}
    for dimension in DIMENSIONS:
        item = checklist.get(dimension)
        if not isinstance(item, Mapping):
            raise QualityEvaluationError(f"Review 缺少{DIMENSION_LABELS[dimension]}評分。")
        score = item.get("score")
        reason = item.get("reason")
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise QualityEvaluationError(f"{DIMENSION_LABELS[dimension]}評分必須是 1～5 的整數。")
        if not isinstance(reason, str) or not reason.strip():
            raise QualityEvaluationError(f"{DIMENSION_LABELS[dimension]}必須提供理由。")
        scores[dimension] = score
        reasons[dimension] = reason.strip()

    weights = dict(PRIORITY_WEIGHTS[priority])
    weighted_score = round(sum(scores[key] * weights[key] for key in DIMENSIONS), 2)
    quality_index = round(weighted_score / 5 * 100, 1)
    risks = [
        {
            "dimension": key,
            "label": DIMENSION_LABELS[key],
            "level": "高" if scores[key] <= 2 else "中",
            "score": scores[key],
            "reason": reasons[key],
        }
        for key in DIMENSIONS
        if scores[key] < 4
    ]
    improvements = [
        {
            "dimension": key,
            "label": DIMENSION_LABELS[key],
            "suggestion": IMPROVEMENTS[key],
        }
        for key in DIMENSIONS
        if scores[key] < 4
    ]
    grade = (
        "優秀" if quality_index >= 90 else
        "良好" if quality_index >= 75 else
        "需改善" if quality_index >= 60 else
        "高風險"
    )
    return {
        "formula_version": "day24-v1",
        "evaluated_artifact": "reviewed_proposal_draft",
        "priority": {"value": priority.value, "label": priority.label},
        "formula": {
            "weighted_score": "sum(score[dimension] * weight[dimension])",
            "quality_index": "weighted_score / 5 * 100",
            "rounding": "weighted_score 取 2 位；quality_index 取 1 位",
        },
        "inputs": {
            "scores": scores,
            "reasons": reasons,
            "weights": weights,
        },
        "outputs": {
            "weighted_score": weighted_score,
            "quality_index": quality_index,
            "grade": grade,
        },
        "risks": risks,
        "improvements": improvements,
        "explanation": (
            f"依「{priority.label}」權重計算；四項分數乘各自權重後加總為 "
            f"{weighted_score}/5，換算品質指標 {quality_index}/100（{grade}）。"
        ),
    }
