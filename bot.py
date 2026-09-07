"""Discord Bot 入口：建立指令並把 AI 問題交給 LLMService。"""

from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands
from discord.ext import commands

from config import Settings, configure_logging, load_settings
from services.llm_service import LLMResponse, LLMService, LLMServiceError


logger = logging.getLogger(__name__)  # 使用模組名稱建立日誌，不記錄 Discord Token。
TRUNCATION_SUFFIX = "\n\n…（回覆過長，已截斷）"


class ChatService(Protocol):
    """Bot 實際需要的最小介面，讓測試能注入假的 LLMService。"""

    async def chat(self, message: str) -> LLMResponse:
        """接收問題並回傳模型回答。"""


class MyBot(commands.Bot):
    """啟動前會同步斜線指令的 Discord Bot。"""

    async def setup_hook(self) -> None:
        synced_commands = await self.tree.sync()
        logger.info("已同步 %s 個斜線指令", len(synced_commands))


def truncate_answer(answer: str, max_length: int) -> str:
    """截斷過長回答，並把提示文字計入最大長度。"""

    if len(answer) <= max_length:
        return answer

    content_length = max_length - len(TRUNCATION_SUFFIX)
    return answer[:content_length] + TRUNCATION_SUFFIX


def create_bot(settings: Settings, llm_service: ChatService) -> MyBot:
    """使用外部傳入的設定與 LLM Service 建立 Bot，方便正式執行和測試。"""

    intents = discord.Intents.default()
    intents.message_content = True  # !hello 與 !ask 都需要讀取訊息內容。
    bot = MyBot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready() -> None:
        logger.info("機器人已上線：%s", bot.user)

    @bot.command(name="hello")
    async def hello(ctx: commands.Context) -> None:
        """回覆並標記執行指令的使用者。"""

        await ctx.send(f"你好，{ctx.author.mention}！")

    @bot.command(name="ask")
    async def ask(ctx: commands.Context, *, question: str) -> None:
        """把使用者的完整問題交給 Qwen，再把結果傳回 Discord。"""

        question = question.strip()
        if len(question) > settings.max_ask_input_length:
            await ctx.send(
                f"問題過長，請限制在 {settings.max_ask_input_length} 字以內。"
            )
            return

        processing_message = await ctx.send("⏳ Qwen 正在處理你的問題，請稍候……")
        logger.info("開始處理 !ask（輸入長度=%s）", len(question))

        try:
            result = await llm_service.chat(question)
        except LLMServiceError as error:
            # LLMService 已將未啟動、逾時等底層例外轉成安全訊息。
            logger.warning("!ask 執行失敗：%s", error)
            await processing_message.edit(content=f"❌ {error}")
            return

        answer = truncate_answer(result.content, settings.max_ask_output_length)
        await processing_message.edit(content=answer)
        logger.info("!ask 處理完成（輸出長度=%s）", len(answer))

    @bot.tree.command(name="help", description="顯示機器人指令說明")
    async def help_command(interaction: discord.Interaction) -> None:
        """使用 Embed 顯示目前可以使用的指令。"""

        embed = discord.Embed(
            title="🤖 機器人指令說明",
            description="以下是目前可以使用的指令：",
            color=discord.Color.green(),
        )
        embed.add_field(name="/help", value="顯示這份 Embed 指令說明。", inline=False)
        embed.add_field(name="!hello", value="讓機器人跟你打招呼。", inline=False)
        embed.add_field(
            name="!ask <問題>",
            value="將問題交給本機 Qwen 模型回答。",
            inline=False,
        )
        embed.set_footer(text=f"查詢者：{interaction.user.display_name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.event
    async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
        """將常見文字指令錯誤轉成容易理解的 Discord 訊息。"""

        if isinstance(error, commands.CommandNotFound):
            await ctx.send("找不到這個指令，請輸入 `/help` 查看可用指令。")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"缺少必要參數：`{error.param.name}`")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("參數格式不正確，請檢查後再試。")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"指令冷卻中，請在 {error.retry_after:.1f} 秒後再試。")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("你沒有使用這個指令的權限。")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send("你目前無法使用這個指令。")
        else:
            logger.error("未處理的文字指令錯誤：%r", error)
            await ctx.send("指令執行時發生錯誤，請稍後再試。")

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """統一處理斜線指令錯誤。"""

        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"指令冷卻中，請在 {error.retry_after:.1f} 秒後再試。"
        elif isinstance(error, app_commands.MissingPermissions):
            message = "你沒有使用這個指令的權限。"
        elif isinstance(error, app_commands.TransformerError):
            message = "參數格式不正確，請檢查後再試。"
        elif isinstance(error, app_commands.CheckFailure):
            message = "你目前無法使用這個指令。"
        else:
            logger.error("未處理的斜線指令錯誤：%r", error)
            message = "指令執行時發生錯誤，請稍後再試。"

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    return bot


def main() -> None:
    """讀取設定、建立相依物件並啟動 Discord Bot。"""

    settings = load_settings()
    configure_logging(settings.log_level)

    llm_service = LLMService(
        host=settings.ollama_host,
        model=settings.ollama_model,
        timeout=settings.ollama_timeout_seconds,
    )
    bot = create_bot(settings, llm_service)

    # 日誌只顯示非敏感設定，絕對不輸出 settings.discord_token。
    logger.info(
        "準備啟動 Bot（Ollama host=%s model=%s）",
        settings.ollama_host,
        settings.ollama_model,
    )
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
