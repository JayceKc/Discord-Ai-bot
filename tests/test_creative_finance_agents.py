import json  # 建立兩個 Agent 共用的專案脈絡與假的 JSON 回覆。
import unittest  # 執行非同步 Agent 單元測試。

from agents.creative_agent import CreativeAgent, CreativeAnalysis
from agents.finance_agent import FinanceAgent, FinanceAnalysis
from services.llm_service import LLMResponse, LLMUsage


def make_response(data: dict[str, object]) -> LLMResponse:
    """將完整假資料轉成模型會回傳的 JSON 文字。"""

    return LLMResponse(
        content=json.dumps(data, ensure_ascii=False),
        usage=LLMUsage(0.01, None, None, 30, 20),
    )


class FakeCreativeFinanceLLMService:
    """取代外部 Ollama，依 Agent 身分提供不同專業回覆。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def chat(self, message: str, **kwargs: object) -> LLMResponse:
        self.calls.append({"message": message, **kwargs})
        system_prompt = str(kwargs.get("system_prompt", ""))

        if "Creative Agent" in system_prompt:
            return make_response(
                {
                    "proposals": [
                        {
                            "title": "互動式 AI 客服導覽",
                            "description": "讓使用者選擇問題類別，再由 Qwen 提供回答。",
                            "research_basis": ["專案需要使用 Qwen 回答常見問題"],
                            "implementation_steps": [
                                "設計問題分類選單",
                                "將分類結果交給 Qwen",
                            ],
                        }
                    ]
                }
            )

        return make_response(
            {
                "cost_considerations": ["本機模型需要足夠的記憶體與運算資源"],
                "constraints": ["預算與可用硬體尚未確認"],
                "risks": ["模型回覆速度可能影響 Discord 使用體驗"],
                "alternatives": ["先使用較小模型完成 MVP"],
            }
        )


class CreativeFinanceAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_agents_produce_different_views_for_same_project(self) -> None:
        # 同一份脈絡包含原始需求與 Research Agent 的分類結果。
        project_context = json.dumps(
            {
                "project_requirement": "建立 AI 客服 Discord Bot",
                "research": {
                    "known_information": ["使用 Qwen 回答常見問題"],
                    "reasonable_inferences": ["需要設計對話流程"],
                    "items_to_verify": ["可用預算與硬體規格"],
                },
            },
            ensure_ascii=False,
        )
        fake_llm = FakeCreativeFinanceLLMService()
        creative_agent = CreativeAgent(fake_llm)
        finance_agent = FinanceAgent(fake_llm)

        creative_result = await creative_agent.respond(project_context)
        finance_result = await finance_agent.respond(project_context)

        # 真正被測試的是兩個 Agent 解析後的不同專業輸出。
        self.assertIsInstance(creative_result, CreativeAnalysis)
        self.assertEqual(
            creative_result.proposals[0].title,
            "互動式 AI 客服導覽",
        )
        self.assertIsInstance(finance_result, FinanceAnalysis)
        self.assertEqual(finance_result.alternatives, ["先使用較小模型完成 MVP"])

        # 兩個 Agent 收到完全相同的專案資料，但角色提示與生成參數不同。
        creative_call, finance_call = fake_llm.calls
        self.assertEqual(creative_call["message"], project_context)
        self.assertEqual(finance_call["message"], project_context)
        self.assertNotEqual(
            creative_call["system_prompt"],
            finance_call["system_prompt"],
        )
        self.assertEqual(creative_call["temperature"], 0.8)
        self.assertEqual(creative_call["seed"], 7)
        self.assertEqual(creative_call["max_output_tokens"], 700)
        self.assertEqual(finance_call["temperature"], 0.1)
        self.assertEqual(finance_call["seed"], 42)
        self.assertEqual(finance_call["max_output_tokens"], 500)
        self.assertNotEqual(
            creative_call["json_schema"],
            finance_call["json_schema"],
        )


if __name__ == "__main__":
    unittest.main()
