"""Discord Bot 入口：建立指令並把 AI 問題交給 LLMService。"""

from __future__ import annotations

import logging  # 記錄 Bot 上線、指令執行與錯誤狀態。
import json  # 將 Agent 的結構化輸出整理成 Discord 可讀文字。
import time  # 顯示 Agent 與 PM 整合草案的實際處理時間。
from typing import Protocol  # 定義 Bot 需要的 LLM 服務介面。

import discord  # Discord API 的主要型別與 Embed 功能。
from discord import app_commands  # 處理 /help 這類斜線指令。
from discord.ext import commands  # 處理 !hello、!ask 這類文字指令。

from agents import CreativeAgent, FinanceAgent, PMAgent, ResearchAgent, ReviewAgent
from config import Settings, configure_logging, load_settings  # 載入集中管理的程式設定。
from models.meeting import MeetingRecord, MeetingStepRecord, ProposalMetrics
from models.project import Project, RequirementChange
from repositories.guild_project_store import JsonGuildProjectStore
from repositories.meeting_repository import JsonMeetingRepository
from repositories.project_repository import (
    JsonProjectRepository,
    ProjectRepository,
    ProjectRepositoryError,
)
from services.llm_service import LLMResponse, LLMService, LLMServiceError  # 載入 Ollama 服務。
from services.meeting_manager import (
    MeetingManager,
    MeetingManagerError,
    MeetingStepCallback,
    ReviewWorkflowResult,
)
from services.project_meeting_service import (
    ProjectMeetingError,
    ProjectMeetingService,
)


logger = logging.getLogger(__name__)  # 使用模組名稱建立日誌，不記錄 Discord Token。
TRUNCATION_SUFFIX = "\n\n…（回覆過長，已截斷）"  # 模型回答太長時加在結尾。
AGENT_TITLES = {
    "PM Agent": "PM 需求拆解",
    "Research Agent": "Research 研究觀點",
    "Creative Agent": "Creative 創意提案",
    "Finance Agent": "Finance 財務評估",
}


class ChatService(Protocol):
    """Bot 實際需要的最小介面，讓測試能注入假的 LLMService。"""

    async def chat(self, message: str) -> LLMResponse:
        """接收問題並回傳模型回答。"""


class MeetingService(Protocol):
    """Bot 啟動專案會議時依賴的最小介面。"""

    async def start_project(self, guild_id: int, project_id: str) -> Project:
        """啟動 Guild 的指定專案並回傳更新後資料。"""


class DiscussionManager(Protocol):
    """Bot 執行多輪 Agent 討論時需要的最小介面。"""

    async def start_first_round(
        self,
        guild_id: int,
        project_id: str,
        requirement: str,
        *,
        on_step: MeetingStepCallback | None = None,
    ) -> MeetingRecord:
        """執行第一輪並在每位 Agent 完成時回報。"""

    async def start_second_round(
        self,
        guild_id: int,
        requirement_change: RequirementChange,
        *,
        on_step: MeetingStepCallback | None = None,
    ) -> MeetingRecord:
        """套用一次需求變更並執行第二輪。"""

    async def create_proposal_draft(self, guild_id: int) -> dict[str, object]:
        """建立或讀取 PM 整合草案。"""

    def get_proposal_metrics(self, guild_id: int) -> ProposalMetrics | None:
        """取得已保存的 PM 草案使用量統計。"""

    async def review_and_finalize(self, guild_id: int) -> ReviewWorkflowResult:
        """審查草案、執行至多一次修改並產生最終方案。"""

    def get_final_proposal_metrics(self, guild_id: int) -> ProposalMetrics | None:
        """取得最終方案的 PM 使用量統計。"""


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


def split_discord_message(content: str, max_length: int) -> list[str]:
    """將完整內容切成多段，優先在換行處分割且不遺失文字。"""

    if max_length <= 0:
        raise ValueError("Discord 訊息長度必須大於 0。")

    remaining = content
    chunks: list[str] = []
    while len(remaining) > max_length:
        split_at = remaining.rfind("\n", 0, max_length + 1)
        if split_at <= 0:
            split_at = max_length
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


def format_discussion_messages(
    agent_name: str,
    current: int,
    total: int,
    output_data: dict[str, object],
    *,
    max_length: int,
    round_number: int | None = None,
    execution_time_seconds: float | None = None,
    input_characters: int | None = None,
    output_characters: int | None = None,
    max_prompt_characters: int | None = None,
    max_response_characters: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    max_output_tokens: int | None = None,
) -> list[str]:
    """將 Agent 結構化輸出轉成帶進度、符合 Discord 上限的訊息。"""

    title = AGENT_TITLES.get(agent_name, agent_name)
    round_label = f"第 {round_number} 輪 " if round_number is not None else ""
    time_label = (
        f"（{execution_time_seconds:.2f} 秒）"
        if execution_time_seconds is not None
        else ""
    )
    usage_parts: list[str] = []
    if input_characters is not None:
        usage_parts.append(
            _format_limit_usage(
                "輸入",
                input_characters,
                max_prompt_characters,
                "字元",
            )
        )
    if output_characters is not None:
        usage_parts.append(
            _format_limit_usage(
                "輸出",
                output_characters,
                max_response_characters,
                "字元",
            )
        )
    if prompt_tokens is not None:
        usage_parts.append(f"Prompt Token {prompt_tokens:,}")
    if completion_tokens is not None:
        usage_parts.append(
            _format_limit_usage(
                "輸出 Token",
                completion_tokens,
                max_output_tokens,
                "",
            )
        )
    usage_line = f"📊 {'｜'.join(usage_parts)}\n" if usage_parts else ""
    first_header = (
        f"**{round_label}{current}/{total} {title}{time_label}**\n{usage_line}"
    )
    continuation_header = (
        f"**{round_label}{current}/{total} {title}（續）{time_label}**\n"
    )
    reserved_length = max(len(first_header), len(continuation_header))
    if max_length <= reserved_length:
        raise ValueError("Discord 訊息上限不足以放入進度標題。")

    # ensure_ascii=False 讓中文直接顯示；縮排可保留結構化輸出的層次。
    body = json.dumps(output_data, ensure_ascii=False, indent=2)
    chunks = split_discord_message(body, max_length - reserved_length)
    return [
        (first_header if index == 0 else continuation_header) + chunk
        for index, chunk in enumerate(chunks)
    ]


def _format_limit_usage(
    label: str,
    value: int,
    limit: int | None,
    unit: str,
) -> str:
    """顯示用量；達上限 80% 警告，95% 顯示高風險。"""

    suffix = f" {unit}" if unit else ""
    if limit is None or limit <= 0:
        return f"{label} {value:,}{suffix}"

    ratio = value / limit
    warning = "🚨 " if ratio >= 0.95 else "⚠️ " if ratio >= 0.80 else ""
    return (
        f"{warning}{label} {value:,}/{limit:,}{suffix}"
        f"（{ratio:.1%}）"
    )


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


def format_proposal_embed(
    proposal: dict[str, object],
    *,
    execution_time_seconds: float | None = None,
    metrics: ProposalMetrics | None = None,
    max_prompt_characters: int | None = None,
    max_response_characters: int | None = None,
) -> discord.Embed:
    """將 PM 整合草案濃縮成適合 Discord 顯示的 Embed。"""

    title = str(proposal.get("title", "PM 整合草案"))[:250]
    summary = str(proposal.get("summary", "尚無摘要"))[:4000]
    raw_decisions = proposal.get("decisions", [])
    decisions = raw_decisions if isinstance(raw_decisions, list) else []
    counts = {"採用": 0, "拒絕": 0, "折衷": 0}
    decision_lines: list[str] = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision", ""))
        if decision in counts:
            counts[decision] += 1
        topic = str(item.get("topic", "未命名決策"))
        reason = str(item.get("reason", ""))
        decision_lines.append(f"• **{decision}｜{topic}**：{reason}")

    embed = discord.Embed(
        title=f"📄 {title}",
        description=summary,
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="決策統計",
        value=(
            f"採用 {counts['採用']}｜拒絕 {counts['拒絕']}｜"
            f"折衷 {counts['折衷']}"
        ),
        inline=False,
    )
    decision_text = "\n".join(decision_lines[:5]) or "尚無決策紀錄。"
    embed.add_field(
        name="主要決策",
        value=decision_text[:1024],
        inline=False,
    )
    footer = "完整章節與決策已保存至會議紀錄。"
    if metrics is not None:
        usage_parts = [
            _format_limit_usage(
                "輸入",
                metrics.input_characters,
                max_prompt_characters,
                "字元",
            ),
            _format_limit_usage(
                "輸出",
                metrics.output_characters,
                max_response_characters,
                "字元",
            ),
        ]
        if metrics.prompt_tokens is not None:
            usage_parts.append(f"Prompt Token {metrics.prompt_tokens:,}")
        if metrics.completion_tokens is not None:
            usage_parts.append(
                _format_limit_usage(
                    "輸出 Token",
                    metrics.completion_tokens,
                    metrics.max_output_tokens,
                    "",
                )
            )
        footer += " " + "｜".join(usage_parts) + "。"
        footer += f" 處理耗時 {metrics.execution_time_seconds:.2f} 秒。"
    elif execution_time_seconds is not None:
        footer += f" 本次處理耗時 {execution_time_seconds:.2f} 秒。"
    embed.set_footer(text=footer)
    return embed


def format_review_embed(review: dict[str, object]) -> discord.Embed:
    """將四項審查、修改要求與指定負責人顯示成 Discord Embed。"""

    status = str(review.get("status", "未知"))
    passed = status == "通過"
    embed = discord.Embed(
        title=f"{'✅' if passed else '🛠️'} Review 審查：{status}",
        color=discord.Color.green() if passed else discord.Color.orange(),
    )
    checklist = review.get("checklist", {})
    checklist_names = {
        "completeness": "完整度",
        "creativity": "創意",
        "credibility": "可信度",
        "feasibility": "可行性",
    }
    if isinstance(checklist, dict):
        lines: list[str] = []
        for key, label in checklist_names.items():
            item = checklist.get(key)
            if not isinstance(item, dict):
                continue
            icon = "✅" if item.get("passed") is True else "❌"
            lines.append(f"{icon} **{label}**：{item.get('reason', '')}")
        embed.description = "\n".join(lines)[:4000]

    raw_issues = review.get("issues", [])
    issues = raw_issues if isinstance(raw_issues, list) else []
    issue_lines = [
        (
            f"• **{item.get('priority', '未定')}｜"
            f"{item.get('assigned_agent', '未指定')}**\n"
            f"問題：{item.get('problem', '')}\n"
            f"要求：{item.get('required_change', '')}"
        )
        for item in issues
        if isinstance(item, dict)
    ]
    embed.add_field(
        name="修改要求",
        value=("\n\n".join(issue_lines) or "無，草案可以直接通過。")[:1024],
        inline=False,
    )
    return embed


def create_bot(
    settings: Settings,
    llm_service: ChatService,
    project_repository: ProjectRepository,
    meeting_service: MeetingService,
    discussion_manager: DiscussionManager,
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
        embed.add_field(
            name="/change <change_id>",
            value="套用一次需求變更並開始第二輪討論。",
            inline=False,
        )
        embed.add_field(
            name="/draft",
            value="由 PM 將兩輪討論整合成提案草案。",
            inline=False,
        )
        embed.add_field(
            name="/review",
            value="審查草案、至多修改一次，再由 PM 產生最終方案。",
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

        # 第一輪發言需要讓伺服器成員看見，因此使用公開的 defer 與 Followup。
        await interaction.response.defer(thinking=True)
        try:
            project = await meeting_service.start_project(
                interaction.guild_id,
                project_id,
            )
        except ProjectMeetingError as error:
            logger.warning("/start 失敗 guild_id=%s：%s", interaction.guild_id, error)
            await interaction.followup.send(f"❌ {error}")
            return

        embed = discord.Embed(
            title="✅ 專案會議已啟動",
            description=f"{project.id}｜{project.title}",
            color=discord.Color.green(),
        )
        embed.add_field(name="狀態", value=project.status.label, inline=True)
        embed.add_field(name="期限", value=project.deadline.isoformat(), inline=True)
        await interaction.followup.send(embed=embed)

        async def display_agent_step(
            current: int,
            total: int,
            step: MeetingStepRecord,
        ) -> None:
            """每位 Agent 完成後，立即把進度與完整發言送到 Discord。"""

            output_data = step.output_data or {}
            messages = format_discussion_messages(
                step.agent_name,
                current,
                total,
                output_data,
                max_length=settings.max_meeting_message_length,
                execution_time_seconds=step.execution_time_seconds,
                input_characters=step.input_characters,
                output_characters=step.output_characters,
                max_prompt_characters=settings.max_meeting_prompt_length,
                max_response_characters=settings.max_meeting_response_length,
                prompt_tokens=step.prompt_tokens,
                completion_tokens=step.completion_tokens,
                max_output_tokens=step.max_output_tokens,
            )
            for message in messages:
                await interaction.followup.send(message)

        requirement = "\n".join(project.requirements)
        try:
            await discussion_manager.start_first_round(
                interaction.guild_id,
                project.id,
                requirement,
                on_step=display_agent_step,
            )
        except MeetingManagerError as error:
            logger.warning(
                "第一輪討論失敗 guild_id=%s project_id=%s：%s",
                interaction.guild_id,
                project.id,
                error,
            )
            await interaction.followup.send(f"❌ 第一輪討論失敗：{error}")
            return

        await interaction.followup.send("✅ 第一輪討論完成。")
        logger.info(
            "第一輪討論完成 guild_id=%s project_id=%s",
            interaction.guild_id,
            project.id,
        )

    @bot.tree.command(name="change", description="套用需求變更並開始第二輪討論")
    @app_commands.describe(change_id="需求變更 ID，例如 CHG-001")
    async def change_command(
        interaction: discord.Interaction,
        change_id: str,
    ) -> None:
        """尋找指定需求變更，讓四位 Agent 各完成一次第二輪回應。"""

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "❌ /change 只能在 Discord 伺服器中使用。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        normalized_change_id = change_id.strip().upper()
        try:
            projects = project_repository.list_projects()
        except ProjectRepositoryError as error:
            logger.error("讀取需求變更失敗：%s", error)
            await interaction.followup.send("❌ 無法讀取需求變更資料，請稍後再試。")
            return

        requirement_change = next(
            (
                change
                for project in projects
                for change in project.requirement_changes
                if change.id.upper() == normalized_change_id
            ),
            None,
        )
        if requirement_change is None:
            await interaction.followup.send(
                f"❌ 找不到需求變更 ID：{normalized_change_id}。"
            )
            return

        await interaction.followup.send(
            "🔄 套用需求變更 "
            f"{requirement_change.id}：{requirement_change.description}\n"
            f"原因：{requirement_change.reason}"
        )

        async def display_second_round_step(
            current: int,
            total: int,
            step: MeetingStepRecord,
        ) -> None:
            output_data = step.output_data or {}
            messages = format_discussion_messages(
                step.agent_name,
                current,
                total,
                output_data,
                max_length=settings.max_meeting_message_length,
                round_number=2,
                execution_time_seconds=step.execution_time_seconds,
                input_characters=step.input_characters,
                output_characters=step.output_characters,
                max_prompt_characters=settings.max_meeting_prompt_length,
                max_response_characters=settings.max_meeting_response_length,
                prompt_tokens=step.prompt_tokens,
                completion_tokens=step.completion_tokens,
                max_output_tokens=step.max_output_tokens,
            )
            for message in messages:
                await interaction.followup.send(message)

        try:
            await discussion_manager.start_second_round(
                interaction.guild_id,
                requirement_change,
                on_step=display_second_round_step,
            )
        except MeetingManagerError as error:
            logger.warning(
                "第二輪討論失敗 guild_id=%s change_id=%s：%s",
                interaction.guild_id,
                normalized_change_id,
                error,
            )
            await interaction.followup.send(f"❌ 第二輪討論失敗：{error}")
            return

        await interaction.followup.send("✅ 第二輪討論完成。")

    @bot.tree.command(name="draft", description="由 PM 整合兩輪討論並顯示提案摘要")
    async def draft_command(interaction: discord.Interaction) -> None:
        """建立固定格式提案，保存決策並在 Discord 顯示摘要。"""

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "❌ /draft 只能在 Discord 伺服器中使用。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        started_at = time.perf_counter()
        try:
            proposal = await discussion_manager.create_proposal_draft(
                interaction.guild_id
            )
        except MeetingManagerError as error:
            execution_time = time.perf_counter() - started_at
            logger.warning(
                "/draft 失敗 guild_id=%s execution_seconds=%.3f：%s",
                interaction.guild_id,
                execution_time,
                error,
            )
            await interaction.followup.send(
                f"❌ 建立整合草案失敗：{error}\n"
                f"處理耗時：{execution_time:.2f} 秒"
            )
            return

        execution_time = time.perf_counter() - started_at
        metrics_getter = getattr(
            discussion_manager,
            "get_proposal_metrics",
            None,
        )
        metrics = (
            metrics_getter(interaction.guild_id)
            if callable(metrics_getter)
            else None
        )
        await interaction.followup.send(
            embed=format_proposal_embed(
                proposal,
                execution_time_seconds=execution_time,
                metrics=metrics,
                max_prompt_characters=settings.max_meeting_prompt_length,
                max_response_characters=settings.max_meeting_response_length,
            )
        )
        logger.info("PM 整合草案完成 guild_id=%s", interaction.guild_id)

    @bot.tree.command(name="review", description="審查草案並產生最終方案")
    async def review_command(interaction: discord.Interaction) -> None:
        """執行 Review、一次指定修改與 PM 最終整合。"""

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "❌ /review 只能在 Discord 伺服器中使用。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            result = await discussion_manager.review_and_finalize(
                interaction.guild_id
            )
        except MeetingManagerError as error:
            logger.warning("/review 失敗 guild_id=%s：%s", interaction.guild_id, error)
            await interaction.followup.send(f"❌ Review 審查失敗：{error}")
            return

        await interaction.followup.send(embed=format_review_embed(result.review))
        if result.revision_performed:
            await interaction.followup.send(
                "🔧 已將最高優先修改要求交給 "
                f"**{result.revision_agent_name}**，本會議修改次數已達 1/1。"
            )
        else:
            await interaction.followup.send("✅ 草案通過，未使用修改機會。")

        metrics = discussion_manager.get_final_proposal_metrics(
            interaction.guild_id
        )
        await interaction.followup.send(
            embed=format_proposal_embed(
                result.final_proposal,
                metrics=metrics,
                max_prompt_characters=settings.max_meeting_prompt_length,
                max_response_characters=settings.max_meeting_response_length,
            )
        )
        logger.info(
            "Review 與最終整合完成 guild_id=%s revision_count=%s",
            interaction.guild_id,
            1 if result.revision_performed else 0,
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
    meeting_repository = JsonMeetingRepository(settings.meetings_file)
    discussion_manager = MeetingManager(
        meeting_repository,
        PMAgent(llm_service),
        ResearchAgent(llm_service),
        CreativeAgent(llm_service),
        FinanceAgent(llm_service),
        ReviewAgent(llm_service),
        max_prompt_characters=settings.max_meeting_prompt_length,
        max_response_characters=settings.max_meeting_response_length,
    )
    bot = create_bot(
        settings,
        llm_service,
        project_repository,
        meeting_service,
        discussion_manager,
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
