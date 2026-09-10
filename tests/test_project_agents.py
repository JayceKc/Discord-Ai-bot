import json  # 將測試資料轉成模型實際會回傳的 JSON 文字。
import unittest  # 使用 Python 內建的非同步單元測試工具。

from agents.base_agent import AgentError
from agents.pm_agent import PMAgent, PMAnalysis
from agents.research_agent import ResearchAgent, ResearchAnalysis
from services.llm_service import LLMResponse, LLMUsage


def make_response(content: str) -> LLMResponse:
    """建立不會真的呼叫 Ollama 的固定模型回覆。"""

    return LLMResponse(
        content=content,
        usage=LLMUsage(0.01, None, None, 20, 10),
    )


class FakeProjectLLMService:
    """依 Agent 角色產生合法的假 JSON，並記錄兩個 Agent 的呼叫。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def chat(self, message: str, **kwargs: object) -> LLMResponse:
        self.calls.append({"message": message, **kwargs})
        system_prompt = str(kwargs.get("system_prompt", ""))

        if "PM Agent" in system_prompt:
            data = {
                "goal": f"完成專案需求：{message}",
                "constraints": ["需確認預算與期限"],
                "work_items": ["整理需求", "安排執行工作"],
                "disagreements": [],
            }
        else:
            data = {
                "known_information": [message],
                "reasonable_inferences": ["專案需要進一步拆解工作"],
                "items_to_verify": ["預算與期限是否確定"],
            }

        return make_response(json.dumps(data, ensure_ascii=False))


class StaticFakeLLMService:
    """固定回傳指定內容，用來測試 Pydantic 拒絕錯誤結構。"""

    def __init__(self, content: str) -> None:
        self.content = content

    async def chat(self, message: str, **kwargs: object) -> LLMResponse:
        return make_response(self.content)


class ProjectAgentTest(unittest.IsolatedAsyncioTestCase):
    async def _assert_agents_analyze(self, requirement: str) -> None:
        """確認相同介面能分析一種需求，且兩個 Agent 共用同一服務。"""

        fake_llm = FakeProjectLLMService()
        pm_agent = PMAgent(fake_llm)
        research_agent = ResearchAgent(fake_llm)

        pm_result = await pm_agent.respond(requirement)
        research_result = await research_agent.respond(requirement)

        self.assertIsInstance(pm_result, PMAnalysis)
        self.assertEqual(pm_result.goal, f"完成專案需求：{requirement}")
        self.assertEqual(pm_result.disagreements, [])
        self.assertIsInstance(research_result, ResearchAnalysis)
        self.assertEqual(research_result.known_information, [requirement])
        self.assertEqual(len(fake_llm.calls), 2)
        self.assertIs(pm_agent.llm_service, fake_llm)
        self.assertIs(research_agent.llm_service, fake_llm)

    async def test_agents_analyze_discord_bot_requirement(self) -> None:
        await self._assert_agents_analyze("建立可以回答問題的 Discord Bot")

    async def test_agents_analyze_company_website_requirement(self) -> None:
        await self._assert_agents_analyze("建立支援手機版的企業網站")

    async def test_agents_analyze_data_dashboard_requirement(self) -> None:
        await self._assert_agents_analyze("建立每日營收資料儀表板")

    async def test_pm_agent_rejects_invalid_structured_output(self) -> None:
        # 缺少 work_items 與 disagreements，應由 Pydantic 判定為不合法。
        fake_llm = StaticFakeLLMService(
            '{"goal":"建立網站","constraints":[]}'
        )
        agent = PMAgent(fake_llm)

        with self.assertRaisesRegex(AgentError, "PM Agent.*格式不正確"):
            await agent.respond("建立企業網站")


if __name__ == "__main__":
    unittest.main()
