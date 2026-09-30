import json  # 建立 Review Agent 的輸入與假模型 JSON 回覆。
import unittest  # 執行非同步單元測試。

from agents.base_agent import AgentError
from agents.review_agent import ReviewAgent
from services.llm_service import LLMResponse, LLMUsage


def checklist(score: int) -> dict[str, object]:
    """建立包含完整度、創意、可信度與可行性的假檢查表。"""

    return {
        "completeness": {"score": score, "reason": "需求欄位檢查完成"},
        "creativity": {"score": score, "reason": "創意方案檢查完成"},
        "credibility": {"score": score, "reason": "研究依據檢查完成"},
        "feasibility": {"score": score, "reason": "成本與風險檢查完成"},
    }


class FakeReviewLLMService:
    """回傳指定審查結果，測試時不連線至 Ollama。"""

    def __init__(self, response_data: dict[str, object]) -> None:
        self.response_data = response_data
        self.calls: list[dict[str, object]] = []

    async def chat(self, message: str, **kwargs: object) -> LLMResponse:
        self.calls.append({"message": message, **kwargs})
        return LLMResponse(
            content=json.dumps(self.response_data, ensure_ascii=False),
            usage=LLMUsage(0.01, None, None, 40, 20),
        )


def review_request(draft: str, revision_count: int = 0) -> str:
    """建立 ReviewAgent.respond() 接收的專案草案 JSON。"""

    return json.dumps(
        {
            "project_id": "PRJ-002",
            "draft": draft,
            "revision_count": revision_count,
        },
        ensure_ascii=False,
    )


class ReviewAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_reasonable_draft_passes_all_checks(self) -> None:
        fake_llm = FakeReviewLLMService(
            {
                "status": "通過",
                "checklist": checklist(4),
                "issues": [],
                # 程式應依狀態固定為 False，不直接相信模型。
                "revision_allowed": True,
            }
        )
        agent = ReviewAgent(fake_llm)

        result = await agent.respond(
            review_request("需求、研究依據、方案、成本與風險皆已完整說明。")
        )

        self.assertEqual(result.status, "通過")
        self.assertEqual(result.checklist.completeness.score, 4)
        self.assertEqual(result.checklist.creativity.score, 4)
        self.assertEqual(result.checklist.credibility.score, 4)
        self.assertEqual(result.checklist.feasibility.score, 4)
        self.assertEqual(result.issues, [])
        self.assertFalse(result.revision_allowed)

    async def test_unreasonable_draft_lists_required_change_and_priority(self) -> None:
        failed_checklist = checklist(4)
        failed_checklist["credibility"] = {
            "score": 2,
            "reason": "草案沒有提供研究依據",
        }
        fake_llm = FakeReviewLLMService(
            {
                "status": "需要修改",
                "checklist": failed_checklist,
                "issues": [
                    {
                        "problem": "成本數字沒有資料來源",
                        "required_change": "補上報價來源或標記為待查證",
                        "priority": "高",
                        "assigned_agent": "Research Agent",
                    }
                ],
                # 初稿需要修改時，程式應固定允許一次修改。
                "revision_allowed": False,
            }
        )
        agent = ReviewAgent(fake_llm)

        result = await agent.respond(review_request("成本一定只要一千元。"))

        self.assertEqual(result.status, "需要修改")
        self.assertEqual(result.issues[0].problem, "成本數字沒有資料來源")
        self.assertEqual(
            result.issues[0].required_change,
            "補上報價來源或標記為待查證",
        )
        self.assertEqual(result.issues[0].priority, "高")
        self.assertEqual(result.issues[0].assigned_agent, "Research Agent")
        self.assertTrue(result.revision_allowed)

    async def test_second_review_cannot_request_another_revision(self) -> None:
        failed_checklist = checklist(4)
        failed_checklist["feasibility"] = {
            "score": 2,
            "reason": "仍未說明執行資源",
        }
        fake_llm = FakeReviewLLMService(
            {
                "status": "需要修改",
                "checklist": failed_checklist,
                "issues": [
                    {
                        "problem": "缺少硬體限制",
                        "required_change": "補充最低硬體規格",
                        "priority": "中",
                        "assigned_agent": "Finance Agent",
                    }
                ],
                "revision_allowed": True,
            }
        )
        agent = ReviewAgent(fake_llm)

        result = await agent.respond(
            review_request("第一次修改後的草案", revision_count=1)
        )

        self.assertEqual(result.status, "需要修改")
        self.assertFalse(result.revision_allowed)

    async def test_more_than_one_revision_is_rejected_before_llm_call(self) -> None:
        fake_llm = FakeReviewLLMService(
            {
                "status": "通過",
                "checklist": checklist(4),
                "issues": [],
                "revision_allowed": False,
            }
        )
        agent = ReviewAgent(fake_llm)

        with self.assertRaisesRegex(AgentError, "修改次數只能是 0 或 1"):
            await agent.respond(review_request("第二次修改", revision_count=2))

        self.assertEqual(fake_llm.calls, [])

    async def test_review_score_outside_one_to_five_is_rejected(self) -> None:
        invalid = checklist(4)
        invalid["creativity"] = {"score": 6, "reason": "超出範圍"}
        fake_llm = FakeReviewLLMService({
            "status": "通過",
            "checklist": invalid,
            "issues": [],
            "revision_allowed": False,
        })
        agent = ReviewAgent(fake_llm)

        with self.assertRaisesRegex(AgentError, "回覆格式不正確"):
            await agent.respond(review_request("格式完整的草案"))


if __name__ == "__main__":
    unittest.main()
