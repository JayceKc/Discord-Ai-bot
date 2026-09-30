import json  # 將測試資料轉成模型實際會回傳的 JSON 文字。
import unittest  # 使用 Python 內建的非同步單元測試工具。

from agents.base_agent import AgentError
from agents.pm_agent import PMAgent, PMAnalysis, PMProposalDraft
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

        if "Agent 名稱：PM Integration Agent" in system_prompt:
            data = {
                "title": "AI 專案整合提案",
                "summary": "整合兩輪討論後，採取分階段交付方案。",
                "sections": {
                    "background_and_goal": "建立可用的 AI 服務。",
                    "integrated_solution": "採分階段方式，先完成核心功能，再擴充進階功能。",
                    "execution_plan": "依照 MVP、驗證、擴充三階段執行。",
                    "risks_and_responses": "先用測試降低模型輸出不穩定的風險。",
                    "acceptance_criteria": "核心指令與錯誤處理皆可正常運作。",
                },
                "decisions": [
                    {
                        "topic": "交付範圍",
                        "decision": "折衷",
                        "reason": "兼顧時程與功能完整度。",
                        "sources": ["PM Agent 第 2 輪", "Finance Agent 第 2 輪"],
                    }
                ],
            }
        elif "Agent 名稱：PM Agent" in system_prompt:
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

    async def test_structured_agent_keeps_token_usage_metadata(self) -> None:
        """結構化解析後仍需保留 Ollama 回傳的 Token 統計。"""

        agent = PMAgent(FakeProjectLLMService())
        response = await agent.respond_with_metadata("建立企業網站")

        self.assertIsInstance(response.output, PMAnalysis)
        self.assertEqual(response.usage.prompt_tokens, 20)
        self.assertEqual(response.usage.completion_tokens, 10)
        self.assertEqual(response.max_output_tokens, 1000)

    async def test_pm_integrates_discussion_into_fixed_proposal_schema(self) -> None:
        """PM 整合流程必須產生固定章節與可追溯決策。"""

        fake_llm = FakeProjectLLMService()
        result = await PMAgent(fake_llm).integrate("兩輪摘要與重要原文")

        self.assertIsInstance(result, PMProposalDraft)
        self.assertEqual(result.decisions[0].decision, "折衷")
        self.assertIn("分階段", result.sections.integrated_solution)
        integration_call = fake_llm.calls[0]
        schema = integration_call["json_schema"]
        self.assertIn("sections", schema["properties"])
        self.assertIn("decisions", schema["properties"])

        agent = PMAgent(fake_llm)
        self.assertEqual(agent._proposal_agent.config.timeout_seconds, 180.0)

    async def test_pm_integration_keeps_draft_token_metadata(self) -> None:
        """整合草案也要回傳 Token 使用量與 1600 Token 上限。"""

        result = await PMAgent(FakeProjectLLMService()).integrate_with_metadata(
            "兩輪摘要與重要原文"
        )

        self.assertIsInstance(result.output, PMProposalDraft)
        self.assertEqual(result.usage.prompt_tokens, 20)
        self.assertEqual(result.usage.completion_tokens, 10)
        self.assertEqual(result.max_output_tokens, 1600)

    def test_pm_proposal_schema_fits_meeting_response_budget(self) -> None:
        """即使各欄位填滿，草案 JSON 仍應低於 4,000 字元。"""

        proposal = PMProposalDraft.model_validate(
            {
                "title": "題" * 100,
                "summary": "摘" * 300,
                "sections": {
                    "background_and_goal": "背" * 240,
                    "integrated_solution": "整" * 240,
                    "execution_plan": "執" * 240,
                    "risks_and_responses": "風" * 240,
                    "acceptance_criteria": "驗" * 240,
                },
                "decisions": [
                    {
                        "topic": "題" * 60,
                        "decision": "折衷",
                        "reason": "因" * 120,
                        "sources": ["來源" * 22, "來源" * 22, "來源" * 22],
                    }
                    for _ in range(4)
                ],
            }
        )

        encoded = json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False)
        self.assertLessEqual(len(encoded), 4000)

    async def test_research_agent_rejects_more_than_three_items_per_category(self) -> None:
        """Research 每類超過三項時，應由共用結構驗證流程拒絕。"""

        fake_llm = StaticFakeLLMService(
            json.dumps(
                {
                    "known_information": ["資訊一", "資訊二", "資訊三", "資訊四"],
                    "reasonable_inferences": [],
                    "items_to_verify": [],
                },
                ensure_ascii=False,
            )
        )
        agent = ResearchAgent(fake_llm)

        with self.assertRaisesRegex(AgentError, "Research Agent.*格式不正確"):
            await agent.respond("建立資料儀表板")

    def test_research_agent_prompt_requires_reference_to_pm(self) -> None:
        """Research 必須延續或查證 PM 的前置建議。"""

        agent = ResearchAgent(StaticFakeLLMService("{}"))

        self.assertIn("PM Agent", agent.config.system_prompt)
        self.assertIn("引用", agent.config.system_prompt)


if __name__ == "__main__":
    unittest.main()
