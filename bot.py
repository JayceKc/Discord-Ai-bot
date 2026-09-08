"""Discord Bot 入口：建立指令並把 AI 問題交給 LLMService。"""

from __future__ import annotations

import logging  # 記錄 Bot 上線、指令執行與錯誤狀態。
from typing import Protocol  # 定義 Bot 需要的 LLM 服務介面。

import discord  # Discord API 的主要型別與 Embed 功能。
from discord import app_commands  # 處理 /help 這類斜線指令。
from discord.ext import commands  # 處理 !hello、!ask 這類文字指令。

from config import Settings, configure_logging, load_settings  # 載入集中管理的程式設定。
from models.project import Project
from repositories.guild_project_store import JsonGuildProjectStore
from repositories.project_repository import (
    JsonProjectRepository,
    ProjectRepository,
    ProjectRepositoryError,
)
from services.llm_service import LLMResponse, LLMService, LLMServiceError  # 載入 Ollama 服務。
from services.project_meeting_service import (
    ProjectMeetingError,
    ProjectMeetingService,
)


logger = logging.getLogger(__name__)  # 使用模組名稱建立日誌，不記錄 Discord Token。
TRUNCATION_SUFFIX = "\n\n…（回覆過長，已截斷）"  # 模型回答太長時加在結尾。


class ChatService(Protocol):
    """Bot 實際需要的最小介面，讓測試能注入假的 LLMService。"""

    async def chat(self, message: str) -> LLMResponse:
        """接收問題並回傳模型回答。"""


class MeetingService(Protocol):
    """Bot 啟動專案會議時依賴的最小介面。"""

    async def start_project(self, guild_id: int, project_id: str) -> Project:
        """啟動 Guild 的指定專案並回傳更新後資料。"""


class MyBot(commands.Bot):
    """啟動前會同步斜線指令的 Discord Bot。"""

    async def setup_hook(self) -> None:  # Discord 登入前會自動執行一次。
        synced_commands = await self.tree.sync()  # 把 /help 同步到 Discord。
        logger.info("已同步 %s 個斜線指令", len(synced_commands))


def truncate_answer(answer: str, max_length: int) -> str:
    """截斷過長回答，並把提示文字計入最大長度。"""

    if len(answer) <= max_length:  # 回答未超過限制時不需要修改。
        return answer  # 直接回傳完整回答。

    # 先保留截斷提示需要的空間，確保組合後仍不超過 max_length。
    content_length = max_length - len(TRUNCATION_SUFFIX)
    return answer[:content_length] + TRUNCATION_SUFFIX  # 取前段回答再接上提示。


def format_project(project: Project) -> str:
    """把 Project 轉成適合放進 Discord Embed 欄位的文字。"""

    requirements = "、".join(project.requirements)
    acceptance = "、".join(project.acceptance_criteria)
    change_summary = (
        f"{len(project.requirement_changes)} 筆"
        if project.requirement_changes
        else "無"
    )
    return (
        f"**類別：** {project.category}\n"
        f"**需求：** {requirements}\n"
        f"**預算：** NT$ {project.budget:,}\n"
        f"**期限：** {project.deadline.isoformat()}\n"
        f"**狀態：** {project.status.label}\n"
        f"**驗收：** {acceptance}\n"
        f"**需求變更：** {change_summary}"
    )


def create_bot(
    settings: Settings,
    llm_service: ChatService,
    project_repository: ProjectRepository,
    meeting_service: MeetingService,
) -> MyBot:
    """使用外部傳入的設定與服務建立 Bot，方便正式執行和測試。"""

    intents = discord.Intents.default()  # 建立 Discord 預設事件權限。
    intents.message_content = True  # !hello 與 !ask 都需要讀取訊息內容。
    bot = MyBot(command_prefix="!", intents=intents)  # 指定文字指令使用 ! 開頭。

    @bot.event  # 將下方函式註冊為 Bot 上線事件。
    async def on_ready() -> None:  # Bot 登入並準備完成時觸發。
        logger.info("機器人已上線：%s", bot.user)

    @bot.command(name="hello")  # 註冊 !hello 文字指令。
    async def hello(ctx: commands.Context) -> None:  # ctx 包含使用者、頻道等指令資訊。
        """回覆並標記執行指令的使用者。"""

        await ctx.send(f"你好，{ctx.author.mention}！")  # mention 會在 Discord 標記該使用者。

    @bot.tree.command(name="hello", description="讓機器人向你打招呼")
    async def slash_hello(interaction: discord.Interaction) -> None:
        """示範 Slash Command 的 defer 與 Followup 回覆流程。"""

        # 先回應 Discord，避免較久的工作讓 Interaction 在三秒內逾時。
        # thinking=True 會顯示「機器人正在思考」，ephemeral=True 代表只有本人看得到。
        await interaction.response.defer(thinking=True, ephemeral=True)

        # defer() 已經使用第一次 Interaction 回應，後續訊息必須改用 followup。
        await interaction.followup.send(
            f"你好，{interaction.user.mention}！",
            ephemeral=True,
        )

    @bot.command(name="ask")  # 註冊 !ask 文字指令。
    async def ask(ctx: commands.Context, *, question: str) -> None:
        """把使用者的完整問題交給 Qwen，再把結果傳回 Discord。"""

        question = question.strip()  # 移除問題前後多餘空白。
        if len(question) > settings.max_ask_input_length:  # 檢查是否超過設定的 500 字。
            await ctx.send(
                f"問題過長，請限制在 {settings.max_ask_input_length} 字以內。"
            )
            return  # 輸入不合法時提前結束，不呼叫 Ollama。

        # ctx.send() 會回傳訊息物件，保存後才能在模型完成時編輯內容。
        processing_message = await ctx.send("⏳ Qwen 正在處理你的問題，請稍候……")
        logger.info("開始處理 !ask（輸入長度=%s）", len(question))  # 只記錄長度，不記錄問題。

        try:
            result = await llm_service.chat(question)  # 非同步等待 Qwen 產生回答。
        except LLMServiceError as error:
            # LLMService 已將未啟動、逾時等底層例外轉成安全訊息。
            logger.warning("!ask 執行失敗：%s", error)
            await processing_message.edit(content=f"❌ {error}")  # 將處理中訊息改成錯誤原因。
            return  # 錯誤發生後停止後續流程。

        # 從 LLMResponse 取出文字，並限制 Discord 訊息長度。
        answer = truncate_answer(result.content, settings.max_ask_output_length)
        await processing_message.edit(content=answer)  # 將原本的處理中訊息改成模型回答。
        logger.info("!ask 處理完成（輸出長度=%s）", len(answer))  # 不記錄回答內容。

    @bot.tree.command(name="help", description="顯示機器人指令說明")
    async def help_command(interaction: discord.Interaction) -> None:
        """使用 Embed 顯示目前可以使用的指令。"""

        embed = discord.Embed(  # 建立格式化的 Discord 說明卡片。
            title="🤖 機器人指令說明",
            description="以下是目前可以使用的指令：",
            color=discord.Color.green(),
        )
        embed.add_field(name="/help", value="顯示這份 Embed 指令說明。", inline=False)
        embed.add_field(
            name="/hello",
            value="使用 Slash Command 讓機器人向你打招呼。",
            inline=False,
        )
        embed.add_field(name="!hello", value="讓機器人跟你打招呼。", inline=False)
        embed.add_field(
            name="!ask <問題>",
            value="將問題交給本機 Qwen 模型回答。",
            inline=False,
        )
        embed.add_field(
            name="/projects",
            value="顯示目前的專案清單。",
            inline=False,
        )
        embed.add_field(
            name="/start <project_id>",
            value="啟動指定專案的會議。",
            inline=False,
        )
        embed.set_footer(text=f"查詢者：{interaction.user.display_name}")  # 顯示查詢者名稱。
        # ephemeral=True 代表只有執行 /help 的使用者看得到訊息。
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="projects", description="顯示目前的專案清單")
    async def projects_command(interaction: discord.Interaction) -> None:
        """從 Repository 取得專案，再使用 Embed 顯示在 Discord。"""

        try:
            projects = project_repository.list_projects()
        except ProjectRepositoryError as error:
            logger.error("讀取專案資料失敗：%s", error)
            await interaction.response.send_message(
                "❌ 無法讀取專案資料，請稍後再試。",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📁 專案清單",
            description=f"目前共有 {len(projects)} 個專案。",
            color=discord.Color.blue(),
        )
        for project in projects:
            embed.add_field(
                name=f"{project.id}｜{project.title}",
                value=format_project(project),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info("已顯示 %s 個專案", len(projects))

    @bot.tree.command(name="start", description="啟動指定專案的會議")
    @app_commands.describe(project_id="要啟動的專案 ID，例如 PRJ-001")
    async def start_command(
        interaction: discord.Interaction,
        project_id: str,
    ) -> None:
        """驗證 Guild 和專案 ID，啟動後顯示專案狀態。"""

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "❌ /start 只能在 Discord 伺服器中使用。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            project = await meeting_service.start_project(
                interaction.guild_id,
                project_id,
            )
        except ProjectMeetingError as error:
            logger.warning("/start 失敗 guild_id=%s：%s", interaction.guild_id, error)
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        embed = discord.Embed(
            title="✅ 專案會議已啟動",
            description=f"{project.id}｜{project.title}",
            color=discord.Color.green(),
        )
        embed.add_field(name="狀態", value=project.status.label, inline=True)
        embed.add_field(name="期限", value=project.deadline.isoformat(), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(
            "專案會議已啟動 guild_id=%s project_id=%s",
            interaction.guild_id,
            project.id,
        )

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

    return bot  # 把已完成所有指令註冊的 Bot 交給 main() 或測試使用。


def main() -> None:
    """讀取設定、建立相依物件並啟動 Discord Bot。"""

    settings = load_settings()  # 從 .env 取得 Token、模型與限制設定。
    configure_logging(settings.log_level)  # 啟用統一的日誌格式。

    # 將 config.py 讀到的 Ollama 設定傳給 LLMService。
    llm_service = LLMService(
        host=settings.ollama_host,
        model=settings.ollama_model,
        timeout=settings.ollama_timeout_seconds,
    )
    project_repository = JsonProjectRepository(settings.projects_file)
    guild_project_store = JsonGuildProjectStore(settings.guild_projects_file)
    meeting_service = ProjectMeetingService(
        project_repository,
        guild_project_store,
    )
    bot = create_bot(
        settings,
        llm_service,
        project_repository,
        meeting_service,
    )  # 將設定和服務注入 Discord Bot。

    # 日誌只顯示非敏感設定，絕對不輸出 settings.discord_token。
    logger.info(
        "準備啟動 Bot（Ollama host=%s model=%s）",
        settings.ollama_host,
        settings.ollama_model,
    )
    bot.run(settings.discord_token)  # 使用 Token 登入 Discord 並持續運行。


if __name__ == "__main__":  # 只有執行 python bot.py 時才會成立。
    main()  # 測試匯入 bot.py 時不會登入 Discord。
