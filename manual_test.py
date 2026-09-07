import asyncio

from services.llm_service import LLMService, LLMServiceError


async def main():
    service = LLMService(timeout=300)

    try:
        result = await service.chat("請只回答：Python 連線測試成功")
    except LLMServiceError as error:
        print(f"測試失敗：{error}")
        return

    print(f"回答：{result.content}")
    print(f"Prompt Token：{result.usage.prompt_tokens}")
    print(f"輸出 Token：{result.usage.completion_tokens}")
    print(f"總 Token：{result.usage.total_tokens}")
    print(f"請求耗時：{result.usage.request_duration_seconds:.3f} 秒")


if __name__ == "__main__":
    asyncio.run(main())
