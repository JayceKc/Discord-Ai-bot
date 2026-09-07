import json
import unittest

import httpx

from services.llm_serviceapi import LLMServiceAPI, LLMServiceAPIError


class LLMServiceAPITest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_posts_json_and_parses_fake_response(self):
        # MockTransport 的 handler 會收到服務送出的 HTTP Request。
        async def handler(request: httpx.Request) -> httpx.Response:
            # 確認真的使用 POST /api/chat。
            self.assertEqual(request.method, "POST")
            self.assertEqual(str(request.url), "http://localhost:11434/api/chat")

            # 將 Request Body 轉回 dict，檢查模型、訊息和 stream=False。
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "qwen3.5:4b")
            self.assertEqual(
                payload["messages"],
                [{"role": "user", "content": "請介紹 Python"}],
            )
            self.assertFalse(payload["stream"])

            # 回傳假的 Ollama JSON，因此測試不需要啟動真正的模型。
            return httpx.Response(
                200,
                json={
                    "message": {
                        "role": "assistant",
                        "content": "Python 是一種程式語言。",
                    },
                    "total_duration": 5_191_566_416,
                    "load_duration": 2_154_458,
                    "prompt_eval_count": 26,
                    "eval_count": 18,
                },
            )

        # 將 MockTransport 注入服務，攔截 AsyncClient 的 HTTP 請求。
        service = LLMServiceAPI(transport=httpx.MockTransport(handler))

        result = await service.chat("請介紹 Python")

        # 驗證 message.content 與 Token 統計解析結果。
        self.assertEqual(result.content, "Python 是一種程式語言。")
        self.assertEqual(result.usage.total_duration_ns, 5_191_566_416)
        self.assertEqual(result.usage.load_duration_ns, 2_154_458)
        self.assertEqual(result.usage.prompt_tokens, 26)
        self.assertEqual(result.usage.completion_tokens, 18)
        self.assertEqual(result.usage.total_tokens, 44)

    async def test_chat_converts_timeout_to_service_error(self):
        # 讓假的 Transport 主動拋出 ReadTimeout。
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        service = LLMServiceAPI(transport=httpx.MockTransport(handler))

        with self.assertRaisesRegex(LLMServiceAPIError, "Ollama 回應逾時"):
            await service.chat("問題")

    async def test_chat_rejects_missing_message_content(self):
        # 模擬 API 狀態為 200，但 JSON 缺少必要回答內容。
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"message": {}})

        service = LLMServiceAPI(transport=httpx.MockTransport(handler))

        with self.assertRaisesRegex(LLMServiceAPIError, "message.content"):
            await service.chat("問題")


if __name__ == "__main__":
    unittest.main()
