import asyncio

from services.llm_serviceapi import LLMServiceAPI, LLMServiceAPIError


async def main():
    # 建立直接呼叫 Ollama HTTP API 的服務，最長等待 300 秒。
    service = LLMServiceAPI(timeout=300)

    try:
        # 這裡會真的呼叫 POST http://localhost:11434/api/chat。
        result = await service.chat("請使用繁體中文，簡單說明什麼是 Discord Bot。")
    except LLMServiceAPIError as error:
        print(f"測試失敗：{error}")
        return

    # 顯示從 message.content 解析出的模型回答。
    print(f"回答：{result.content}")

    # 顯示 Ollama 回傳的 Token 統計與 Python 實際等待時間。
    print(f"Prompt Token：{result.usage.prompt_tokens}")
    print(f"輸出 Token：{result.usage.completion_tokens}")
    print(f"總 Token：{result.usage.total_tokens}")
    print(f"請求耗時：{result.usage.request_duration_seconds:.3f} 秒")


if __name__ == "__main__":
    asyncio.run(main())
