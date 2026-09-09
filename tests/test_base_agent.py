import unittest

from agents.base_agent import AgentConfig, BaseAgent
from services.llm_service import LLMResponse, LLMUsage


def make_response(content: str) -> LLMResponse:
    # 建立固定的 Fake 回覆，讓測試專注驗證 BaseAgent 的傳遞與解析行為。
    return LLMResponse(
        content=content,
        usage=LLMUsage(0.01, None, None, 10, 5),
    )


class FakeLLMService:
    # Fake 服務記錄每次呼叫，方便確認 Agent 傳入的設定是否正確。
    def __init__(self, response: LLMResponse):
        self.response = response
        self.calls = []

    async def chat(self, message: str, **kwargs):
        self.calls.append({"message": message, **kwargs})
        return self.response


class BaseAgentTest(unittest.IsolatedAsyncioTestCase):
    # Agent 應組合身份提示並傳入規格要求的預設模型選項。
    async def test_respond_passes_identity_prompt_and_default_options(self):
        fake_llm = FakeLLMService(make_response("需求已整理。"))
        agent = BaseAgent(
            AgentConfig(
                name="小企",
                role="專案經理",
                system_prompt="請整理使用者的專案需求。",
            ),
            fake_llm,
        )

        result = await agent.respond("建立 Discord Bot")

        self.assertEqual(result.content, "需求已整理。")
        self.assertIsNone(result.data)
        self.assertEqual(fake_llm.calls[0]["message"], "建立 Discord Bot")
        self.assertEqual(
            fake_llm.calls[0]["system_prompt"],
            "Agent 名稱：小企\n角色：專案經理\n\n請整理使用者的專案需求。",
        )
        self.assertEqual(fake_llm.calls[0]["temperature"], 0.2)
        self.assertEqual(fake_llm.calls[0]["seed"], 42)
        self.assertEqual(fake_llm.calls[0]["max_output_tokens"], 500)
        self.assertIsNone(fake_llm.calls[0]["json_schema"])

    # 設定 JSON Schema 時，Agent 應將模型文字解析成結構化資料。
    async def test_respond_parses_json_when_schema_is_configured(self):
        schema = {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        }
        fake_llm = FakeLLMService(make_response('{"summary":"建立 Bot"}'))
        agent = BaseAgent(
            AgentConfig("小企", "專案經理", "整理需求。", json_schema=schema),
            fake_llm,
        )

        result = await agent.respond("建立 Discord Bot")

        self.assertEqual(result.data, {"summary": "建立 Bot"})
        self.assertEqual(fake_llm.calls[0]["json_schema"], schema)


if __name__ == "__main__":
    unittest.main()
