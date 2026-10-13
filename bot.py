"""Discord Bot 入口：建立指令並把 AI 問題交給 LLMService。"""

from __future__ import annotations

import logging  # 記錄 Bot 上線、指令執行與錯誤狀態。
import json  # 將 Agent 的結構化輸出整理成 Discord 可讀文字。
import time  # 顯示 Agent 與 PM 整合草案的實際處理時間。
import inspect
import asyncio
import sys
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
from config import Settings, load_settings  # 載入集中管理的程式設定。
from database.db import MySQLDatabase, DatabaseError
from core.errors import AppError, ErrorCode, ErrorContext, classify_error, safe_error_text
from core.error_handler import ErrorHandler
from core.logging_security import configure_safe_logging, register_secrets, log_error
from presentation.discord_delivery import (safe_defer, safe_followup_send, safe_channel_send, safe_message_edit, safe_original_response_edit, DELIVERY_ERRORS)
from services.project_submission_service import ProjectSubmissionService
from models.meeting import MeetingRecord, MeetingStatus, MeetingStepRecord, ProposalMetrics
from presentation.message_chunks import split_discord_message as split_message_chunks
from models.project import Project, RequirementChange
from models.workspace import ProjectPriority
from repositories.mysql_guild_project_store import MySQLGuildProjectStore
from repositories.mysql_meeting_repository import MySQLMeetingRepository
from repositories.mysql_project_repository import MySQLProjectRepository
from repositories.mysql_workspace_repository import MySQLWorkspaceRepository, WorkspaceRepositoryError
from repositories.project_repository import (
    ProjectRepository,
    ProjectRepositoryError,
    ProjectListing,
)
from services.llm_service import LLMResponse, LLMService, LLMServiceError  # 載入 Ollama 服務。
from services.workflow_events import WorkflowProgressCallback
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
from services.project_intake import ProjectIntake, QUESTIONS, format_requirement
from views.decision_view import DecisionPresenter
from presentation.channel_router import ChannelRouter
from presentation.progress import ProgressPresenter, build_status_embeds, build_intake_status_embeds
from presentation.embeds import (build_project_embeds, build_discussion_embeds,
    build_proposal_embeds, build_review_embeds)


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
        on_progress: WorkflowProgressCallback | None = None,
    ) -> MeetingRecord:
        """執行第一輪並在每位 Agent 完成時回報。"""

    async def start_second_round(
        self,
        guild_id: int,
        requirement_change: RequirementChange,
        *,
        on_step: MeetingStepCallback | None = None,
        on_progress: WorkflowProgressCallback | None = None,
    ) -> MeetingRecord:
        """套用一次需求變更並執行第二輪。"""

    async def create_proposal_draft(
        self, guild_id: int, *, allow_first_round_only: bool = True,
        on_progress: WorkflowProgressCallback | None = None,
    ) -> dict[str, object]:
        """建立或讀取 PM 整合草案。"""

    async def resume(self, guild_id: int, *, on_step: MeetingStepCallback | None = None, on_progress: WorkflowProgressCallback | None = None) -> MeetingRecord:
        """恢復模型失敗後的會議步驟。"""

    async def get_proposal_metrics(self, guild_id: int) -> ProposalMetrics | None:
        """取得已保存的 PM 草案使用量統計。"""

    async def review_and_finalize(self, guild_id: int, *, on_progress: WorkflowProgressCallback | None = None) -> ReviewWorkflowResult:
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
        on_progress: WorkflowProgressCallback | None = None,
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

    def __init__(self, *args: object, database: MySQLDatabase | None = None,
                 sync_guild_id: int = 0, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.database = database
        self.sync_guild_id = sync_guild_id
        self._restore_task = None
        self.error_handler = ErrorHandler()

    async def setup_hook(self) -> None:  # Discord 登入前會自動執行一次。
        try:
            await self._restore_data_services()
        except Exception as error:
            report = await self.error_handler.handle(error)
            if report.code != ErrorCode.DB_BAD_CONFIG:
                self._restore_task = asyncio.create_task(self._restore_when_ready())
        try:
            synced_commands = await asyncio.wait_for(self.tree.sync(), 30)
            if self.sync_guild_id:
                guild = discord.Object(id=self.sync_guild_id)
                self.tree.clear_commands(guild=guild)
                await asyncio.wait_for(self.tree.sync(guild=guild), 30)
            logger.info("已同步 %s 個斜線指令", len(synced_commands))
        except DELIVERY_ERRORS as error:
            # Sync failure need not terminate login; prefix commands and any
            # previously registered slash commands can still serve the guild.
            await self.error_handler.handle(error, ErrorContext(command='delivery', write_outcome='not_started'))

    async def _restore_data_services(self):
        if self.database is not None:
            await self.database.open()
        if getattr(self, 'restore_decisions', None) is not None:
            await self.restore_decisions()

    async def _restore_when_ready(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self._restore_data_services()
                return
            except Exception as error:
                report = await self.error_handler.handle(error)
                if report.code == ErrorCode.DB_BAD_CONFIG:
                    return

    async def on_error(self, event_method, *args, **kwargs):
        error = sys.exc_info()[1]
        if error is not None:
            await self.error_handler.handle(error)

    async def close(self) -> None:
        if self._restore_task is not None:
            self._restore_task.cancel()
            try:
                await self._restore_task
            except asyncio.CancelledError:
                pass
            self._restore_task = None
        try:
            if self.database is not None:
                await self.database.close()
        finally:
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



def split_discord_message(content: str, max_length: int) -> list[str]:
    """將完整內容切成多段，優先在換行處分割且不遺失文字。"""

    return split_message_chunks(content, max_length)


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
    status_store=None,
) -> MyBot:
    """使用外部傳入的設定與服務建立 Bot，方便正式執行和測試。"""

    intents = discord.Intents.default()  # 建立 Discord 預設事件權限。
    intents.message_content = True  # !hello 與 !ask 都需要讀取訊息內容。
    bot = MyBot(command_prefix="!", intents=intents, database=database,
        sync_guild_id=settings.discord_guild_id,
        allowed_mentions=discord.AllowedMentions.none())  # 指定文字指令使用 ! 開頭。
    intake = ProjectIntake(max_prompt_characters=settings.max_meeting_prompt_length)
    bot.project_intake = intake
    project_intent_classifier = intent_classifier or ProjectIntentAgent(llm_service)
    decision_service = getattr(discussion_manager, "decision_service", None)
    router = ChannelRouter(bot, settings, status_store)
    active_progress = {}
    published_proposal_metrics = {}

    async def latest_record(guild_id, *, strict=False):
        getter = getattr(discussion_manager, "_get_for_guild", None)
        if not callable(getter):
            return None
        try:
            record = await resolve_repository_result(getter(guild_id))
            return record if isinstance(record, MeetingRecord) else None
        except Exception as error:
            logger.warning("無法查詢狀態 guild_id=%s error_type=%s", guild_id, type(error).__name__)
            if strict:
                raise MeetingManagerError("暫時無法讀取會議狀態，請稍後重試。") from error
            return None

    async def run_workflow(method, *args, channel=None, **kwargs):
        guild_id = args[0]
        if isinstance(guild_id, MeetingRecord):
            guild_id = guild_id.guild_id
        progress = ProgressPresenter(router, guild_id, channel)
        # Preserve small test/adaptor protocols that do not expose progress.
        if "on_progress" in inspect.signature(method).parameters:
            async def update(event):
                active_progress[guild_id] = (progress, event)
                await progress.update(event)
                if event.state in {"retrying", "failed"}:
                    await router.send(guild_id, "dev_log", channel,
                        content=f"會議 {event.meeting_id}｜階段 {event.stage}｜{event.state}｜嘗試 {event.attempt}")
            kwargs["on_progress"] = update
        try:
            return await method(*args, **kwargs)
        finally:
            try:
                record = await latest_record(guild_id) if progress.latest is not None else None
                await progress.finish(record)
                if record is not None:
                    metrics = record.candidate_proposal_metrics or record.final_proposal_metrics or record.proposal_metrics
                    signature = repr(metrics)
                    published = published_proposal_metrics.get(record.meeting_id)
                    if published is None and status_store is not None:
                        published = status_store.metric_signature(guild_id, record.meeting_id)
                    if metrics is not None and published != signature:
                        # 使用量不是憑證；避免「Token 值」被安全遮罩誤認為登入 Token。
                        sent = await router.send(guild_id, "dev_log", channel, content=f"專案 {record.project_id}｜會議 {record.meeting_id}｜PM 整合耗時 {metrics.execution_time_seconds} 秒｜輸入／輸出字元 {metrics.input_characters}／{metrics.output_characters}｜推論用量（輸入／輸出）：{metrics.prompt_tokens}／{metrics.completion_tokens}")
                        if sent is not None:
                            published_proposal_metrics[record.meeting_id] = signature
                            if status_store is not None:
                                status_store.remember_metrics(guild_id, record.meeting_id, signature)
            except Exception as error:
                logger.warning("流程收尾通知失敗 guild_id=%s error_type=%s", guild_id, type(error).__name__)
            finally:
                if active_progress.get(guild_id, (None,))[0] is progress:
                    active_progress.pop(guild_id, None)

    async def send_business_channel(guild_id, channel, kind, data, *, stage=None, next_step=None):
        record = await latest_record(guild_id)
        pages = (build_review_embeds(data, record, stage=stage or "草案") if kind == "review"
            else build_proposal_embeds(data, record, stage=stage or "PM 草案", next_step=next_step))
        for embed in pages:
            await router.send(guild_id, "meeting", channel, embed=embed)

    async def send_business(interaction, kind, data, *, stage=None):
        if getattr(interaction, "channel", None) is None:
            pages = build_review_embeds(data, stage=stage or "草案") if kind == "review" else build_proposal_embeds(data, stage=stage or "PM 草案")
            for embed in pages:
                await safe_followup_send(interaction, embed=embed)
        else:
            await send_business_channel(interaction.guild_id, interaction.channel, kind, data, stage=stage)

    async def display_step_embeds(guild_id, channel, current, total, step, interaction=None):
        record = await latest_record(guild_id)
        for embed in build_discussion_embeds(step, record, current=current, total=total):
            if channel is None and interaction is not None:
                await safe_followup_send(interaction, embed=embed)
            else:
                await router.send(guild_id, "meeting", channel, embed=embed)
        stats = (f"Agent：{step.agent_name}｜第 {step.round_number} 輪｜耗時 {step.execution_time_seconds} 秒"
            f"\n輸入／輸出字元：{step.input_characters}／{step.output_characters}"
            f"｜推論用量（輸入／輸出）：{step.prompt_tokens}／{step.completion_tokens}"
            f"\n專案 {record.project_id if record else '?'}｜會議 {record.meeting_id if record else '?'}")
        await router.send(guild_id, "dev_log", channel, content=stats)

    presenter = DecisionPresenter(bot, decision_service, format_proposal_embed, format_review_embed,
        router=router, run_workflow=run_workflow) if decision_service is not None else None
    bot.restore_decisions = presenter.restore if presenter else None

    def owner_kwargs(user_id):
        return {"decision_owner_user_id": user_id} if presenter else {}

    def meeting_requirement(project):
        if presenter is None:
            return "\n".join(project.requirements)
        return format_requirement(vars(project))

    async def present_decision(guild_id, channel, status):
        if presenter is None or status == "legacy":
            return False
        record = await decision_service.get(guild_id)
        sent = await presenter.present(record, channel)
        if not sent:
            logger.warning("決策等待顯示，可由 /review 或 /decide 恢復 guild_id=%s", guild_id)
            await router.send(guild_id, "meeting", channel, content="❌ 決策資料已保存，但畫面未完整送出。請用 /review 重新顯示，或 /decide 操作目前選項。")
        return True


    async def send_intake(channel: discord.abc.Messageable, content: str | None = None, *, embed: discord.Embed | None = None) -> bool:
        try:
            for chunk in split_message_chunks(content or "", min(settings.max_meeting_message_length, 1900)):
                await channel.send(chunk or None, embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return True
        except DELIVERY_ERRORS as error:
            logger.warning("專案入口訊息傳送失敗 error_type=%s", type(error).__name__)
            return False

    async def display_intake_step(
        channel: discord.abc.Messageable, current: int, total: int, step: MeetingStepRecord,
    ) -> None:
        await display_step_embeds(getattr(getattr(channel, "guild", None), "id", 0), channel, current, total, step)

    async def display_intake_draft(guild_id: int, channel: discord.abc.Messageable) -> None:
        draft = await run_workflow(discussion_manager.create_proposal_draft, guild_id, allow_first_round_only=True, channel=channel)
        await send_business_channel(guild_id, channel, 'proposal', draft, next_step=f'回到 {getattr(channel, "mention", "原 requests 頻道")} 回覆「直接定稿」或「修改：具體需求」。')

    async def finalize_intake(guild_id: int, channel: discord.abc.Messageable) -> None:
        result = await run_workflow(discussion_manager.review_and_finalize, guild_id, channel=channel)
        await send_business_channel(guild_id, channel, 'review', result.review, stage="草案" if result.revision_performed else None)
        if await present_decision(guild_id, channel, getattr(result, "decision_status", "legacy")):
            return
        if result.revision_performed:
            await send_intake(channel, f"🔧 已由 **{result.revision_agent_name}** 完成一次修改。")
            final_review = result.review.get("post_revision_review")
            if isinstance(final_review, dict):
                await send_business_channel(guild_id, channel, 'review', final_review, stage="最終方案")
        await send_business_channel(guild_id, channel, 'proposal', result.final_proposal, stage='已完成方案（舊流程）')

    submission_service = ProjectSubmissionService(project_repository)

    @bot.listen("on_message")
    async def project_intake_message(message: discord.Message) -> None:
        try:
            await process_project_intake(message)
        except Exception as error:
            session = intake.get(message.guild.id, message.channel.id, message.author.id) if message.guild else None
            if session is not None:
                session.running = False
            await bot.error_handler.handle(error, ErrorContext(guild_id=getattr(message.guild, 'id', None),
                write_outcome='unknown'), channel=message.channel)

    async def process_project_intake(message: discord.Message) -> None:
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
        if session.stage == 'change_outcome_unknown':
            if content == '查證':
                try:
                    await submission_service.reconcile_change(session)
                except Exception as error:
                    await bot.error_handler.handle(error, ErrorContext(guild_id=message.guild.id,
                        write_outcome='unknown'), channel=message.channel)
                    return
                await send_intake(message.channel, f'✅ 已查證變更 {session.change_id} 保存成功；回覆「重試」接續第二輪。')
            else:
                await send_intake(message.channel, '❌ 需求變更保存結果尚未確認。請回覆「查證」，不要再次新增變更。')
            return
        if session.stage == 'write_outcome_unknown':
            if content == '查證':
                try:
                    await submission_service.reconcile_submission(session)
                except Exception as error:
                    await bot.error_handler.handle(error, ErrorContext(guild_id=message.guild.id,
                        write_outcome='unknown'), channel=message.channel)
                    return
                await send_intake(message.channel, f'✅ 專案已保存；使用 /start project_id:{session.project_id} 接續。')
            else:
                await send_intake(message.channel, '❌ 建立結果尚未確認。請回覆「查證」或先 /projects 查詢；不要重複建立。')
            return
        if session.stage == 'submission_verified':
            await send_intake(message.channel, f'專案已保存；使用 /start project_id:{session.project_id} 接續。')
            return
        if session.project_id is None and content.startswith(('修改需求：', '修改驗收：')):
            field = 'requirements' if content.startswith('修改需求：') else 'acceptance_criteria'
            error = intake.update_text_field(session, field, content.split('：', 1)[1])
            if error:
                await send_intake(message.channel, f'❌ {error}')
            elif session.awaiting_confirmation:
                await send_intake(message.channel, f'請核對更新資料：\n{truncate_answer(intake.summary(session), 1500)}\n回覆「確認」開始。')
            else:
                await send_intake(message.channel, QUESTIONS[session.missing_field() or 'acceptance_criteria'])
            return
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
                    await bot.error_handler.handle(error, ErrorContext(guild_id=message.guild.id, write_outcome="unknown"), channel=message.channel)
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
                        submission_service.submit_change(session, description)
                    )
                except ProjectRepositoryError as error:
                    session.running = False
                    await bot.error_handler.handle(error, ErrorContext(guild_id=message.guild.id, write_outcome="unknown"), channel=message.channel)
                    return
                session.change_id = change.id
                session.change = change
                session.stage = "second_round_failed"
                await send_intake(message.channel, f"已記錄變更 {change.id}，開始第二輪討論……")
                try:
                    await run_workflow(discussion_manager.start_second_round, message.guild.id, change, on_step=lambda current, total, step: display_intake_step(
                            message.channel, current, total, step,
                        ), channel=message.channel)
                    session.stage = "second_round_complete"
                    await display_intake_draft(message.guild.id, message.channel)
                    await finalize_intake(message.guild.id, message.channel)
                except MeetingManagerError as error:
                    session.running = False
                    session.updated_at = time.monotonic()
                    await bot.error_handler.handle(error, ErrorContext(guild_id=message.guild.id, write_outcome="unknown"), channel=message.channel)
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
                    record = await latest_record(message.guild.id, strict=True)
                    applied = record.applied_requirement_change if record is not None else None
                    if applied is not None:
                        if record.project_id != session.project_id or applied.id != session.change.id:
                            raise MeetingManagerError(code=ErrorCode.INVALID_STATE)
                        if record.status != MeetingStatus.COMPLETED:
                            await run_workflow(discussion_manager.resume, message.guild.id,
                                on_step=lambda current, total, step: display_intake_step(message.channel, current, total, step),
                                channel=message.channel)
                    else:
                        await run_workflow(discussion_manager.start_second_round, message.guild.id, session.change, on_step=lambda current, total, step: display_intake_step(
                                message.channel, current, total, step,
                            ), channel=message.channel)
                elif session.stage == "first_round_failed":
                    record = await latest_record(message.guild.id, strict=True)
                    if record is None or record.project_id != session.project_id:
                        active = await meeting_service.prepare_project(message.guild.id, session.project_id)
                        await run_workflow(discussion_manager.start_first_round, message.guild.id,
                            active.id, meeting_requirement(active), **owner_kwargs(message.author.id),
                            on_step=lambda current, total, step: display_intake_step(message.channel, current, total, step),
                            channel=message.channel)
                    elif record.status != MeetingStatus.COMPLETED:
                        await run_workflow(discussion_manager.resume, message.guild.id,
                            on_step=lambda current, total, step: display_intake_step(message.channel, current, total, step),
                            channel=message.channel)
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
                await bot.error_handler.handle(error, ErrorContext(guild_id=message.guild.id, write_outcome="unknown"), channel=message.channel)
            finally:
                session.running = False
                session.updated_at = time.monotonic()
            return
        if session.awaiting_confirmation:
            if content != "確認":
                await send_intake(message.channel, f"{message.author.mention} 請回覆「確認」開始建立並開會，或「取消」放棄。")
                return
            budget_error = intake.validate_prompt(session)
            if budget_error:
                await send_intake(message.channel, f'❌ {budget_error} 可用「修改需求：內容」或「修改驗收：內容」調整。')
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
                logger.warning("專案入口無法讀取工作區 guild_id=%s: %s", message.guild.id, type(error).__name__)
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
                project = await submission_service.submit(session)
            except ProjectRepositoryError as error:
                session.running = False
                logger.warning("專案入口建立失敗 guild_id=%s: %s", message.guild.id, type(error).__name__)
                await bot.error_handler.handle(error, ErrorContext(guild_id=message.guild.id,
                    write_outcome='unknown' if session.stage == 'write_outcome_unknown' else 'not_started'), channel=message.channel)
                return
            session.project_id = project.id
            meeting_target = router.resolve(message.guild.id, "meeting", message.channel)
            status_target = router.resolve(message.guild.id, "status", message.channel)
            await send_intake(message.channel, f"討論與決策請看 {getattr(meeting_target, 'mention', '本頻道')}，進度請看 {getattr(status_target, 'mention', '本頻道')}。草案後的「直接定稿／修改」仍在本頻道回覆；隨時可用 /status。")
            await send_intake(message.channel, f"✅ 已建立 {project.id}｜{project.title}。先進行第一輪討論，完成後給你看草案。")
            try:
                active_project = await meeting_service.prepare_project(message.guild.id, project.id)
            except ProjectMeetingError as error:
                logger.warning("專案入口無法開會 guild_id=%s project_id=%s: %s", message.guild.id, project.id, type(error).__name__)
                intake.cancel(session)
                await bot.error_handler.handle(error, ErrorContext(guild_id=message.guild.id, write_outcome="unknown"), channel=message.channel)
                return
            session.stage = "first_round_failed"
            try:
                await run_workflow(discussion_manager.start_first_round, message.guild.id, active_project.id, meeting_requirement(active_project), **owner_kwargs(message.author.id), on_step=lambda current, total, step: display_intake_step(
                        message.channel, current, total, step,
                    ), channel=message.channel)
                session.stage = "first_round_complete"
                await display_intake_draft(message.guild.id, message.channel)
            except MeetingManagerError as error:
                session.running = False
                session.updated_at = time.monotonic()
                logger.warning("專案入口第一輪失敗 guild_id=%s project_id=%s: %s", message.guild.id, project.id, type(error).__name__)
                await bot.error_handler.handle(error, ErrorContext(guild_id=message.guild.id, write_outcome="unknown"), channel=message.channel)
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

    async def visible_project_listing(guild_id: int | None) -> ProjectListing:
        method = getattr(project_repository, "list_for_guild_with_issues", None)
        if guild_id is not None and callable(method):
            return await resolve_repository_result(method(guild_id))
        return ProjectListing(await visible_projects(guild_id))

    @bot.event  # 將下方函式註冊為 Bot 上線事件。
    async def on_ready() -> None:  # Bot 登入並準備完成時觸發。
        logger.info("機器人已上線")

    @bot.command(name="hello")  # 註冊 !hello 文字指令。
    async def hello(ctx: commands.Context) -> None:  # ctx 包含使用者、頻道等指令資訊。
        """回覆並標記執行指令的使用者。"""

        await ctx.send(f"你好，{ctx.author.mention}！")  # mention 會在 Discord 標記該使用者。

    @bot.tree.command(name="hello", description="讓機器人向你打招呼")
    async def slash_hello(interaction: discord.Interaction) -> None:
        """示範 Slash Command 的 defer 與 Followup 回覆流程。"""

        # 先回應 Discord，避免較久的工作讓 Interaction 在三秒內逾時。
        # thinking=True 會顯示「機器人正在思考」，ephemeral=True 代表只有本人看得到。
        if not await safe_defer(interaction, thinking=True, ephemeral=True):
            return

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
        processing_message = await safe_channel_send(ctx, "⏳ Qwen 正在處理你的問題，請稍候……")
        if processing_message is None:
            return
        logger.info("開始處理 !ask（輸入長度=%s）", len(question))  # 只記錄長度，不記錄問題。

        try:
            result = await llm_service.chat(question)  # 非同步等待 Qwen 產生回答。
        except LLMServiceError as error:
            # LLMService 已將未啟動、逾時等底層例外轉成安全訊息。
            logger.warning("!ask 執行失敗 exception_type=%s", type(error).__name__)
            await safe_message_edit(processing_message, content=safe_error_text(error, ErrorContext(command="ask")))  # 將處理中訊息改成錯誤原因。
            return  # 錯誤發生後停止後續流程。

        # 從 LLMResponse 取出文字，並限制 Discord 訊息長度。
        answer = truncate_answer(result.content, settings.max_ask_output_length)
        await safe_message_edit(processing_message, content=answer)  # 將原本的處理中訊息改成模型回答。
        logger.info("!ask 處理完成（輸出長度=%s）", len(answer))  # 不記錄回答內容。

    @bot.tree.command(name="help", description="顯示機器人指令說明")
    async def help_command(interaction: discord.Interaction) -> None:
        """使用 Embed 顯示目前可以使用的指令。"""

        embed = discord.Embed(  # 建立格式化的 Discord 說明卡片。
            title="🤖 機器人指令說明",
            description="需求確認 → Agent 討論 → PM 草案 → 審查 → 你選擇方向 → 重新評分 → 你批准或駁回。批准後專案才完成。",
            color=discord.Color.green(),
        )
        entry = f"<#{settings.project_intake_channel_id}>" if settings.project_intake_channel_id else "/projects 選案後用 /start"
        meeting_channel = router.resolve(interaction.guild_id, "meeting", getattr(interaction, "channel", None)) if getattr(interaction, "guild_id", None) else None
        status_channel = router.resolve(interaction.guild_id, "status", getattr(interaction, "channel", None)) if getattr(interaction, "guild_id", None) else None
        entry_action = (f"前往 {entry} 輸入「我要做一個網站」，按提示補充需求後回覆「確認」。"
            if settings.project_intake_channel_id else "自然語言入口尚未啟用。先用 /projects 選案，再用 /start project_id:專案ID。")
        embed.add_field(name="在哪裡操作", value=f"Agent 討論與決策：{getattr(meeting_channel, 'mention', '原操作頻道')}；進度：{getattr(status_channel, 'mention', '原操作頻道')}。自然語言草案回覆留在原 requests 頻道。", inline=False)

        embed.add_field(name="目前該做什麼", value="/status 顯示目前階段、操作者、操作位置與下一步。只有會議啟動者能選擇與批准。", inline=False)
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
            name="/cleanup",
            value="清理目前頻道最近 100 則由 Bot 發送的訊息；需要管理訊息權限。",
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
        embed.add_field(name="/resume", value="接續失敗的討論步驟；審查／決策中斷用 /review 或 /decide。", inline=False)
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
        embed.add_field(name="錯誤與恢復", value="錯誤卡會顯示保存狀態與查詢代碼。先 /status 確認；討論用 /resume，審查用 /review。建立或批准結果不明時先查證，不要重送。", inline=False)
        embed.set_footer(text=f"查詢者：{interaction.user.display_name}")  # 顯示查詢者名稱。
        # ephemeral=True 代表只有執行 /help 的使用者看得到訊息。
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="projects", description="顯示目前的專案清單")
    async def projects_command(interaction: discord.Interaction) -> None:
        """從 Repository 取得專案，再使用 Embed 顯示在 Discord。"""

        if not await safe_defer(interaction, ephemeral=True):
            return
        try:
            listing = await visible_project_listing(getattr(interaction, "guild_id", None))
            projects = listing.projects
        except ProjectRepositoryError as error:
            logger.error("讀取專案資料失敗：%s", type(error).__name__)
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="not_started"), interaction=interaction)
            return

        summary = discord.Embed(title="📁 專案清單", description=f"目前共有 {len(projects)} 個專案。", color=discord.Color.blue())
        if listing.invalid_count:
            # 不顯示異常資料原文；舊紀錄保留，等待管理員查核與補齊。
            summary.add_field(name="⚠️ 資料待修復", value=(
                f"另有 {listing.invalid_count} 筆專案資料不完整，已暫時排除顯示。"
                "請管理員檢查需求、驗收條件及其他欄位；紀錄未刪除。"
            ), inline=False)
        await safe_followup_send(interaction, embed=summary, ephemeral=True)
        for project in projects:
            for embed in build_project_embeds(project):
                await safe_followup_send(interaction, embed=embed, ephemeral=True)
        logger.info("已顯示 %s 個專案", len(projects))

    @bot.tree.command(name="workspace", description="查看工作區、預設優先目標與成果數")
    async def workspace_command(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or workspace_repository is None:
            await interaction.response.send_message("❌ 請在已設定資料庫的 Discord 伺服器中使用。", ephemeral=True)
            return
        if not await safe_defer(interaction, ephemeral=True):
            return
        try:
            workspace = await workspace_repository.get_or_create(
                interaction.guild_id, interaction.guild.name if interaction.guild else "Discord 工作區"
            )
        except WorkspaceRepositoryError as error:
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
            return
        embed = discord.Embed(title=f"🏢 {workspace.name}", color=discord.Color.blue())
        embed.add_field(name="預設優先目標", value=workspace.default_priority.label)
        embed.add_field(name="目前專案", value=workspace.current_project_id or "無")
        embed.add_field(name="已完成自訂專案數", value=str(workspace.completed_custom_projects))
        embed.add_field(name="已產生最終方案的專案數", value=str(workspace.projects_with_final_proposal))
        await safe_followup_send(interaction, embed=embed, ephemeral=True)

    @bot.tree.command(name="resume", description="接續討論階段失敗的 Agent 步驟")
    async def resume_command(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ /resume 只能在伺服器使用。", ephemeral=True)
            return
        if not await safe_defer(interaction, thinking=True):
            return
        await safe_followup_send(interaction, "⏳ 正在接續已保存的討論；可用 /status 查看進度。")
        try:
            await run_workflow(discussion_manager.resume, interaction.guild_id,
                on_step=lambda current, total, step: display_step_embeds(interaction.guild_id,
                    getattr(interaction, "channel", None), current, total, step, interaction),
                channel=getattr(interaction, "channel", None))
        except MeetingManagerError as error:
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
            return
        await safe_followup_send(interaction, "✅ 討論已接續完成。下一步：/draft，再 /review。")

    @bot.tree.command(name="status", description="查看目前階段、操作者與下一步")
    async def status_command(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ /status 只能在伺服器使用。", ephemeral=True)
            return
        if not await safe_defer(interaction, thinking=True, ephemeral=True):
            return
        user_id = getattr(interaction.user, "id", None)
        session = intake.get(interaction.guild_id, settings.project_intake_channel_id, user_id)
        if session is not None and not session.running and (session.project_id is None or session.stage == "preview"):
            for embed in build_intake_status_embeds(session):
                await safe_followup_send(interaction, embed=embed, ephemeral=True)
            return
        try:
            record = await latest_record(interaction.guild_id, strict=True)
        except MeetingManagerError as error:
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
            return
        locks = getattr(discussion_manager, "_guild_locks", {})
        running = bool(locks.get(interaction.guild_id) and locks[interaction.guild_id].locked())
        running = running or interaction.guild_id in getattr(discussion_manager, "_full_workflow_guilds", set())
        active = active_progress.get(interaction.guild_id)
        pages = build_status_embeds(record, is_running=running, event=active[1] if active else None)
        # 每次指令都有自己的等待訊息，必須回覆這次互動才能結束「思考中」。
        # 私人查詢不更新公開狀態卡，也不復用另一個 Interaction 的訊息。
        for page in pages:
            await safe_followup_send(interaction, embed=page, ephemeral=True)

    @bot.tree.command(name="cleanup", description="清理目前頻道中 Bot 發送的舊訊息")
    async def cleanup_command(interaction: discord.Interaction) -> None:
        """只刪除目前頻道的 Bot 訊息，保留使用者訊息與資料庫紀錄。"""

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "❌ /cleanup 只能在伺服器頻道使用。", ephemeral=True
            )
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is None or not permissions.manage_messages:
            await interaction.response.send_message(
                "❌ 只有具備「管理訊息」權限的人可以清理頻道。", ephemeral=True
            )
            return
        channel = getattr(interaction, "channel", None)
        if channel is None or not hasattr(channel, "history"):
            await interaction.response.send_message(
                "❌ 目前頻道不支援訊息清理。", ephemeral=True
            )
            return

        if not await safe_defer(interaction, ephemeral=True):
            return
        bot_user = bot.user
        deleted = 0
        try:
            # 只掃描最近 100 則，避免一次操作時間過長；不刪除使用者訊息。
            async for message in channel.history(limit=100):
                if bot_user is not None and message.author.id == bot_user.id:
                    try:
                        await message.delete()
                        deleted += 1
                    except DELIVERY_ERRORS:
                        logger.warning(
                            "清理訊息失敗 channel_id=%s message_id=%s",
                            getattr(channel, "id", None), message.id,
                        )
        except DELIVERY_ERRORS as error:
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
            return
        await interaction.followup.send(
            f"✅ 已清理目前頻道中的 {deleted} 則 Bot 訊息；資料庫紀錄未刪除。",
            ephemeral=True,
        )

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
        if not await safe_defer(interaction, ephemeral=True):
            return
        try:
            await workspace_repository.get_or_create(
                interaction.guild_id, interaction.guild.name if interaction.guild else "Discord 工作區"
            )
            workspace = await workspace_repository.set_default_priority(
                interaction.guild_id, ProjectPriority(priority.value)
            )
        except (WorkspaceRepositoryError, ValueError) as error:
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
            return
        await safe_followup_send(interaction,
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

        if not await safe_defer(interaction, thinking=True):
            return
        normalized_project_id = project_id.strip().upper()
        normalized_change_id = change_id.strip().upper()
        try:
            projects = await visible_projects(interaction.guild_id)
        except ProjectRepositoryError as error:
            logger.error("/meeting 無法讀取專案資料：%s", type(error).__name__)
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
            return

        project = next(
            (item for item in projects if item.id.upper() == normalized_project_id),
            None,
        )
        if project is None:
            await bot.error_handler.handle(AppError(code=ErrorCode.PROJECT_NOT_FOUND), ErrorContext(guild_id=interaction.guild_id), interaction=interaction)
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
            await bot.error_handler.handle(AppError(code=ErrorCode.PROJECT_NOT_FOUND), ErrorContext(guild_id=interaction.guild_id), interaction=interaction)
            return

        prepare_project = getattr(meeting_service, "prepare_project", None)
        try:
            active_project = (
                await prepare_project(interaction.guild_id, project.id)
                if callable(prepare_project)
                else await meeting_service.start_project(interaction.guild_id, project.id)
            )
        except ProjectMeetingError as error:
            logger.warning("/meeting 無法準備專案 guild_id=%s：%s", interaction.guild_id, type(error).__name__)
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
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
            await display_step_embeds(interaction.guild_id, getattr(interaction, "channel", None), current, total, step, interaction)

        try:
            result = await run_workflow(discussion_manager.run_full_meeting, interaction.guild_id, active_project.id, meeting_requirement(active_project), requirement_change, **owner_kwargs(interaction.user.id) if presenter else {}, on_step=display_step, channel=getattr(interaction, "channel", None))
        except MeetingManagerError as error:
            logger.warning("/meeting 執行失敗 guild_id=%s：%s", interaction.guild_id, type(error).__name__)
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
            return

        await send_business(interaction, 'review', result.review, stage="草案" if result.revision_performed else None)
        if await present_decision(interaction.guild_id, getattr(interaction, "channel", None), getattr(result, "decision_status", "legacy")):
            return
        if result.revision_performed:
            await safe_followup_send(
                interaction,
                f"🔧 已由 **{result.revision_agent_name}** 完成唯一一次修改。",
            )
            final_review = result.review.get("post_revision_review")
            if isinstance(final_review, dict):
                await send_business(interaction, 'review', final_review, stage="最終方案")
        await send_business(interaction, 'proposal', result.final_proposal, stage='已完成方案（舊流程）')
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
        if not await safe_defer(interaction, thinking=True):
            return
        try:
            # 已占用但尚未建立會議的同一專案，可安全重試初始化。
            previous = await latest_record(interaction.guild_id, strict=True)
            if previous is not None and previous.final_proposal is None:
                raise ProjectMeetingError(code=ErrorCode.INVALID_STATE)
            prepare = getattr(meeting_service, 'prepare_project', None)
            project = await (prepare(interaction.guild_id, project_id) if callable(prepare)
                else meeting_service.start_project(interaction.guild_id, project_id))
        except (ProjectMeetingError, MeetingManagerError) as error:
            logger.warning("/start 失敗 guild_id=%s：%s", interaction.guild_id, type(error).__name__)
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
            return

        embed = discord.Embed(
            title="✅ 專案會議已啟動",
            description=f"{project.id}｜{project.title}",
            color=discord.Color.green(),
        )
        embed.add_field(name="狀態", value=project.status.label, inline=True)
        embed.add_field(name="期限", value=project.deadline.isoformat(), inline=True)
        await safe_followup_send(interaction, embed=embed)

        async def display_agent_step(
            current: int,
            total: int,
            step: MeetingStepRecord,
        ) -> None:
            await display_step_embeds(interaction.guild_id, getattr(interaction, "channel", None), current, total, step, interaction)

        requirement = meeting_requirement(project)
        try:
            await run_workflow(discussion_manager.start_first_round, interaction.guild_id, project.id, requirement, **owner_kwargs(interaction.user.id) if presenter else {}, on_step=display_agent_step, channel=getattr(interaction, "channel", None))
        except MeetingManagerError as error:
            logger.warning(
                "第一輪討論失敗 guild_id=%s project_id=%s：%s",
                interaction.guild_id,
                project.id,
                type(error).__name__,
            )
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
            return

        await safe_followup_send(interaction, "✅ 第一輪討論完成。下一步：/draft 整合草案；若要第二輪，先用 /change。")
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

        if not await safe_defer(interaction, thinking=True):
            return
        normalized_change_id = change_id.strip().upper()
        try:
            projects = await visible_projects(interaction.guild_id)
        except ProjectRepositoryError as error:
            logger.error("讀取需求變更失敗：%s", type(error).__name__)
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
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
            await bot.error_handler.handle(AppError(code=ErrorCode.PROJECT_NOT_FOUND), ErrorContext(guild_id=interaction.guild_id), interaction=interaction)
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
            await display_step_embeds(interaction.guild_id, getattr(interaction, "channel", None), current, total, step, interaction)

        try:
            await run_workflow(discussion_manager.start_second_round, interaction.guild_id, requirement_change, on_step=display_second_round_step, channel=getattr(interaction, "channel", None))
        except MeetingManagerError as error:
            logger.warning(
                "第二輪討論失敗 guild_id=%s change_id=%s：%s",
                interaction.guild_id,
                normalized_change_id,
                type(error).__name__,
            )
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
            return

        await interaction.followup.send("✅ 第二輪討論完成。下一步：/draft 整合草案，再用 /review 審查與選擇。")

    @bot.tree.command(name="draft", description="由 PM 整合已完成的討論並顯示提案摘要")
    async def draft_command(interaction: discord.Interaction) -> None:
        """建立固定格式提案，保存決策並在 Discord 顯示摘要。"""

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "❌ /draft 只能在 Discord 伺服器中使用。",
                ephemeral=True,
            )
            return

        if not await safe_defer(interaction, thinking=True):
            return
        await safe_followup_send(interaction, "⏳ PM 正在整合草案；進度與結果會顯示在會議／狀態頻道。")
        started_at = time.perf_counter()
        try:
            proposal = await run_workflow(discussion_manager.create_proposal_draft, interaction.guild_id, channel=getattr(interaction, "channel", None))
        except MeetingManagerError as error:
            execution_time = time.perf_counter() - started_at
            logger.warning(
                "/draft 失敗 guild_id=%s execution_seconds=%.3f：%s",
                interaction.guild_id,
                execution_time,
                type(error).__name__,
            )
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
            return

        await send_business(interaction, 'proposal', proposal)
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
        if not await safe_defer(interaction, thinking=True, ephemeral=True):
            return
        await safe_original_response_edit(interaction, "⏳ 正在驗證並處理操作；公開進度可用 /status 查看。")
        completed = False
        try:
            record = await run_workflow(decision_service.act, interaction.guild_id, interaction.user.id, choice, channel=getattr(interaction, "channel", None))
            completed = True
            await presenter.present(record, interaction.channel)
        except MeetingManagerError as error:
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
        finally:
            await safe_original_response_edit(interaction, "✅ 操作已保存；請查看會議頻道的結果，或使用 /status 查詢目前階段與下一步。" if completed else "❌ 操作未完成；請查看錯誤卡。若已保存選擇，請用 /decide 重試相同選項。")

    @bot.tree.command(name="review", description="審查草案並顯示使用者決策狀態")
    async def review_command(interaction: discord.Interaction) -> None:
        """執行 Review、一次指定修改與 PM 最終整合。"""

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "❌ /review 只能在 Discord 伺服器中使用。",
                ephemeral=True,
            )
            return

        if not await safe_defer(interaction, thinking=True):
            return
        # 原互動回覆就是這次指令的提示；完成後編輯它，不另發一則完成提示。
        await safe_original_response_edit(interaction, "⏳ 正在讀取或審查草案；請查看會議／狀態頻道，或用 /status 查詢。")
        completed = False
        try:
            result = await run_workflow(discussion_manager.review_and_finalize, interaction.guild_id, channel=getattr(interaction, "channel", None))
            await send_business(interaction, 'review', result.review, stage="草案" if result.revision_performed else None)
            if await present_decision(interaction.guild_id, getattr(interaction, "channel", None), getattr(result, "decision_status", "legacy")):
                completed = True
                return
            if result.revision_performed:
                await interaction.followup.send(
                    "🔧 已將最高優先修改要求交給 "
                    f"**{result.revision_agent_name}**，本會議修改次數已達 1/1。"
                )
                final_review = result.review.get("post_revision_review")
                if isinstance(final_review, dict):
                    await send_business(interaction, 'review', final_review, stage="最終方案")
            else:
                await interaction.followup.send("✅ 草案通過，未使用修改機會。")

            await send_business(interaction, 'proposal', result.final_proposal, stage='已完成方案（舊流程）')
            completed = True
            logger.info(
                "Review 與最終整合完成 guild_id=%s revision_count=%s",
                interaction.guild_id,
                1 if result.revision_performed else 0,
            )
        except MeetingManagerError as error:
            logger.warning("/review 失敗 guild_id=%s：%s", interaction.guild_id, type(error).__name__)
            await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id, write_outcome="unknown"), interaction=interaction)
            return
        finally:
            # finally 也涵蓋上方決策重顯的 return，以及審查／畫面傳送中斷。
            # 完成不等於批准；公開失敗提示也不包含錯誤細節或查詢代碼。
            content = (
                "✅ 審查處理已完成。請查看會議／狀態頻道的結果與下一步；目前狀態可用 /status 查詢。"
                if completed else
                "❌ 本次審查操作未完成。請查看私人錯誤回覆，或用 /status 確認保存狀態與下一步。"
            )
            await safe_original_response_edit(interaction, content)

    @bot.event
    async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
        await bot.error_handler.handle(error, ErrorContext(guild_id=getattr(ctx.guild, 'id', None),
            write_outcome='unknown'), channel=ctx.channel)

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await bot.error_handler.handle(error, ErrorContext(guild_id=interaction.guild_id,
            write_outcome='unknown'), interaction=interaction)

    return bot  # 把已完成所有指令註冊的 Bot 交給 main() 或測試使用。


def _main() -> None:
    """讀取設定、建立相依物件並啟動 Discord Bot。"""

    from pathlib import Path
    from presentation.status_message_store import StatusMessageStore
    settings = load_settings()  # 從 .env 取得 Token、模型與限制設定。
    configure_safe_logging(settings.log_level, secrets=(settings.discord_token, settings.db_password))  # 啟用統一的日誌格式。

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
        acquire_timeout_seconds=settings.db_acquire_timeout_seconds,
        operation_timeout_seconds=settings.db_operation_timeout_seconds,
        pool_recycle_seconds=settings.db_pool_recycle_seconds,
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
        status_store=StatusMessageStore(Path(__file__).resolve().parent / ".runtime/status_messages.json"),
    )  # 將設定和服務注入 Discord Bot。

    logger.info('準備啟動 Bot')
    try:
        bot.run(settings.discord_token, log_handler=None)
    except Exception as error:
        raise AppError(code=ErrorCode.DISCORD_LOGIN_FAILED) from error


def main() -> None:
    configure_safe_logging()
    try:
        _main()
    except Exception as error:
        register_secrets()
        context = ErrorContext(write_outcome='not_started')
        wrapped = error if isinstance(error, (discord.DiscordException, AppError)) else AppError(code=ErrorCode.CONFIG_INVALID)
        report = classify_error(wrapped, context)
        log_error(report, context, error)
        logger.error('%s', report.text())
        raise SystemExit(1) from None


if __name__ == "__main__":  # 只有執行 python bot.py 時才會成立。
    main()  # 測試匯入 bot.py 時不會登入 Discord。
