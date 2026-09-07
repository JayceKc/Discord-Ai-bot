import unittest

import httpx

from services.llm_service import LLMService, LLMServiceError


class FakeOllamaClient:
    """模擬 ollama.AsyncClient，測試時不會真的呼叫模型。"""

    def __init__(self, response=None, error=None):
        # response 是預計回傳的假資料；error 是預計拋出的假錯誤。
        self.response = response
        self.error = error
        # 保存每次 chat() 收到的參數，讓測試可以檢查請求內容。
        self.calls = []

    async def chat(self, **kwargs):
        # 介面保持 async，才能替代正式的 ollama.AsyncClient.chat()。
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class LLMServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_uses_fake_response_and_parses_content_and_usage(self):
        # Arrange：準備一份假的 Ollama 完整回應。
        fake_client = FakeOllamaClient(
            response={
                "message": {
                    "role": "assistant",
                    "content": "Python 是一種程式語言。",
                },
                "total_duration": 5_191_566_416,
                "load_duration": 2_154_458,
                "prompt_eval_count": 26,
                "eval_count": 18,
            }
        )
        # 將 Fake Client 注入服務，因此不需要啟動 Ollama。
        service = LLMService(client=fake_client)

        # Act：呼叫被測試的方法。
        result = await service.chat("請介紹 Python")

        # Assert：確認模型、訊息及 stream=False 都正確傳入。
        self.assertEqual(
            fake_client.calls,
            [
                {
                    "model": "qwen3.5:4b",
                    "messages": [{"role": "user", "content": "請介紹 Python"}],
                    "stream": False,
                }
            ],
        )
        # 確認 message.content 和 Token 統計都有正確解析。
        self.assertEqual(result.content, "Python 是一種程式語言。")
        self.assertEqual(result.usage.total_duration_ns, 5_191_566_416)
        self.assertEqual(result.usage.load_duration_ns, 2_154_458)
        self.assertEqual(result.usage.prompt_tokens, 26)
        self.assertEqual(result.usage.completion_tokens, 18)
        self.assertEqual(result.usage.total_tokens, 44)
        self.assertGreaterEqual(result.usage.request_duration_seconds, 0)

    async def test_chat_converts_timeout_to_service_error(self):
        # 模擬 Ollama 超過等待時間後拋出的 ReadTimeout。
        request = httpx.Request("POST", "http://localhost:11434/api/chat")
        fake_client = FakeOllamaClient(
            error=httpx.ReadTimeout("timed out", request=request)
        )
        service = LLMService(client=fake_client)

        # 對外只拋出專案自己的 LLMServiceError，避免上層依賴 HTTPX 細節。
        with self.assertRaisesRegex(LLMServiceError, "Ollama 回應逾時"):
            await service.chat("問題")

    async def test_chat_rejects_missing_message_content(self):
        # 模擬 API 成功，但回覆內沒有 message.content。
        service = LLMService(client=FakeOllamaClient(response={"message": {}}))

        with self.assertRaisesRegex(LLMServiceError, "message.content"):
            await service.chat("問題")


if __name__ == "__main__":
    unittest.main()
