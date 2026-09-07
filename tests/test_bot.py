"""測試 Discord 指令流程；不會真的登入 Discord 或呼叫 Ollama。"""

import unittest  # Python 內建單元測試框架。
from types import SimpleNamespace  # 快速建立只具有必要欄位的假物件。
from unittest.mock import AsyncMock  # 模擬需要 await 的 Discord 與 LLM 方法。

from bot import TRUNCATION_SUFFIX, create_bot
from config import Settings
from services.llm_service import LLMServiceError


class BotCommandTest(unittest.IsolatedAsyncioTestCase):
    """使用假 LLMService 驗證 !hello 與 !ask 的行為。"""

    async def asyncSetUp(self) -> None:
        # 測試設定使用假的 Token；create_bot 不會拿它登入 Discord。
        self.settings = Settings(discord_token="fake-discord-token")
        self.fake_service = SimpleNamespace(chat=AsyncMock())  # 不連線 Ollama 的假服務。
        self.bot = create_bot(self.settings, self.fake_service)  # 注入假設定和假服務。

    async def asyncTearDown(self) -> None:
        # 關閉 Bot 內部資源，避免不同測試互相影響。
        await self.bot.close()

    async def test_hello_mentions_user(self) -> None:
        """!hello 應該標記使用者並向他打招呼。"""

        ctx = SimpleNamespace(
            author=SimpleNamespace(mention="@Jayce"),
            send=AsyncMock(),
        )

        # get_command 找到 !hello；callback 直接執行裝飾器包住的原始函式。
        await self.bot.get_command("hello").callback(ctx)

        ctx.send.assert_awaited_once_with("你好，@Jayce！")  # 驗證只傳送過一次正確訊息。

    async def test_ask_shows_processing_then_returns_answer(self) -> None:
        """!ask 應該先顯示處理中，再編輯為模型回答。"""

        processing_message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(send=AsyncMock(return_value=processing_message))
        # 指定假 chat() 被等待後要回傳的模型內容。
        self.fake_service.chat.return_value = SimpleNamespace(content="Qwen 的回答")

        await self.bot.get_command("ask").callback(
            ctx,
            question="什麼是 Discord Bot？",
        )

        ctx.send.assert_awaited_once_with("⏳ Qwen 正在處理你的問題，請稍候……")
        self.fake_service.chat.assert_awaited_once_with("什麼是 Discord Bot？")  # 問題有交給服務。
        processing_message.edit.assert_awaited_once_with(content="Qwen 的回答")

    async def test_ask_rejects_question_over_input_limit(self) -> None:
        """超過 500 字的問題應該被拒絕，而且不能呼叫模型。"""

        ctx = SimpleNamespace(send=AsyncMock())

        await self.bot.get_command("ask").callback(
            ctx,
            question="問" * (self.settings.max_ask_input_length + 1),
        )

        ctx.send.assert_awaited_once_with("問題過長，請限制在 500 字以內。")
        self.fake_service.chat.assert_not_awaited()  # 超長問題不可送進模型。

    async def test_ask_truncates_long_answer(self) -> None:
        """模型回答超過 1900 字時應該附上截斷提示。"""

        processing_message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(send=AsyncMock(return_value=processing_message))
        self.fake_service.chat.return_value = SimpleNamespace(content="答" * 2500)

        await self.bot.get_command("ask").callback(ctx, question="請回答")

        # 從 edit(content=...) 的呼叫紀錄取出實際送給 Discord 的內容。
        edited_content = processing_message.edit.await_args.kwargs["content"]
        self.assertEqual(len(edited_content), self.settings.max_ask_output_length)
        self.assertTrue(edited_content.endswith(TRUNCATION_SUFFIX))

    async def test_ask_displays_ollama_not_running_error(self) -> None:
        """Ollama 未啟動時應該顯示可理解的連線錯誤。"""

        processing_message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(send=AsyncMock(return_value=processing_message))
        self.fake_service.chat.side_effect = LLMServiceError(
            "無法連線到 Ollama，請確認服務是否已啟動。"
        )

        # 捕捉 WARNING 日誌，避免測試畫面出現預期中的錯誤訊息。
        with self.assertLogs("bot", level="WARNING") as captured_logs:
            await self.bot.get_command("ask").callback(ctx, question="請回答")

        processing_message.edit.assert_awaited_once_with(
            content="❌ 無法連線到 Ollama，請確認服務是否已啟動。"
        )
        self.assertNotIn(
            self.settings.discord_token,
            "\n".join(captured_logs.output),
        )

    async def test_ask_displays_timeout_error(self) -> None:
        """Ollama 回應逾時時應該把逾時原因顯示在 Discord。"""

        processing_message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(send=AsyncMock(return_value=processing_message))
        self.fake_service.chat.side_effect = LLMServiceError(
            "Ollama 回應逾時，請稍後再試。"
        )

        with self.assertLogs("bot", level="WARNING") as captured_logs:
            await self.bot.get_command("ask").callback(ctx, question="請回答")

        processing_message.edit.assert_awaited_once_with(
            content="❌ Ollama 回應逾時，請稍後再試。"
        )
        self.assertNotIn(
            self.settings.discord_token,
            "\n".join(captured_logs.output),
        )


if __name__ == "__main__":
    unittest.main()
