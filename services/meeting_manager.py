"""依固定順序協調多位專業 Agent 的會議服務。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from agents.errors import RetryableAgentError

from models.meeting import (
    AgentSuggestion,
    MeetingRecord,
    MeetingRoundSummary,
    MeetingStatus,
    MeetingStepRecord,
    MeetingStepStatus,
    ProposalMetrics,
)
from models.project import RequirementChange
from repositories.meeting_repository import MeetingRepository, MeetingRepositoryError
from repositories.mysql_workspace_repository import MySQLWorkspaceRepository, WorkspaceRepositoryError
from models.workspace import ProjectPriority
from services.quality_evaluator import QualityEvaluationError, evaluate_review


class MeetingManagerError(RuntimeError):
    """會議無法啟動、執行、取消或恢復時的統一錯誤。"""


@dataclass(frozen=True)
class ReviewWorkflowResult:
    """回傳審查結果、修改負責人與 PM 最終方案。"""

    review: dict[str, object]
    revision_performed: bool
    revision_agent_name: str | None
    final_proposal: dict[str, object] | None
    decision_status: str = "legacy"


@dataclass(frozen=True)
class FullMeetingResult:
    """單一入口完成兩輪討論、審查與最終整合後的結果。"""

    record: MeetingRecord
    proposal_draft: dict[str, object]
    review: dict[str, object]
    revision_performed: bool
    revision_agent_name: str | None
    final_proposal: dict[str, object] | None
    decision_status: str = "legacy"


class MeetingAgent(Protocol):
    """Meeting Manager 對所有專業 Agent 使用的共用介面。"""

    async def respond(self, user_input: str) -> BaseModel:
        """根據輸入產生可序列化的 Pydantic 結果。"""


class PMIntegrationAgent(MeetingAgent, Protocol):
    """除了一般回應外，也能把兩輪討論整合成提案。"""

    async def integrate(self, meeting_input: str) -> BaseModel:
        """根據精簡會議內容建立固定格式提案。"""


# 每完成一位 Agent 就通知呼叫端目前進度與已保存的步驟資料。
MeetingStepCallback = Callable[
    [int, int, MeetingStepRecord],
    Awaitable[None],
]

ResultT = TypeVar("ResultT")
logger = logging.getLogger(__name__)


# 每位後發言 Agent 只取得工作所需的前文欄位，避免 Prompt 持續膨脹。
RELEVANT_OUTPUT_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "Research Agent": {
        "PM Agent": ("goal", "constraints", "work_items", "disagreements"),
    },
    "Creative Agent": {
        "Research Agent": (
            "known_information",
            "reasonable_inferences",
            "items_to_verify",
        ),
    },
    "Finance Agent": {
        "PM Agent": ("constraints",),
        "Research Agent": ("known_information", "items_to_verify"),
        "Creative Agent": ("proposals",),
    },
}

# 第二輪先讓 PM 回應需求變更，再讓後面的 Agent 依序延續新的觀點。
SECOND_ROUND_RELEVANT_OUTPUT_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "PM Agent": {
        "Research Agent": ("known_information", "items_to_verify"),
        "Creative Agent": ("proposals",),
        "Finance Agent": ("constraints", "risks", "alternatives"),
    },
    "Research Agent": {
        "PM Agent": ("goal", "constraints", "work_items", "disagreements"),
    },
    "Creative Agent": {
        "Research Agent": (
            "known_information",
            "reasonable_inferences",
            "items_to_verify",
        ),
    },
    "Finance Agent": {
        "PM Agent": ("constraints",),
        "Research Agent": ("known_information", "items_to_verify"),
        "Creative Agent": ("proposals",),
    },
}

# 只有帶有行動或判斷意義的欄位會登記成可追溯的建議。
SUGGESTION_FIELDS: dict[str, tuple[str, ...]] = {
    "PM Agent": ("work_items", "disagreements"),
    "Research Agent": ("reasonable_inferences", "items_to_verify"),
    "Creative Agent": ("proposals",),
    "Finance Agent": ("alternatives",),
}

# 每位 Agent 摘要只保留最能代表其專業結論的欄位。
SUMMARY_FIELDS: dict[str, tuple[str, ...]] = {
    "PM Agent": ("goal",),
    "Research Agent": ("known_information", "items_to_verify"),
    "Creative Agent": ("proposals",),
    "Finance Agent": ("risks", "alternatives"),
    "Review Agent": ("status", "issues", "revision_requests"),
}


class MeetingManager:
    """控制 Agent 順序、上下文傳遞與每一步資料保存。"""

    def __init__(
        self,
        repository: MeetingRepository,
        pm_agent: PMIntegrationAgent,
        research_agent: MeetingAgent,
        creative_agent: MeetingAgent,
        finance_agent: MeetingAgent,
        review_agent: MeetingAgent,
        *,
        meeting_id_factory: Callable[[], str] | None = None,
        max_prompt_characters: int = 6000,
        max_response_characters: int = 4000,
        agent_max_attempts: int = 2,
        retry_delay_seconds: float = 1.0,
        workspace_repository: MySQLWorkspaceRepository | None = None,
    ) -> None:
        if (
            max_prompt_characters <= 0
            or max_response_characters <= 0
            or agent_max_attempts <= 0
            or retry_delay_seconds < 0
        ):
            raise ValueError("會議 Prompt 與回覆字元預算必須大於 0。")
        self.repository = repository
        self._pm_agent = pm_agent
        # tuple 的位置就是固定發言順序，外部不能在執行途中改動。
        self._agents = (
            pm_agent,
            research_agent,
            creative_agent,
            finance_agent,
            review_agent,
        )
        self._agents_by_name = {
            "PM Agent": pm_agent,
            "Research Agent": research_agent,
            "Creative Agent": creative_agent,
            "Finance Agent": finance_agent,
            "Review Agent": review_agent,
        }
        self._meeting_id_factory = meeting_id_factory or (lambda: str(uuid4()))
        self.max_prompt_characters = max_prompt_characters
        self.max_response_characters = max_response_characters
        self.agent_max_attempts = agent_max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.workspace_repository = workspace_repository
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._running_tasks: dict[int, asyncio.Task[object]] = {}
        self._full_workflow_guilds: set[int] = set()
        from services.user_decision_service import UserDecisionService
        self.decision_service = UserDecisionService(self)

    async def start(
        self,
        guild_id: int,
        project_id: str,
        requirement: str,
    ) -> MeetingRecord:
        """建立新會議並依序執行五位 Agent。"""

        return await self._start(
            guild_id,
            project_id,
            requirement,
            first_round_only=False,
        )

    async def start_first_round(
        self,
        guild_id: int,
        project_id: str,
        requirement: str,
        *,
        on_step: MeetingStepCallback | None = None,
        decision_owner_user_id: int | None = None,
    ) -> MeetingRecord:
        """只執行 PM、Research、Creative 與 Finance，逐步回報結果。"""

        return await self._start(
            guild_id,
            project_id,
            requirement,
            first_round_only=True,
            on_step=on_step,
            decision_owner_user_id=decision_owner_user_id,
        )

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
        """從可用檢查點完成第一輪、第二輪、草案、審查與最終方案。"""

        if guild_id in self._full_workflow_guilds:
            raise MeetingManagerError("這個伺服器已有完整會議流程正在執行。")
        self._full_workflow_guilds.add(guild_id)
        normalized_project_id = project_id.strip().upper()
        try:
            record = await self._get_for_guild(guild_id)
            if (record is None or (
                record.project_id != normalized_project_id
                and record.final_proposal is not None
            )):
                record = await self.start_first_round(
                    guild_id,
                    normalized_project_id,
                    requirement,
                    on_step=on_step,
                    **({"decision_owner_user_id": decision_owner_user_id} if decision_owner_user_id is not None else {}),
                )
            else:
                if record.project_id != normalized_project_id:
                    raise MeetingManagerError("目前會議不屬於指定專案。")
                if record.status == MeetingStatus.FAILED:
                    record = await self.resume(guild_id)
                elif record.status != MeetingStatus.COMPLETED:
                    raise MeetingManagerError("目前會議無法接續，請先取消或恢復。")

            if record.applied_requirement_change is None:
                record = await self.start_second_round(
                    guild_id,
                    requirement_change,
                    on_step=on_step,
                )
            elif record.applied_requirement_change.id != requirement_change.id:
                raise MeetingManagerError("目前會議已套用不同的需求變更。")

            proposal_draft = await self.create_proposal_draft(guild_id)
            workflow_result = await self.review_and_finalize(guild_id)
            latest_record = await self._get_for_guild(guild_id)
            if latest_record is None:
                raise MeetingManagerError("找不到已完成的會議紀錄。")
            return FullMeetingResult(
                record=latest_record,
                proposal_draft=proposal_draft,
                review=workflow_result.review,
                revision_performed=workflow_result.revision_performed,
                revision_agent_name=workflow_result.revision_agent_name,
                final_proposal=workflow_result.final_proposal,
                decision_status=workflow_result.decision_status,
            )
        finally:
            self._full_workflow_guilds.discard(guild_id)

    async def start_second_round(
        self,
        guild_id: int,
        requirement_change: RequirementChange,
        *,
        on_step: MeetingStepCallback | None = None,
    ) -> MeetingRecord:
        """套用一次需求變更，讓四位 Agent 各完成一次第二輪回應。"""

        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        if lock.locked():
            raise MeetingManagerError("這個伺服器已有會議正在執行。")

        async with lock:
            record = await self._get_for_guild(guild_id)
            if record is None:
                raise MeetingManagerError("這個伺服器沒有可進行第二輪的會議。")
            if record.status != MeetingStatus.COMPLETED:
                raise MeetingManagerError("第一輪尚未完成，無法開始第二輪。")
            if requirement_change.project_id.upper() != record.project_id:
                raise MeetingManagerError("需求變更不屬於目前進行中的專案。")
            if record.applied_requirement_change is not None:
                raise MeetingManagerError("這場會議已經加入過一次需求變更。")
            if record.final_proposal is not None:
                raise MeetingManagerError("這場會議已有最終方案，不能再加入需求變更。")
            if record.decision_status not in {"legacy", "preparing"}:
                raise MeetingManagerError("已進入使用者決策階段，不能再加入第二輪需求變更。")
            if any(step.round_number == 2 for step in record.steps):
                raise MeetingManagerError("這場會議已經建立第二輪回應。")

            # 第一輪草案已展示給使用者；加入變更後需重新整合兩輪結果。
            record.proposal_draft = None
            record.proposal_metrics = None
            record.review_result = None
            record.applied_requirement_change = requirement_change
            start_index = len(record.steps)
            for offset, agent_name in enumerate(self._agents_by_name.keys()):
                if agent_name == "Review Agent":
                    continue
                record.steps.append(
                    MeetingStepRecord(
                        agent_name=agent_name,
                        order=start_index + offset,
                        round_number=2,
                    )
                )
            # 現有架構以 COMPLETED 表示單輪完成；DAY 18 先直接重啟同一筆
            # 會議，後續再將「整場狀態」與「目前階段」正式拆開。
            record.status = MeetingStatus.RUNNING
            await self._save(record)
            return await self._run(
                record,
                already_running=True,
                start_index=start_index,
                on_step=on_step,
            )

    async def create_proposal_draft(
        self, guild_id: int, *, allow_first_round_only: bool = True,
    ) -> dict[str, object]:
        """讓 PM 整合已完成的討論；自然語言入口可先展示第一輪草案。"""

        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        if lock.locked():
            raise MeetingManagerError("這個伺服器已有會議正在執行。")

        async with lock:
            record = await self._get_for_guild(guild_id)
            if record is None:
                raise MeetingManagerError("這個伺服器沒有可整合的會議。")
            if record.status != MeetingStatus.COMPLETED:
                raise MeetingManagerError("會議尚未完成，無法建立整合草案。")
            completed_rounds = {
                step.round_number
                for step in record.steps
                if step.status == MeetingStepStatus.COMPLETED
            }
            required_rounds = {1} if allow_first_round_only else {1, 2}
            if not required_rounds.issubset(completed_rounds):
                raise MeetingManagerError("尚未完成草案所需的討論輪次。")
            if record.applied_requirement_change is not None and 2 not in completed_rounds:
                raise MeetingManagerError("第二輪尚未完成，不能沿用第一輪草案。")
            if record.proposal_draft is not None:
                return record.proposal_draft

            prompt = self._build_proposal_input(record)
            started_at = time.perf_counter()
            try:
                response, prompt_tokens, completion_tokens, max_output_tokens = (
                    await self._retry_operation(
                        lambda: self._call_pm_integration(prompt),
                        meeting_id=record.meeting_id,
                        agent_name="PM Agent",
                        stage="proposal_draft",
                    )
                )
            except RuntimeError as error:
                # AgentError 繼承 RuntimeError，內容已去除敏感資料，可提供給 Discord。
                raise MeetingManagerError(str(error)) from error
            except Exception as error:
                raise MeetingManagerError("PM 整合草案失敗。") from error

            proposal = response.model_dump(mode="json")
            proposal_text = json.dumps(proposal, ensure_ascii=False)
            metrics = ProposalMetrics(
                input_characters=len(prompt),
                output_characters=len(proposal_text),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                max_output_tokens=max_output_tokens,
                execution_time_seconds=round(
                    time.perf_counter() - started_at,
                    3,
                ),
            )
            if len(proposal_text) > self.max_response_characters:
                raise MeetingManagerError("PM 整合草案超過字元預算。")
            record.proposal_draft = proposal
            record.proposal_metrics = metrics
            await self._save(record)
            return proposal

    async def get_proposal_metrics(self, guild_id: int) -> ProposalMetrics | None:
        """取得已保存的草案統計，讓 Discord 顯示實際模型用量。"""

        record = await self._get_for_guild(guild_id)
        return record.proposal_metrics if record is not None else None

    async def review_and_finalize(self, guild_id: int) -> ReviewWorkflowResult:
        """審查草案、最多修改一次，再由 PM 重新整合最終方案。"""

        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        if lock.locked():
            raise MeetingManagerError("這個伺服器已有會議正在執行。")

        async with lock:
            record = await self._get_for_guild(guild_id)
            if record is None:
                raise MeetingManagerError("這個伺服器沒有可審查的會議。")
            if record.proposal_draft is None:
                raise MeetingManagerError("請先使用 /draft 建立 PM 整合草案。")
            if record.revision_count not in {0, 1}:
                raise MeetingManagerError("草案修改次數不合法。")

            if record.decision_status != "legacy":
                try:
                    await self.decision_service.prepare(record)
                except MeetingManagerError:
                    raise
                except Exception as error:
                    raise MeetingManagerError("決策整理失敗，請重試 /review。") from error
                return ReviewWorkflowResult(
                    review=record.review_result,
                    revision_performed=record.revision_count == 1,
                    revision_agent_name=record.revision_agent_name,
                    final_proposal=record.final_proposal,
                    decision_status=record.decision_status,
                )

            # 已完成的流程直接回傳保存結果，不會再次呼叫 Agent。
            if record.final_proposal is not None and record.review_result is not None:
                self._attach_quality_evaluation(record)
                # 讓升級前已完成但仍佔用 Guild 的會議在重試時釋放專案。
                await self._save(record)
                return ReviewWorkflowResult(
                    review=record.review_result,
                    revision_performed=record.revision_count == 1,
                    revision_agent_name=record.revision_agent_name,
                    final_proposal=record.final_proposal,
                )

            record.error = None
            if record.review_result is None:
                review_prompt = self._build_review_input(record)
                review_result = await self._execute_workflow_agent(
                    record,
                    "Review Agent",
                    review_prompt,
                )
                record.review_result = review_result
                self._attach_quality_evaluation(record, strict=True)
                await self._save(record)
            else:
                review_result = record.review_result

            status = review_result.get("status")
            if status == "通過":
                record.final_proposal = dict(record.proposal_draft)
                record.final_proposal_metrics = record.proposal_metrics
                await self._save(record)
                return ReviewWorkflowResult(
                    review=review_result,
                    revision_performed=False,
                    revision_agent_name=None,
                    final_proposal=record.final_proposal,
                )
            if status != "需要修改":
                raise MeetingManagerError("Review Agent 回傳未知審查狀態。")

            if record.revision_count == 0:
                issue = self._select_revision_issue(review_result)
                assigned_agent = str(issue["assigned_agent"])
                revision_prompt = self._build_revision_input(
                    record,
                    assigned_agent,
                    review_result,
                )
                revision_output = await self._execute_workflow_agent(
                    record,
                    assigned_agent,
                    revision_prompt,
                )
                # 修改次數只在指定 Agent 成功後增加，且程式只允許 0 -> 1。
                record.revision_count = 1
                record.revision_agent_name = assigned_agent
                record.revision_output = revision_output
                await self._save(record)

            if record.revision_output is None or record.revision_agent_name is None:
                raise MeetingManagerError("找不到可供 PM 整合的修改結果。")

            final_prompt = self._build_final_proposal_input(record)
            final_proposal, final_metrics = await self._integrate_final_proposal(
                final_prompt
            )
            final_review = await self._execute_workflow_agent(
                record,
                "Review Agent",
                self._build_review_input(record, proposal=final_proposal),
            )
            try:
                final_evaluation = evaluate_review(
                    final_review,
                    record.meeting_context.priority_snapshot,
                )
            except QualityEvaluationError as error:
                raise MeetingManagerError(f"最終方案評估失敗：{error}") from error
            final_evaluation["evaluated_artifact"] = "final_proposal"
            final_review["quality_evaluation"] = final_evaluation
            review_result["post_revision_review"] = final_review
            record.final_proposal = final_proposal
            record.final_proposal_metrics = final_metrics
            await self._save(record)
            return ReviewWorkflowResult(
                review=review_result,
                revision_performed=True,
                revision_agent_name=record.revision_agent_name,
                final_proposal=final_proposal,
            )

    async def get_final_proposal_metrics(self, guild_id: int) -> ProposalMetrics | None:
        """取得 Review 流程完成後的 PM 最終方案統計。"""

        record = await self._get_for_guild(guild_id)
        return record.final_proposal_metrics if record is not None else None

    def _attach_quality_evaluation(
        self,
        record: MeetingRecord,
        *,
        strict: bool = False,
    ) -> None:
        """以 Python 固定公式補上評估；舊版 passed 格式仍可讀取。"""

        if record.review_result is None or "quality_evaluation" in record.review_result:
            return
        try:
            evaluation = evaluate_review(
                record.review_result,
                record.meeting_context.priority_snapshot,
            )
        except QualityEvaluationError as error:
            if strict:
                raise MeetingManagerError(f"方案評估失敗：{error}") from error
            logger.info("舊版 Review 沒有 Day 24 評分，保留原紀錄 meeting_id=%s", record.meeting_id)
            return
        record.review_result["quality_evaluation"] = evaluation

    async def _retry_operation(
        self,
        operation: Callable[[], Awaitable[ResultT]],
        *,
        meeting_id: str,
        agent_name: str,
        stage: str,
    ) -> ResultT:
        """只針對明確可恢復的模型錯誤執行有限次數重試。"""

        for attempt in range(1, self.agent_max_attempts + 1):
            try:
                return await operation()
            except RetryableAgentError as error:
                if attempt == self.agent_max_attempts:
                    raise
                logger.warning(
                    "模型回覆可重試失敗 meeting_id=%s stage=%s agent=%s "
                    "attempt=%s/%s error_type=%s",
                    meeting_id,
                    stage,
                    agent_name,
                    attempt,
                    self.agent_max_attempts,
                    type(error).__name__,
                )
                if self.retry_delay_seconds:
                    await asyncio.sleep(self.retry_delay_seconds)
        raise AssertionError("可重試操作未正常結束。")

    async def _call_agent(
        self,
        agent: MeetingAgent,
        prompt: str,
    ) -> tuple[BaseModel, int | None, int | None, int | None]:
        """呼叫 Agent，並相容於具有或不具有 token 統計的實作。"""

        respond_with_metadata = getattr(agent, "respond_with_metadata", None)
        if callable(respond_with_metadata):
            detailed_response = await respond_with_metadata(prompt)
            return (
                detailed_response.output,
                detailed_response.usage.prompt_tokens,
                detailed_response.usage.completion_tokens,
                detailed_response.max_output_tokens,
            )
        response = await agent.respond(prompt)
        config = getattr(agent, "config", None)
        configured_limit = getattr(config, "max_output_tokens", None)
        max_output_tokens = (
            configured_limit
            if isinstance(configured_limit, int)
            and not isinstance(configured_limit, bool)
            and configured_limit >= 0
            else None
        )
        return response, None, None, max_output_tokens

    async def _call_pm_integration(
        self,
        prompt: str,
    ) -> tuple[BaseModel, int | None, int | None, int | None]:
        """呼叫 PM 整合，並保留可用的 token 統計。"""

        integrate_with_metadata = getattr(self._pm_agent, "integrate_with_metadata", None)
        if callable(integrate_with_metadata):
            detailed_response = await integrate_with_metadata(prompt)
            return (
                detailed_response.output,
                detailed_response.usage.prompt_tokens,
                detailed_response.usage.completion_tokens,
                detailed_response.max_output_tokens,
            )
        response = await self._pm_agent.integrate(prompt)
        return response, None, None, None

    async def _execute_workflow_agent(
        self,
        record: MeetingRecord,
        agent_name: str,
        prompt: str,
    ) -> dict[str, object]:
        """執行審查階段的一位 Agent，並將輸入、輸出及統計保存為步驟。"""

        if record.decision_status != "legacy":
            for previous in reversed(record.steps):
                if previous.agent_name == agent_name and previous.input_text == prompt and previous.status == MeetingStepStatus.COMPLETED:
                    return previous.output_data
        agent = self._agents_by_name.get(agent_name)
        if agent is None:
            raise MeetingManagerError(f"找不到 {agent_name} 的執行器。")
        prompt = self._validate_prompt_budget(prompt)
        step = MeetingStepRecord(
            agent_name=agent_name,
            order=len(record.steps),
            round_number=3,
            status=MeetingStepStatus.RUNNING,
            input_text=prompt,
            input_characters=len(prompt),
        )
        record.steps.append(step)
        record.current_step_index = step.order
        await self._save(record)

        started_at = time.perf_counter()
        try:
            response, prompt_tokens, completion_tokens, max_output_tokens = (
                await self._retry_operation(
                    lambda: self._call_agent(agent, prompt),
                    meeting_id=record.meeting_id,
                    agent_name=agent_name,
                    stage="review_workflow",
                )
            )
            step.prompt_tokens = prompt_tokens
            step.completion_tokens = completion_tokens
            step.max_output_tokens = max_output_tokens
            output = response.model_dump(mode="json")
            output_text = json.dumps(output, ensure_ascii=False)
            step.output_characters = len(output_text)
            if len(output_text) > self.max_response_characters:
                raise MeetingManagerError(f"{agent_name} 回覆超過字元預算。")
        except Exception as error:
            step.execution_time_seconds = round(time.perf_counter() - started_at, 3)
            step.status = MeetingStepStatus.FAILED
            step.error = f"{agent_name} 審查流程執行失敗。"
            record.error = step.error
            await self._save(record)
            if isinstance(error, MeetingManagerError):
                raise
            raise MeetingManagerError(step.error) from error

        step.execution_time_seconds = round(time.perf_counter() - started_at, 3)
        step.output_data = output
        step.status = MeetingStepStatus.COMPLETED
        record.error = None
        await self._save(record)
        return output

    def _build_review_input(
        self,
        record: MeetingRecord,
        *,
        proposal: dict[str, object] | None = None,
    ) -> str:
        """把 PM 草案或修改後方案轉成 ReviewRequest 固定格式。"""

        assert record.proposal_draft is not None
        review_target = proposal if proposal is not None else record.proposal_draft
        return self._validate_prompt_budget(
            json.dumps(
                {
                    "project_id": record.project_id,
                    "draft": json.dumps(review_target, ensure_ascii=False),
                    "revision_count": record.revision_count,
                    **({"user_choice": self._selected_option(record)} if record.user_decision and record.user_decision.get("selection") else {}),
                    **self._priority_context(record),
                },
                ensure_ascii=False,
            )
        )

    @staticmethod
    def _selected_option(record):
        if not record.user_decision or not record.user_decision.get("selection"):
            return None
        return next(o for o in record.user_decision["question"]["options"]
            if o["option_id"] == record.user_decision["selection"]["option_id"])

    @staticmethod
    def _select_revision_issue(
        review_result: dict[str, object],
    ) -> dict[str, object]:
        """依高、中、低排序，挑選本次唯一修改要優先處理的問題。"""

        raw_issues = review_result.get("issues")
        if not isinstance(raw_issues, list) or not raw_issues:
            raise MeetingManagerError("Review Agent 沒有提供結構化修改要求。")
        issues = [item for item in raw_issues if isinstance(item, dict)]
        allowed_agents = {
            "PM Agent",
            "Research Agent",
            "Creative Agent",
            "Finance Agent",
        }
        issues = [
            item for item in issues
            if item.get("assigned_agent") in allowed_agents
        ]
        if not issues:
            raise MeetingManagerError("Review Agent 沒有指定合法的修改負責人。")
        priority_order = {"高": 0, "中": 1, "低": 2}
        return min(
            issues,
            key=lambda item: priority_order.get(str(item.get("priority")), 3),
        )

    def _build_revision_input(
        self,
        record: MeetingRecord,
        assigned_agent: str,
        review_result: dict[str, object],
    ) -> str:
        """只把指定負責人的修改要求與必要草案內容交給該 Agent。"""

        assert record.proposal_draft is not None
        assigned_issues = [
            item
            for item in review_result.get("issues", [])
            if isinstance(item, dict)
            and item.get("assigned_agent") == assigned_agent
        ]
        context = {
            "project_id": record.project_id,
            "task": "依 Review Agent 的要求提出一次具體修正，禁止只表示同意。",
            "assigned_agent": assigned_agent,
            "original_proposal": json.dumps(
                record.candidate_proposal or record.proposal_draft,
                ensure_ascii=False,
            )[:2800],
            "review_issues": assigned_issues,
            "revision_limit": "這是唯一一次修改機會",
            "user_choice": self._selected_option(record),
            "requirement": record.requirement,
            "requirement_change": record.applied_requirement_change.to_dict() if record.applied_requirement_change else None,
            **self._priority_context(record),
        }
        return self._validate_prompt_budget(json.dumps(context, ensure_ascii=False))

    def _build_final_proposal_input(self, record: MeetingRecord) -> str:
        """提供初稿、審查要求與唯一修改結果，要求 PM 產生最終方案。"""

        assert record.proposal_draft is not None
        assert record.review_result is not None
        assert record.revision_output is not None
        context = {
            "project_id": record.project_id,
            "task": "依審查要求與指定 Agent 的修改結果，重新整合最終方案。",
            "original_proposal": json.dumps(
                record.proposal_draft,
                ensure_ascii=False,
            )[:2600],
            "review_issues": record.review_result.get("issues", []),
            "revision_agent": record.revision_agent_name,
            "revision_output": json.dumps(
                record.revision_output,
                ensure_ascii=False,
            )[:1800],
            "revision_count": record.revision_count,
            "rules": [
                "必須回應審查問題",
                "保留固定五章節與決策紀錄",
                "不得要求第二次修改",
            ],
            **self._priority_context(record),
        }
        return self._validate_prompt_budget(json.dumps(context, ensure_ascii=False))

    async def _integrate_final_proposal(
        self,
        prompt: str,
    ) -> tuple[dict[str, object], ProposalMetrics]:
        """讓 PM 重新整合最終方案，並保留字元、Token 與耗時統計。"""

        started_at = time.perf_counter()
        try:
            response, prompt_tokens, completion_tokens, max_output_tokens = (
                await self._retry_operation(
                    lambda: self._call_pm_integration(prompt),
                    meeting_id="final-proposal",
                    agent_name="PM Agent",
                    stage="final_proposal",
                )
            )
        except Exception as error:
            if isinstance(error, RuntimeError):
                raise MeetingManagerError(str(error)) from error
            raise MeetingManagerError("PM 最終方案整合失敗。") from error

        proposal = response.model_dump(mode="json")
        proposal_text = json.dumps(proposal, ensure_ascii=False)
        metrics = ProposalMetrics(
            input_characters=len(prompt),
            output_characters=len(proposal_text),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            max_output_tokens=max_output_tokens,
            execution_time_seconds=round(time.perf_counter() - started_at, 3),
        )
        if len(proposal_text) > self.max_response_characters:
            raise MeetingManagerError("PM 最終方案超過字元預算。")
        return proposal, metrics

    async def _start(
        self,
        guild_id: int,
        project_id: str,
        requirement: str,
        *,
        first_round_only: bool,
        on_step: MeetingStepCallback | None = None,
        decision_owner_user_id: int | None = None,
    ) -> MeetingRecord:
        """套用共用 Guild Lock 與資料驗證後建立會議。"""

        normalized_project_id = project_id.strip().upper()
        normalized_requirement = requirement.strip()
        if not normalized_project_id or not normalized_requirement:
            raise MeetingManagerError("專案 ID 與需求不可為空。")

        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        # 不讓第二個請求排隊後又啟動一次相同 Guild 的會議。
        if lock.locked():
            raise MeetingManagerError("這個伺服器已有會議正在執行。")

        async with lock:
            current = await self._get_for_guild(guild_id)
            if current is not None and current.status in {
                MeetingStatus.PENDING,
                MeetingStatus.RUNNING,
                MeetingStatus.FAILED,
            }:
                raise MeetingManagerError("這個伺服器已有尚未結束的會議。")

            if current is not None and current.decision_status not in {"legacy", "approved"}:
                raise MeetingManagerError("目前會議尚待使用者決策或批准，不能啟動另一場。")
            record = MeetingRecord.new(
                self._meeting_id_factory(),
                guild_id,
                normalized_project_id,
                normalized_requirement,
            )
            if decision_owner_user_id is not None:
                if isinstance(decision_owner_user_id, bool) or not isinstance(decision_owner_user_id, int) or decision_owner_user_id <= 0:
                    raise MeetingManagerError("決策者 ID 不合法。")
                record.decision_owner_user_id = decision_owner_user_id
                record.decision_status = "preparing"
            if self.workspace_repository is not None:
                try:
                    workspace = await self.workspace_repository.get_or_create(
                        guild_id, f"Guild {guild_id}"
                    )
                    experiences = await self.workspace_repository.recent_experiences(
                        guild_id, normalized_project_id, exclude_meeting_id=record.meeting_id
                    )
                except WorkspaceRepositoryError as error:
                    raise MeetingManagerError(str(error)) from error
                record.meeting_context.priority_snapshot = workspace.default_priority.value
                record.meeting_context.experience_snapshot = experiences
            if first_round_only:
                # Review 屬於後續審查階段，不放進第一輪討論紀錄。
                record.steps = record.steps[:4]
            await self._save(record)
            return await self._run(record, on_step=on_step)

    async def _run(
        self,
        record: MeetingRecord,
        *,
        already_running: bool = False,
        start_index: int = 0,
        on_step: MeetingStepCallback | None = None,
    ) -> MeetingRecord:
        """執行未完成步驟，並保存成功、失敗或取消狀態。"""

        if not already_running:
            record.transition_to(MeetingStatus.RUNNING)
        # 恢復舊會議時，用已完成步驟補齊可能缺少的共享內容。
        self._refresh_context_from_completed_steps(record)
        record.error = None
        await self._save(record)
        current_task = asyncio.current_task()
        if current_task is not None:
            self._running_tasks[record.guild_id] = current_task

        try:
            total_steps = len(record.steps) - start_index
            for index in range(start_index, len(record.steps)):
                step = record.steps[index]
                if step.status == MeetingStepStatus.COMPLETED:
                    continue

                agent = self._agents_by_name.get(step.agent_name)
                if agent is None:
                    raise MeetingManagerError(f"找不到 {step.agent_name} 的執行器。")

                record.current_step_index = index
                step.input_text = self._build_input(record, index)
                step.input_characters = len(step.input_text)
                step.status = MeetingStepStatus.RUNNING
                step.error = None
                await self._save(record)

                started_at = time.perf_counter()
                try:
                    response, prompt_tokens, completion_tokens, max_output_tokens = (
                        await self._retry_operation(
                            lambda: self._call_agent(agent, step.input_text or ""),
                            meeting_id=record.meeting_id,
                            agent_name=step.agent_name,
                            stage=f"round_{step.round_number}",
                        )
                    )
                    step.prompt_tokens = prompt_tokens
                    step.completion_tokens = completion_tokens
                    step.max_output_tokens = max_output_tokens
                    output_data = response.model_dump(mode="json")
                    if step.round_number == 2:
                        self._validate_second_round_output(output_data)
                    output_text = json.dumps(output_data, ensure_ascii=False)
                    step.output_characters = len(output_text)
                    if len(output_text) > self.max_response_characters:
                        raise MeetingManagerError(
                            f"{step.agent_name} 回覆超過字元預算。"
                        )
                finally:
                    # 成功、失敗或取消都記錄實際等待 Agent 的時間。
                    step.execution_time_seconds = round(
                        time.perf_counter() - started_at,
                        3,
                    )
                step.output_data = output_data
                step.status = MeetingStepStatus.COMPLETED
                self._record_step_context(record, step)
                if step.agent_name == "Finance Agent":
                    self._save_round_summary(record, step.round_number)
                await self._save(record)
                if on_step is not None:
                    await on_step(index - start_index + 1, total_steps, step)

            record.transition_to(MeetingStatus.COMPLETED)
            record.current_step_index = len(record.steps)
            await self._save(record)
            return record
        except asyncio.CancelledError:
            # CancelledError 必須繼續向上拋出，但先保存可恢復辨識的狀態。
            if record.status != MeetingStatus.CANCELLED:
                record.transition_to(MeetingStatus.CANCELLED)
                step = record.steps[record.current_step_index]
                if step.status in {
                    MeetingStepStatus.PENDING,
                    MeetingStepStatus.RUNNING,
                }:
                    step.status = MeetingStepStatus.CANCELLED
                await self._save(record)
            raise
        except Exception as error:
            step = record.steps[record.current_step_index]
            step.status = MeetingStepStatus.FAILED
            # 不把底層例外細節寫入 JSON，避免日後意外保存敏感資訊。
            elapsed = (
                f"（耗時 {step.execution_time_seconds:.2f} 秒）"
                if step.execution_time_seconds is not None
                else ""
            )
            safe_error = f"{step.agent_name} 執行失敗{elapsed}。"
            step.error = safe_error
            record.error = safe_error
            record.transition_to(MeetingStatus.FAILED)
            try:
                await self._save(record)
            except MeetingManagerError as save_error:
                raise MeetingManagerError("無法保存會議失敗狀態。") from save_error
            raise MeetingManagerError(safe_error) from error
        finally:
            self._running_tasks.pop(record.guild_id, None)

    async def cancel(self, guild_id: int) -> MeetingRecord:
        """取消目前會議並中止正在等待 Agent 的 Task。"""

        record = await self._get_for_guild(guild_id)
        if record is None:
            raise MeetingManagerError("這個伺服器沒有可取消的會議。")
        if record.status not in {
            MeetingStatus.PENDING,
            MeetingStatus.RUNNING,
            MeetingStatus.FAILED,
        }:
            raise MeetingManagerError("這場會議目前無法取消。")

        record.transition_to(MeetingStatus.CANCELLED)
        if record.current_step_index < len(record.steps):
            step = record.steps[record.current_step_index]
            if step.status in {
                MeetingStepStatus.PENDING,
                MeetingStepStatus.RUNNING,
            }:
                step.status = MeetingStepStatus.CANCELLED
        await self._save(record)

        running_task = self._running_tasks.get(guild_id)
        if running_task is not None:
            running_task.cancel()
        return record

    async def resume(self, guild_id: int) -> MeetingRecord:
        """從第一個失敗步驟繼續執行，保留先前成功結果。"""

        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        if lock.locked():
            raise MeetingManagerError("這個伺服器已有會議正在執行。")

        async with lock:
            record = await self._get_for_guild(guild_id)
            if record is None or record.status != MeetingStatus.FAILED:
                raise MeetingManagerError("這個伺服器沒有可恢復的失敗會議。")

            failed_step = next(
                (
                    step
                    for step in record.steps
                    if step.status == MeetingStepStatus.FAILED
                ),
                None,
            )
            if failed_step is None:
                raise MeetingManagerError("失敗會議中找不到可恢復的 Agent 步驟。")

            failed_step.status = MeetingStepStatus.PENDING
            failed_step.input_text = None
            failed_step.output_data = None
            failed_step.error = None
            record.transition_to(MeetingStatus.RUNNING)
            await self._save(record)
            return await self._run(record, already_running=True)

    def _build_input(self, record: MeetingRecord, index: int) -> str:
        """依目前步驟組合需求與先前 Agent 的結構化結果。"""

        if index == 0:
            if record.meeting_context.priority_snapshot is None:
                return self._validate_prompt_budget(record.requirement)
            return self._validate_prompt_budget(json.dumps({
                "requirement": record.requirement,
                **self._priority_context(record),
            }, ensure_ascii=False))

        agent_name = record.steps[index].agent_name
        current_step = record.steps[index]
        field_rules = (
            SECOND_ROUND_RELEVANT_OUTPUT_FIELDS
            if current_step.round_number == 2
            else RELEVANT_OUTPUT_FIELDS
        )
        field_selection = field_rules.get(agent_name, {})
        previous_outputs: dict[str, dict[str, object]] = {}
        for source_name, fields in field_selection.items():
            source_step = next(
                (
                    step
                    for step in reversed(record.steps[:index])
                    if step.agent_name == source_name and step.output_data is not None
                ),
                None,
            )
            if source_step is not None and source_step.output_data is not None:
                previous_outputs[source_name] = {
                    field: source_step.output_data[field]
                    for field in fields
                    if field in source_step.output_data
                }

        relevant_sources = set(field_selection)
        # 第二輪的 previous_outputs 已包含各角色最新且必要的欄位。
        # 不再重複附上完整建議與輪次摘要，避免越後面的 Agent Prompt 越長。
        if current_step.round_number == 2:
            shared_context = {
                "previous_outputs": previous_outputs,
            }
        else:
            shared_context = {
                "previous_outputs": previous_outputs,
                "suggestions": [
                    item.to_dict()
                    for item in record.meeting_context.suggestions
                    if item.agent_name in relevant_sources
                ],
                "round_summaries": [
                    item.to_dict()
                    for item in record.meeting_context.round_summaries
                ],
            }
        context = {
            "project_id": record.project_id,
            "requirement": record.requirement,
            "meeting_context": shared_context,
            **self._priority_context(record),
        }

        if current_step.round_number == 2:
            assert record.applied_requirement_change is not None
            context["requirement_change"] = record.applied_requirement_change.to_dict()
            context["response_rules"] = {
                "required_action": "針對新限制或前文，至少補充、反對或修正一項內容",
                "forbidden_response": "不得只回覆「我同意」",
                "response_limit": "本輪只能回應一次",
            }

        # ReviewAgent 有固定的 ReviewRequest 輸入格式。
        if agent_name == "Review Agent" and current_step.round_number == 1:
            review_context = {
                "project_id": record.project_id,
                "requirement": record.requirement,
                "agent_summaries": record.meeting_context.agent_summaries,
                "suggestions": [
                    item.to_dict()
                    for item in record.meeting_context.suggestions
                ],
                "round_summaries": [
                    item.to_dict()
                    for item in record.meeting_context.round_summaries
                ],
            }
            return self._validate_prompt_budget(json.dumps(
                {
                    "project_id": record.project_id,
                    "draft": json.dumps(review_context, ensure_ascii=False),
                    "revision_count": 0,
                    **self._priority_context(record),
                },
                ensure_ascii=False,
            ))
        return self._validate_prompt_budget(
            json.dumps(context, ensure_ascii=False)
        )

    def _build_proposal_input(self, record: MeetingRecord) -> str:
        """挑選兩輪摘要與重要原文，避免把完整會議紀錄全部塞給 PM。"""

        round_summaries = [
            {
                "round_number": item.round_number,
                "summary": item.summary[:1400],
            }
            for item in sorted(
                record.meeting_context.round_summaries,
                key=lambda item: item.round_number,
            )
            if item.round_number in {1, 2}
        ]
        important_originals: list[dict[str, object]] = []
        agent_order = (
            "PM Agent",
            "Research Agent",
            "Creative Agent",
            "Finance Agent",
        )
        for round_number in (1, 2):
            for agent_name in agent_order:
                suggestion = next(
                    (
                        item
                        for item in record.meeting_context.suggestions
                        if item.round_number == round_number
                        and item.agent_name == agent_name
                    ),
                    None,
                )
                if suggestion is not None:
                    important_originals.append(
                        {
                            "agent_name": suggestion.agent_name,
                            "round_number": suggestion.round_number,
                            "category": suggestion.category,
                            "content": suggestion.content[:220],
                        }
                    )

        context: dict[str, object] = {
            "project_id": record.project_id,
            "requirement": record.requirement,
            "requirement_change": (
                record.applied_requirement_change.to_dict()
                if record.applied_requirement_change is not None
                else None
            ),
            "round_summaries": round_summaries,
            "important_originals": important_originals,
            "required_sections": [
                "專案背景與目標",
                "整合方案",
                "執行計畫",
                "風險與對策",
                "驗收標準",
            ],
            "decision_types": ["採用", "拒絕", "折衷"],
            **self._priority_context(record),
        }
        return self._validate_prompt_budget(
            json.dumps(context, ensure_ascii=False)
        )

    @staticmethod
    def _priority_context(record: MeetingRecord) -> dict[str, object]:
        raw = record.meeting_context.priority_snapshot
        if raw is None:
            return {}
        try:
            priority = ProjectPriority(raw)
        except ValueError as error:
            raise MeetingManagerError("會議優先目標資料不正確。") from error
        return {
            "project_priority": priority.label,
            "priority_guidance": priority.guidance,
            "recent_experiences": record.meeting_context.experience_snapshot,
        }

    def _validate_prompt_budget(self, prompt: str) -> str:
        """限制單次送給 Agent 的 Prompt 字元數。"""

        if len(prompt) > self.max_prompt_characters:
            raise MeetingManagerError("Agent Prompt 超過字元預算。")
        return prompt

    @staticmethod
    def _validate_second_round_output(output_data: dict[str, object]) -> None:
        """拒絕沒有實質內容或只表達同意的第二輪回覆。"""

        texts: list[str] = []

        def collect(value: object) -> None:
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())
            elif isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item)

        collect(output_data)
        agreement_only = {"同意", "我同意", "贊成", "我贊成", "沒有意見"}
        normalized = {
            text.rstrip("。！!，, ")
            for text in texts
        }
        if not normalized or normalized.issubset(agreement_only):
            raise MeetingManagerError("第二輪回覆必須包含補充、反對或修正內容。")

    def _refresh_context_from_completed_steps(self, record: MeetingRecord) -> None:
        """從已完成步驟重建摘要與建議，支援舊紀錄及失敗恢復。"""

        for step in record.steps:
            if step.status == MeetingStepStatus.COMPLETED and step.output_data:
                self._record_step_context(record, step)
        if len(record.steps) >= 4 and all(
            step.status == MeetingStepStatus.COMPLETED
            for step in record.steps[:4]
        ):
            self._save_round_summary(record, 1)

    def _record_step_context(
        self,
        record: MeetingRecord,
        step: MeetingStepRecord,
    ) -> None:
        """更新單一 Agent 摘要，並登記帶有來源的建議。"""

        assert step.output_data is not None
        summary_fields = SUMMARY_FIELDS.get(step.agent_name, tuple(step.output_data))
        summary_data = {
            field: step.output_data[field]
            for field in summary_fields
            if field in step.output_data
        }
        summary = json.dumps(summary_data, ensure_ascii=False, separators=(",", ":"))
        if len(summary) > 800:
            summary = summary[:780] + "…（摘要已截斷）"
        summary_key = (
            step.agent_name
            if step.round_number == 1
            else f"{step.agent_name}（第 {step.round_number} 輪）"
        )
        record.meeting_context.agent_summaries[summary_key] = summary

        # 恢復會議時會重新整理，因此先移除同一 Agent 的舊建議避免重複。
        record.meeting_context.suggestions = [
            item
            for item in record.meeting_context.suggestions
            if not (
                item.agent_name == step.agent_name
                and item.round_number == step.round_number
            )
        ]
        for field in SUGGESTION_FIELDS.get(step.agent_name, ()):
            for content in self._suggestion_texts(step.output_data.get(field)):
                record.meeting_context.suggestions.append(
                    AgentSuggestion(
                        step.agent_name,
                        field,
                        content,
                        round_number=step.round_number,
                    )
                )

    @staticmethod
    def _suggestion_texts(value: object) -> list[str]:
        """將字串或方案物件整理為可追溯的建議文字。"""

        if not isinstance(value, list):
            return []
        suggestions: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                suggestions.append(item.strip())
            elif isinstance(item, dict):
                title = item.get("title")
                description = item.get("description")
                parts = [
                    part.strip()
                    for part in (title, description)
                    if isinstance(part, str) and part.strip()
                ]
                if parts:
                    suggestions.append("：".join(parts))
        return suggestions

    def _save_round_summary(self, record: MeetingRecord, round_number: int) -> None:
        """彙整指定輪次的 Agent 摘要並保存。"""

        parts = [
            f"{step.agent_name}：{record.meeting_context.agent_summaries[summary_key]}"
            for step in record.steps
            if step.round_number == round_number
            if (summary_key := (
                step.agent_name
                if round_number == 1
                else f"{step.agent_name}（第 {round_number} 輪）"
            )) in record.meeting_context.agent_summaries
        ]
        summary = "\n".join(parts)
        record.meeting_context.round_summaries = [
            item
            for item in record.meeting_context.round_summaries
            if item.round_number != round_number
        ]
        record.meeting_context.round_summaries.append(
            MeetingRoundSummary(round_number=round_number, summary=summary)
        )

    async def _get_for_guild(self, guild_id: int) -> MeetingRecord | None:
        """將 Repository 讀取錯誤轉為服務層的安全訊息。"""

        try:
            result = self.repository.get_for_guild(guild_id)
            return await result if inspect.isawaitable(result) else result
        except MeetingRepositoryError as error:
            raise MeetingManagerError("無法讀取會議狀態。") from error

    async def _save(self, record: MeetingRecord) -> None:
        """將 Repository 寫入錯誤轉為服務層的安全訊息。"""

        try:
            result = self.repository.save(record)
            if inspect.isawaitable(result):
                await result
        except MeetingRepositoryError as error:
            raise MeetingManagerError("無法保存會議狀態。") from error
