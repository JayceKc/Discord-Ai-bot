"""Discord Bot 入口：建立指令並把 AI 問題交給 LLMService。"""

from __future__ import annotations

import logging  # 記錄 Bot 上線、指令執行與錯誤狀態。
import json  # 將 Agent 的結構化輸出整理成 Discord 可讀文字。
import time  # 顯示 Agent 與 PM 整合草案的實際處理時間。
import inspect
from typing import Protocol  # 定義 Bot 需要的 LLM 服務介面。

import discord  # Discord API 的主要型別與 Embed 功能。
from discord import app_commands  # 處理 /help 這類斜線指令。
from discord.ext import commands  # 處理 !hello、!ask 這類文字指令。

from agents import (
    AgentError,
    AgentLLMServiceProtocol,
    CreativeAgent,
    FinanceAgent,
    PMAgent,
    ProjectIntentAgent,
    ResearchAgent,
    ReviewAgent,
)
from agents.project_intent_agent import ProjectIntent
from config import Settings, configure_logging, load_settings  # 載入集中管理的程式設定。
from database.db import MySQLDatabase
from models.meeting import MeetingRecord, MeetingStepRecord, ProposalMetrics
from models.project import Project, RequirementChange
from models.workspace import ProjectPriority
from repositories.mysql_guild_project_store import MySQLGuildProjectStore
from repositories.mysql_meeting_repository import MySQLMeetingRepository
from repositories.mysql_project_repository import MySQLProjectRepository
from repositories.mysql_workspace_repository import MySQLWorkspaceRepository, WorkspaceRepositoryError
from repositories.project_repository import (
    ProjectRepository,
    ProjectRepositoryError,
)
from services.llm_service import LLMResponse, LLMService, LLMServiceError  # 載入 Ollama 服務。
from services.meeting_manager import (
    FullMeetingResult,
    MeetingManager,
    MeetingManagerError,
    MeetingStepCallback,
    ReviewWorkflowResult,
)
from services.project_meeting_service import (
    ProjectMeetingError,
    ProjectMeetingService,
)
from services.project_intake import ProjectIntake, QUESTIONS
from views.decision_view import DecisionPresenter


logger = logging.getLogger(__name__)  # 使用模組名稱建立日誌，不記錄 Discord Token。
TRUNCATION_SUFFIX = "\n\n…（回覆過長，已截斷）"  # 模型回答太長時加在結尾。
AGENT_TITLES = {
    "PM Agent": "PM 需求拆解",
    "Research Agent": "Research 研究觀點",
    "Creative Agent": "Creative 創意提案",
    "Finance Agent": "Finance 財務評估",
}


class MeetingService(Protocol):
    """Bot 啟動專案會議時依賴的最小介面。"""

    async def start_project(self, guild_id: int, project_id: str) -> Project:
        """啟動 Guild 的指定專案並回傳更新後資料。"""

    async def prepare_project(self, guild_id: int, project_id: str) -> Project:
        """啟動新專案或接續同一個已進行中的專案。"""


class DiscussionManager(Protocol):
    """Bot 執行多輪 Agent 討論時需要的最小介面。"""

    async def start_first_round(
        self,
        guild_id: int,
        project_id: str,
        requirement: str,
        *,
        on_step: MeetingStepCallback | None = None,
        decision_owner_user_id: int | None = None,
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

    async def create_proposal_draft(
        self, guild_id: int, *, allow_first_round_only: bool = True,
    ) -> dict[str, object]:
        """建立或讀取 PM 整合草案。"""

    async def resume(self, guild_id: int) -> MeetingRecord:
        """恢復模型失敗後的會議步驟。"""

    async def get_proposal_metrics(self, guild_id: int) -> ProposalMetrics | None:
        """取得已保存的 PM 草案使用量統計。"""

    async def review_and_finalize(self, guild_id: int) -> ReviewWorkflowResult:
        """審查草案、執行至多一次修改並產生最終方案。"""

    async def run_full_meeting(
        self,
        guild_id: int,
        project_id: str,
        requirement: str,
        requirement_change: RequirementChange,
        *,
        on_step: MeetingStepCallback | None = None,
        decision_owner_user_id: int | None = None,
    ) -> FullMeetingResult:
        """從兩輪討論一路執行到最終方案。"""

    async def get_final_proposal_metrics(self, guild_id: int) -> ProposalMetrics | None:
        """取得最終方案的 PM 使用量統計。"""


class ProjectIntentClassifier(Protocol):
    """自然語言入口使用的可替換意圖分類器。"""

    async def classify(self, message: str) -> ProjectIntent:
        """判斷訊息是否要求建立新專案。"""


class MyBot(commands.Bot):
    """啟動前會同步斜線指令的 Discord Bot。"""

    def __init__(self, *args: object, database: MySQLDatabase | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.database = database

    async def setup_hook(self) -> None:  # Discord 登入前會自動執行一次。
        if self.database is not None:
            await self.database.open()
        if getattr(self, "restore_decisions", None) is not None:
            await self.restore_decisions()
        synced_commands = await self.tree.sync()  # 把 /help 同步到 Discord。
        logger.info("已同步 %s 個斜線指令", len(synced_commands))

    async def close(self) -> None:
        if self.database is not None:
            await self.database.close()
        await super().close()


async def resolve_repository_result(value: object) -> object:
    """相容 MySQL async Repository 與既有同步 JSON／測試替身。"""

    return await value if inspect.isawaitable(value) else value


def truncate_answer(answer: str, max_length: int) -> str:
    """截斷過長回答，並把提示文字計入最大長度。"""

    if len(answer) <= max_length:  # 回答未超過限制時不需要修改。
        return answer  # 直接回傳完整回答。

    # 先保留截斷提示需要的空間，確保組合後仍不超過 max_length。
    content_length = max_length - len(TRUNCATION_SUFFIX)
    return answer[:content_length] + TRUNCATION_SUFFIX  # 取前段回答再接上提示。


async def safe_followup_send(
    interaction: discord.Interaction,
    *args: object,
    ephemeral: bool = False,
    **kwargs: object,
) -> bool:
    """傳送公開 Followup；失敗時不影響會議工作流程與已保存資料。"""

    for attempt in range(1, 3):
        try:
            await interaction.followup.send(*args, ephemeral=ephemeral, **kwargs)
            return True
        except discord.HTTPException as error:
            logger.warning(
                "Discord Followup 傳送失敗 guild_id=%s attempt=%s error_type=%s",
                interaction.guild_id,
                attempt,
                type(error).__name__,
            )

    # 私密訊息不能降級為頻道公開訊息，避免資料意外曝光。
    if ephemeral:
        return False
    channel = getattr(interaction, "channel", None)
    send = getattr(channel, "send", None)
    if not callable(send):
        logger.warning("Discord Followup 失敗且無可用頻道 fallback guild_id=%s", interaction.guild_id)
        return False
    try:
        await send(*args, **kwargs)
        logger.info("Discord Followup 改由頻道傳送 guild_id=%s", interaction.guild_id)
        return True
    except discord.HTTPException as error:
        logger.warning(
            "Discord 頻道 fallback 傳送失敗 guild_id=%s error_type=%s",
            interaction.guild_id,
            type(error).__name__,
        )
        return False


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


def format_review_embed(
    review: dict[str, object], *, stage: str | None = None,
) -> discord.Embed:
    """將四項審查、修改要求與指定負責人顯示成 Discord Embed。"""

    status = str(review.get("status", "未知"))
    passed = status == "通過"
    embed = discord.Embed(
        title=f"{'✅' if passed else '🛠️'} {stage + ' ' if stage else ''}Review 審查：{status}",
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
            score = item.get("score")
            if isinstance(score, int) and not isinstance(score, bool):
                icon = "✅" if score >= 3 else "❌"
                lines.append(f"{icon} **{label} {score}/5**：{item.get('reason', '')}")
            else:
                # 舊會議仍使用 passed 布林值，顯示時保持相容。
                icon = "✅" if item.get("passed") is True else "❌"
                lines.append(f"{icon} **{label}**：{item.get('reason', '')}")
        embed.description = "\n".join(lines)[:4000]

    evaluation = review.get("quality_evaluation")
    if isinstance(evaluation, dict):
        outputs = evaluation.get("outputs", {})
        priority = evaluation.get("priority", {})
        risks = evaluation.get("risks", [])
        if isinstance(outputs, dict) and isinstance(priority, dict):
            risk_labels = "、".join(
                str(item.get("label", ""))
                for item in risks
                if isinstance(item, dict)
            ) if isinstance(risks, list) else ""
            embed.add_field(
                name=f"{stage}品質指標" if stage else "整體品質指標",
                value=(
                    f"**{outputs.get('quality_index', '未知')}/100｜{outputs.get('grade', '未知')}**\n"
                    f"優先目標：{priority.get('label', '未設定')}\n"
                    f"風險面向：{risk_labels or '無'}"
                )[:1024],
                inline=False,
            )
        explanation = evaluation.get("explanation")
        improvements = evaluation.get("improvements", [])
        improvement_lines = [
            f"• **{item.get('label', '')}**：{item.get('suggestion', '')}"
            for item in improvements
            if isinstance(item, dict)
        ] if isinstance(improvements, list) else []
        if isinstance(explanation, str):
            embed.add_field(
                name="計算說明與改善建議",
                value=(explanation + ("\n" + "\n".join(improvement_lines) if improvement_lines else ""))[:1024],
                inline=False,
            )

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
    llm_service: AgentLLMServiceProtocol,
    project_repository: ProjectRepository,
    meeting_service: MeetingService,
    discussion_manager: DiscussionManager,
    database: MySQLDatabase | None = None,
    workspace_repository: MySQLWorkspaceRepository | None = None,
    intent_classifier: ProjectIntentClassifier | None = None,
) -> MyBot:
    """使用外部傳入的設定與服務建立 Bot，方便正式執行和測試。"""

    intents = discord.Intents.default()  # 建立 Discord 預設事件權限。
    intents.message_content = True  # !hello 與 !ask 都需要讀取訊息內容。
    bot = MyBot(command_prefix="!", intents=intents, database=database)  # 指定文字指令使用 ! 開頭。
    intake = ProjectIntake()
    project_intent_classifier = intent_classifier or ProjectIntentAgent(llm_service)
    decision_service = getattr(discussion_manager, "decision_service", None)
    presenter = DecisionPresenter(bot, decision_service, format_proposal_embed, format_review_embed) if decision_service is not None else None
    bot.restore_decisions = presenter.restore if presenter else None

    def owner_kwargs(user_id):
        return {"decision_owner_user_id": user_id} if presenter else {}

    def meeting_requirement(project):
        if presenter is None:
            return "\n".join(project.requirements)
        return "\n".join((*project.requirements,
            f"硬性預算上限：{project.budget}", f"硬性交付期限：{project.deadline.isoformat()}",
            "驗收條件：" + "；".join(project.acceptance_criteria)))

    async def present_decision(guild_id, channel, status):
        if presenter is None or status == "legacy":
            return False
        record = await decision_service.get(guild_id)
        sent = await presenter.present(record, channel)
        if not sent:
            logger.warning("決策等待顯示，可由 /review 或 /decide 恢復 guild_id=%s", guild_id)
        return True


    async def send_intake(channel: discord.abc.Messageable, content: str | None = None, *, embed: discord.Embed | None = None) -> bool:
        try:
            await channel.send(content, embed=embed)
            return True
        except discord.HTTPException as error:
            logger.warning("專案入口訊息傳送失敗 error_type=%s", type(error).__name__)
            return False

    async def display_intake_step(
        channel: discord.abc.Messageable, current: int, total: int, step: MeetingStepRecord,
    ) -> None:
        for output in format_discussion_messages(
            step.agent_name, current, total, step.output_data or {},
            max_length=settings.max_meeting_message_length,
            round_number=step.round_number if step.round_number == 2 else None,
            execution_time_seconds=step.execution_time_seconds,
            input_characters=step.input_characters,
            output_characters=step.output_characters,
            max_prompt_characters=settings.max_meeting_prompt_length,
            max_response_characters=settings.max_meeting_response_length,
            prompt_tokens=step.prompt_tokens,
            completion_tokens=step.completion_tokens,
            max_output_tokens=step.max_output_tokens,
        ):
            await send_intake(channel, output)

    async def display_intake_draft(guild_id: int, channel: discord.abc.Messageable) -> None:
        draft = await discussion_manager.create_proposal_draft(
            guild_id, allow_first_round_only=True,
        )
        metrics = await resolve_repository_result(discussion_manager.get_proposal_metrics(guild_id))
        await send_intake(channel, embed=format_proposal_embed(
            draft, metrics=metrics,
            max_prompt_characters=settings.max_meeting_prompt_length,
            max_response_characters=settings.max_meeting_response_length,
        ))

    async def finalize_intake(guild_id: int, channel: discord.abc.Messageable) -> None:
        result = await discussion_manager.review_and_finalize(guild_id)
        await send_intake(channel, embed=format_review_embed(
            result.review, stage="草案" if result.revision_performed else None,
        ))
        if await present_decision(guild_id, channel, getattr(result, "decision_status", "legacy")):
            return
        if result.revision_performed:
            await send_intake(channel, f"🔧 已由 **{result.revision_agent_name}** 完成一次修改。")
            final_review = result.review.get("post_revision_review")
            if isinstance(final_review, dict):
                await send_intake(channel, embed=format_review_embed(final_review, stage="最終方案"))
        metrics = await resolve_repository_result(discussion_manager.get_final_proposal_metrics(guild_id))
        await send_intake(channel, embed=format_proposal_embed(
            result.final_proposal, metrics=metrics,
            max_prompt_characters=settings.max_meeting_prompt_length,
            max_response_characters=settings.max_meeting_response_length,
        ))

    @bot.listen("on_message")
    async def project_intake_message(message: discord.Message) -> None:
        """只在指定頻道回應明確專案意圖，保留原有 prefix commands。"""

        if (not settings.project_intake_channel_id
                or message.guild is None
                or message.channel.id != settings.project_intake_channel_id
                or message.author.bot
                or message.content.strip().startswith(("!", "/"))):
            return
        session = intake.get(message.guild.id, message.channel.id, message.author.id)
        content = message.content.strip()
        if session is None:
            session = intake.start(message.guild.id, message.channel.id, message.author.id, content)
            if session is None:
                try:
                    intent: ProjectIntent = await project_intent_classifier.classify(content)
                except AgentError as error:
                    logger.warning(
                        "專案入口意圖判斷失敗 guild_id=%s error_type=%s",
                        message.guild.id,
                        type(error).__name__,
                    )
                    await send_intake(
                        message.channel,
                        f"{message.author.mention} 我暫時無法判斷這是否為新專案。"
                        "請試著輸入「我要做一個網站」。",
                    )
                    return
                if intent.intent != "create_project" or intent.confidence < 0.8:
                    await send_intake(
                        message.channel,
                        f"{message.author.mention} 這裡是專案建立入口。"
                        "例如輸入「我要做一個網站」或「我要開一間咖啡廳」。",
                    )
                    return
                session = intake.start_from_title(
                    message.guild.id,
                    message.channel.id,
                    message.author.id,
                    intent.title or "新專案",
                    stage="intent_confirmation",
                )
                await send_intake(
                    message.channel,
                    f"{message.author.mention} 我理解你想建立「{session.fields['title']}」專案。"
                    "請回覆「確認」繼續，或「取消」。",
                )
                return
            question = QUESTIONS[session.missing_field() or "title"]
            await send_intake(message.channel, f"{message.author.mention} 收到，我會協助建立專案並在你確認後開會。\n{question}\n（隨時輸入「取消」）")
            return
        if session.running:
            return
        session.updated_at = time.monotonic()
        if content == "取消" and session.project_id is None:
            intake.cancel(session)
            await send_intake(message.channel, f"{message.author.mention} 已取消這份專案草稿；資料庫沒有新增專案。")
            return
        if session.stage == "intent_confirmation":
            if content != "確認":
                await send_intake(
                    message.channel,
                    f"{message.author.mention} 請回覆「確認」建立「{session.fields['title']}」"
                    "的草稿，或回覆「取消」。",
                )
                return
            session.stage = "collecting"
            session.updated_at = time.monotonic()
            await send_intake(
                message.channel,
                f"{message.author.mention} 已確認建立專案。"
                f"\n{QUESTIONS[session.missing_field() or 'title']}"
                "\n（隨時輸入「取消」）",
            )
            return
        if session.stage == "preview":
            if content == "直接定稿":
                session.running = True
                try:
                    await finalize_intake(message.guild.id, message.channel)
                except MeetingManagerError as error:
                    session.running = False
                    session.updated_at = time.monotonic()
                    await send_intake(message.channel, f"❌ 審查尚未完成：{error}。可回覆「直接定稿」重試。")
                    return
                intake.cancel(session)
                return
            if content.startswith("修改：") or content.startswith("修改:"):
                description = content.split("：", 1)[1].strip() if "：" in content else content.split(":", 1)[1].strip()
                if not description or len(description) > 2000:
                    await send_intake(message.channel, "❌ 修改內容需介於 1–2000 字。請用「修改：具體需求」。")
                    return
                add_change = getattr(project_repository, "add_requirement_change", None)
                if not callable(add_change) or session.project_id is None:
                    await send_intake(message.channel, "❌ 目前無法保存第二輪需求變更。")
                    return
                session.running = True
                try:
                    change = await resolve_repository_result(
                        add_change(message.guild.id, session.project_id, description)
                    )
                except ProjectRepositoryError as error:
                    session.running = False
                    await send_intake(message.channel, f"❌ 無法保存需求變更：{error}")
                    return
                session.change_id = change.id
                session.change = change
                session.stage = "second_round_failed"
                await send_intake(message.channel, f"已記錄變更 {change.id}，開始第二輪討論……")
                try:
                    await discussion_manager.start_second_round(
                        message.guild.id, change,
                        on_step=lambda current, total, step: display_intake_step(
                            message.channel, current, total, step,
                        ),
                    )
                    session.stage = "second_round_complete"
                    await display_intake_draft(message.guild.id, message.channel)
                    await finalize_intake(message.guild.id, message.channel)
                except MeetingManagerError as error:
                    session.running = False
                    session.updated_at = time.monotonic()
                    await send_intake(message.channel, f"❌ 第二輪或審查中斷：{error}。回覆「重試」接續；專案與變更已保存。")
                    return
                intake.cancel(session)
                return
            await send_intake(message.channel, f"{message.author.mention} 已看到第一輪草案。請回覆「直接定稿」進入選項與方案確認，或用「修改：具體需求」進入第二輪。")
            return
        if session.stage in {"first_round_failed", "first_round_complete", "second_round_failed", "second_round_complete"}:
            if content != "重試":
                await send_intake(message.channel, f"{message.author.mention} 專案 {session.project_id} 已保存；請回覆「重試」接續，不能用「取消」刪除。")
                return
            session.running = True
            try:
                if session.stage == "second_round_failed" and session.change is not None:
                    try:
                        await discussion_manager.start_second_round(
                            message.guild.id, session.change,
                            on_step=lambda current, total, step: display_intake_step(
                                message.channel, current, total, step,
                            ),
                        )
                    except MeetingManagerError:
                        # 第二輪若已部分執行，start_second_round 會拒絕重複套用；
                        # resume 只會接續已保存的失敗步驟。
                        await discussion_manager.resume(message.guild.id)
                elif session.stage == "first_round_failed":
                    await discussion_manager.resume(message.guild.id)
                session.stage = (
                    "first_round_complete" if session.stage.startswith("first")
                    else "second_round_complete"
                )
                await display_intake_draft(message.guild.id, message.channel)
                if session.stage == "first_round_complete":
                    session.stage = "preview"
                    await send_intake(message.channel, "這是第一輪草案。回覆「直接定稿」進入選項與方案確認，或用「修改：具體需求」進入第二輪。")
                else:
                    await finalize_intake(message.guild.id, message.channel)
                    intake.cancel(session)
                    return
            except MeetingManagerError as error:
                await send_intake(message.channel, f"❌ 接續失敗：{error}。請檢查已保存的會議狀態。")
            finally:
                session.running = False
                session.updated_at = time.monotonic()
            return
        if session.awaiting_confirmation:
            if content != "確認":
                await send_intake(message.channel, f"{message.author.mention} 請回覆「確認」開始建立並開會，或「取消」放棄。")
                return
            session.running = True
            if workspace_repository is None:
                session.running = False
                await send_intake(message.channel, "❌ 自然語言入口需要 MySQL 工作區設定。")
                return
            try:
                workspace = await workspace_repository.get_or_create(message.guild.id, message.guild.name)
            except WorkspaceRepositoryError as error:
                session.running = False
                logger.warning("專案入口無法讀取工作區 guild_id=%s: %s", message.guild.id, error)
                await send_intake(message.channel, "❌ 無法讀取工作區，稍後回覆「確認」重試。")
                return
            if workspace.current_project_id:
                session.running = False
                await send_intake(message.channel, f"❌ 這個伺服器已有進行中專案：{workspace.current_project_id}。草稿保留；目前不能啟動另一場會議。")
                return
            create = getattr(project_repository, "create_custom_project", None)
            if not callable(create):
                session.running = False
                await send_intake(message.channel, "❌ 目前沒有可用的自訂專案 Repository。")
                return
            try:
                project = await resolve_repository_result(create(message.guild.id, **session.fields))
            except ProjectRepositoryError as error:
                session.running = False
                logger.warning("專案入口建立失敗 guild_id=%s: %s", message.guild.id, error)
                await send_intake(message.channel, "❌ 專案建立失敗，草稿已保留；稍後回覆「確認」重試。")
                return
            session.project_id = project.id
            await send_intake(message.channel, f"✅ 已建立 {project.id}｜{project.title}。先進行第一輪討論，完成後給你看草案。")
            try:
                active_project = await meeting_service.prepare_project(message.guild.id, project.id)
            except ProjectMeetingError as error:
                logger.warning("專案入口無法開會 guild_id=%s project_id=%s: %s", message.guild.id, project.id, error)
                intake.cancel(session)
                await send_intake(message.channel, f"❌ 專案已建立，但會議未啟動：{error}。可稍後使用 `/start project_id:{project.id}`。")
                return
            session.stage = "first_round_failed"
            try:
                await discussion_manager.start_first_round(
                    message.guild.id, active_project.id, meeting_requirement(active_project),
                    **owner_kwargs(message.author.id),
                    on_step=lambda current, total, step: display_intake_step(
                        message.channel, current, total, step,
                    ),
                )
                session.stage = "first_round_complete"
                await display_intake_draft(message.guild.id, message.channel)
            except MeetingManagerError as error:
                session.running = False
                session.updated_at = time.monotonic()
                logger.warning("專案入口第一輪失敗 guild_id=%s project_id=%s: %s", message.guild.id, project.id, error)
                await send_intake(message.channel, f"❌ 第一輪或草案中斷：{error}。回覆「重試」接續；專案已保存。")
                return
            session.stage = "preview"
            session.running = False
            session.updated_at = time.monotonic()
            await send_intake(message.channel, "這是第一輪草案。回覆「直接定稿」進入選項與方案確認，或用「修改：具體需求」進入第二輪。")
            return
        error = intake.accept(session, content)
        if error:
            await send_intake(message.channel, f"{message.author.mention} ❌ {error}\n{QUESTIONS[session.missing_field() or 'title']}")
        elif session.awaiting_confirmation:
            summary = intake.summary(session)
            await send_intake(message.channel, f"{message.author.mention} 請核對資料：\n{truncate_answer(summary, 1500)}\n\n回覆「確認」後會建立專案並立即開會；回覆「取消」則放棄。")
        else:
            await send_intake(message.channel, f"{message.author.mention} {QUESTIONS[session.missing_field() or 'title']}")

    async def visible_projects(guild_id: int | None) -> list[Project]:
        method = getattr(project_repository, "list_for_guild", None)
        if guild_id is not None and callable(method):
            return await resolve_repository_result(method(guild_id))
        projects = await resolve_repository_result(project_repository.list_projects())
        return [item for item in projects if item.owner_guild_id is None or item.owner_guild_id == guild_id]

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
            name="/meeting <project_id> <change_id>",
            value="完成兩輪討論與審查，再選擇方案方向並批准。",
            inline=False,
        )
        if settings.project_intake_channel_id:
            embed.add_field(
                name="自然語言專案入口",
                value=f"到 <#{settings.project_intake_channel_id}> 輸入「我要做一個網站」，先看第一輪草案，再決定是否修改。",
                inline=False,
            )
        embed.add_field(name="/workspace", value="查看工作區設定與成果數。", inline=False)
        embed.add_field(name="/priority <priority>", value="設定下一場會議的預設優先目標。", inline=False)
        embed.add_field(
            name="/change <change_id>",
            value="套用一次需求變更並開始第二輪討論。",
            inline=False,
        )
        embed.add_field(
            name="/draft",
            value="由 PM 將已完成的一輪或兩輪討論整合成提案草案。",
            inline=False,
        )
        embed.add_field(name="/decide choice", value="選 A／B／C 影響方案；approve 批准、reject 駁回。只有會議啟動者可操作。", inline=False)
        embed.add_field(
            name="/review",
            value="審查草案並顯示決策狀態；新版方案批准後才會完成專案。",
            inline=False,
        )
        embed.set_footer(text=f"查詢者：{interaction.user.display_name}")  # 顯示查詢者名稱。
        # ephemeral=True 代表只有執行 /help 的使用者看得到訊息。
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="projects", description="顯示目前的專案清單")
    async def projects_command(interaction: discord.Interaction) -> None:
        """從 Repository 取得專案，再使用 Embed 顯示在 Discord。"""

        try:
            projects = await visible_projects(getattr(interaction, "guild_id", None))
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
                name=f"{project.id}｜{project.title}" + ("（自訂）" if project.owner_guild_id is not None else ""),
                value=format_project(project),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info("已顯示 %s 個專案", len(projects))

    @bot.tree.command(name="workspace", description="查看工作區、預設優先目標與成果數")
    async def workspace_command(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or workspace_repository is None:
            await interaction.response.send_message("❌ 請在已設定資料庫的 Discord 伺服器中使用。", ephemeral=True)
            return
        try:
            workspace = await workspace_repository.get_or_create(
                interaction.guild_id, interaction.guild.name if interaction.guild else "Discord 工作區"
            )
        except WorkspaceRepositoryError as error:
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return
        embed = discord.Embed(title=f"🏢 {workspace.name}", color=discord.Color.blue())
        embed.add_field(name="預設優先目標", value=workspace.default_priority.label)
        embed.add_field(name="目前專案", value=workspace.current_project_id or "無")
        embed.add_field(name="已完成自訂專案數", value=str(workspace.completed_custom_projects))
        embed.add_field(name="已產生最終方案的專案數", value=str(workspace.projects_with_final_proposal))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="priority", description="設定下一場會議的預設優先目標")
    @app_commands.choices(priority=[
        app_commands.Choice(name=item.label, value=item.value) for item in ProjectPriority
    ])
    async def priority_command(
        interaction: discord.Interaction, priority: app_commands.Choice[str]
    ) -> None:
        if interaction.guild_id is None or workspace_repository is None:
            await interaction.response.send_message("❌ 請在已設定資料庫的 Discord 伺服器中使用。", ephemeral=True)
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is None or not permissions.manage_guild:
            await interaction.response.send_message("❌ 只有伺服器管理員可設定優先目標。", ephemeral=True)
            return
        try:
            await workspace_repository.get_or_create(
                interaction.guild_id, interaction.guild.name if interaction.guild else "Discord 工作區"
            )
            workspace = await workspace_repository.set_default_priority(
                interaction.guild_id, ProjectPriority(priority.value)
            )
        except (WorkspaceRepositoryError, ValueError) as error:
            await interaction.response.send_message(f"❌ 無法設定優先目標：{error}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ 預設優先目標已設為 **{workspace.default_priority.label}**，下一場新會議生效。",
            ephemeral=True,
        )

    @bot.tree.command(name="meeting", description="完成兩輪討論、審查並等待使用者決策")
    @app_commands.describe(
        project_id="要執行的專案 ID，例如 PRJ-001",
        change_id="要套用的需求變更 ID，例如 CHG-001",
    )
    async def meeting_command(
        interaction: discord.Interaction,
        project_id: str,
        change_id: str,
    ) -> None:
        """以單一 Discord 指令執行完整會議，並隔離訊息傳送失敗。"""

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "❌ /meeting 只能在 Discord 伺服器中使用。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        normalized_project_id = project_id.strip().upper()
        normalized_change_id = change_id.strip().upper()
        try:
            projects = await visible_projects(interaction.guild_id)
        except ProjectRepositoryError as error:
            logger.error("/meeting 無法讀取專案資料：%s", error)
            await safe_followup_send(interaction, "❌ 無法讀取專案資料，請稍後再試。")
            return

        project = next(
            (item for item in projects if item.id.upper() == normalized_project_id),
            None,
        )
        if project is None:
            await safe_followup_send(
                interaction,
                f"❌ 找不到專案 ID：{normalized_project_id}。",
            )
            return
        requirement_change = next(
            (
                change
                for change in project.requirement_changes
                if change.id.upper() == normalized_change_id
            ),
            None,
        )
        if requirement_change is None:
            await safe_followup_send(
                interaction,
                f"❌ 找不到專案 {project.id} 的需求變更：{normalized_change_id}。",
            )
            return

        prepare_project = getattr(meeting_service, "prepare_project", None)
        try:
            active_project = (
                await prepare_project(interaction.guild_id, project.id)
                if callable(prepare_project)
                else await meeting_service.start_project(interaction.guild_id, project.id)
            )
        except ProjectMeetingError as error:
            logger.warning("/meeting 無法準備專案 guild_id=%s：%s", interaction.guild_id, error)
            await safe_followup_send(interaction, f"❌ {error}")
            return

        await safe_followup_send(
            interaction,
            f"🚀 開始完整會議：{active_project.id}｜{active_project.title}\n"
            f"需求變更：{requirement_change.id}｜{requirement_change.description}",
        )

        async def display_step(
            current: int,
            total: int,
            step: MeetingStepRecord,
        ) -> None:
            messages = format_discussion_messages(
                step.agent_name,
                current,
                total,
                step.output_data or {},
                max_length=settings.max_meeting_message_length,
                round_number=step.round_number if step.round_number == 2 else None,
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
                await safe_followup_send(interaction, message)

        try:
            result = await discussion_manager.run_full_meeting(
                interaction.guild_id,
                active_project.id,
                meeting_requirement(active_project),
                requirement_change,
                **(owner_kwargs(interaction.user.id) if presenter else {}),
                on_step=display_step,
            )
        except MeetingManagerError as error:
            logger.warning("/meeting 執行失敗 guild_id=%s：%s", interaction.guild_id, error)
            await safe_followup_send(
                interaction,
                f"❌ 完整會議失敗：{error}\n可重新執行相同指令以接續已保存的階段。",
            )
            return

        await safe_followup_send(interaction, embed=format_review_embed(
            result.review, stage="草案" if result.revision_performed else None,
        ))
        if await present_decision(interaction.guild_id, getattr(interaction, "channel", None), getattr(result, "decision_status", "legacy")):
            return
        if result.revision_performed:
            await safe_followup_send(
                interaction,
                f"🔧 已由 **{result.revision_agent_name}** 完成唯一一次修改。",
            )
            final_review = result.review.get("post_revision_review")
            if isinstance(final_review, dict):
                await safe_followup_send(
                    interaction, embed=format_review_embed(final_review, stage="最終方案"),
                )
        await safe_followup_send(
            interaction,
            embed=format_proposal_embed(
                result.final_proposal,
                metrics=await resolve_repository_result(
                    discussion_manager.get_final_proposal_metrics(interaction.guild_id)
                ),
                max_prompt_characters=settings.max_meeting_prompt_length,
                max_response_characters=settings.max_meeting_response_length,
            ),
        )
        logger.info("完整會議完成 guild_id=%s meeting_id=%s", interaction.guild_id, result.record.meeting_id)

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

        requirement = meeting_requirement(project)
        try:
            await discussion_manager.start_first_round(
                interaction.guild_id,
                project.id,
                requirement,
                **(owner_kwargs(interaction.user.id) if presenter else {}),
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
            projects = await visible_projects(interaction.guild_id)
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

    @bot.tree.command(name="draft", description="由 PM 整合已完成的討論並顯示提案摘要")
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
            await resolve_repository_result(metrics_getter(interaction.guild_id))
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

    @bot.tree.command(name="decide", description="選擇方案方向，或批准／駁回待確認方案")
    @app_commands.describe(choice="A／B／C 選擇方向；approve 批准；reject 駁回")
    @app_commands.choices(choice=[
        app_commands.Choice(name="A｜第一個選項", value="A"),
        app_commands.Choice(name="B｜第二個選項", value="B"),
        app_commands.Choice(name="C｜第三個選項", value="C"),
        app_commands.Choice(name="批准方案", value="approve"),
        app_commands.Choice(name="駁回、重新選擇", value="reject"),
    ])
    async def decide_command(interaction: discord.Interaction, choice: str) -> None:
        if interaction.guild_id is None or presenter is None:
            await interaction.response.send_message("❌ /decide 只能在有使用者決策會議的伺服器使用。", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            record = await decision_service.act(interaction.guild_id, interaction.user.id, choice)
        except MeetingManagerError as error:
            await safe_followup_send(interaction, f"❌ {error}", ephemeral=True)
            return
        await safe_followup_send(interaction, "✅ 操作已保存。", ephemeral=True)
        await presenter.present(record, interaction.channel)

    @bot.tree.command(name="review", description="審查草案並顯示使用者決策狀態")
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

        await interaction.followup.send(embed=format_review_embed(
            result.review, stage="草案" if result.revision_performed else None,
        ))
        if await present_decision(interaction.guild_id, getattr(interaction, "channel", None), getattr(result, "decision_status", "legacy")):
            return
        if result.revision_performed:
            await interaction.followup.send(
                "🔧 已將最高優先修改要求交給 "
                f"**{result.revision_agent_name}**，本會議修改次數已達 1/1。"
            )
            final_review = result.review.get("post_revision_review")
            if isinstance(final_review, dict):
                await interaction.followup.send(
                    embed=format_review_embed(final_review, stage="最終方案")
                )
        else:
            await interaction.followup.send("✅ 草案通過，未使用修改機會。")

        metrics = await resolve_repository_result(
            discussion_manager.get_final_proposal_metrics(interaction.guild_id)
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
    if not settings.db_password:
        raise RuntimeError("找不到 DB_PASSWORD，請先依 .env.example 設定 MySQL。")
    database = MySQLDatabase(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        connect_timeout_seconds=settings.db_connect_timeout_seconds,
    )
    project_repository = MySQLProjectRepository(database)
    workspace_repository = MySQLWorkspaceRepository(database)
    guild_project_store = MySQLGuildProjectStore(database)
    meeting_service = ProjectMeetingService(
        project_repository,
        guild_project_store,
    )
    meeting_repository = MySQLMeetingRepository(database)
    discussion_manager = MeetingManager(
        meeting_repository,
        PMAgent(llm_service),
        ResearchAgent(llm_service),
        CreativeAgent(llm_service),
        FinanceAgent(llm_service),
        ReviewAgent(llm_service),
        max_prompt_characters=settings.max_meeting_prompt_length,
        max_response_characters=settings.max_meeting_response_length,
        agent_max_attempts=settings.meeting_agent_max_attempts,
        retry_delay_seconds=settings.meeting_retry_delay_seconds,
        workspace_repository=workspace_repository,
    )
    bot = create_bot(
        settings,
        llm_service,
        project_repository,
        meeting_service,
        discussion_manager,
        database,
        workspace_repository,
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
