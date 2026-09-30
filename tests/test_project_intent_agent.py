"""自然語言專案意圖分類 Agent。"""

import unittest

from pydantic import ValidationError

from agents.project_intent_agent import ProjectIntent, ProjectIntentAgent
from services.llm_service import LLMResponse, LLMUsage


class FakeLLMService:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def chat(self, message: str, **kwargs: object) -> LLMResponse:
        self.calls.append((message, kwargs))
        return LLMResponse(
            self.content,
            LLMUsage(0.01, None, None, 10, 8),
        )


class ProjectIntentModelTest(unittest.TestCase):
    def test_create_project_requires_title(self):
        with self.assertRaises(ValidationError):
            ProjectIntent(
                intent="create_project",
                title=None,
                confidence=0.9,
                reason="明確建立要求",
            )

    def test_non_project_rejects_title(self):
        with self.assertRaises(ValidationError):
            ProjectIntent(
                intent="not_project",
                title="不應存在",
                confidence=0.9,
                reason="一般聊天",
            )


class ProjectIntentAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_classifies_with_structured_schema(self):
        llm = FakeLLMService(
            '{"intent":"create_project","title":"咖啡廳",'
            '"confidence":0.96,"reason":"明確提出開店需求"}'
        )

        result = await ProjectIntentAgent(llm).classify("我要開一間咖啡廳")

        self.assertEqual(result.intent, "create_project")
        self.assertEqual(result.title, "咖啡廳")
        self.assertIn('"message": "我要開一間咖啡廳"', llm.calls[0][0])
        self.assertIn("json_schema", llm.calls[0][1])
