"""依固定順序協調多位專業 Agent 的會議服務。"""

from __future__ import annotations

import asyncio
import json
from typing import Callable, Protocol
from uuid import uuid4

from pydantic import BaseModel

from models.meeting import MeetingRecord, MeetingStatus, MeetingStepStatus
from repositories.meeting_repository import MeetingRepository, MeetingRepositoryError


class MeetingManagerError(RuntimeError):
    """會議無法啟動、執行、取消或恢復時的統一錯誤。"""


class MeetingAgent(Protocol):
    """Meeting Manager 對所有專業 Agent 使用的共用介面。"""

    async def respond(self, user_input: str) -> BaseModel:
        """根據輸入產生可序列化的 Pydantic 結果。"""


class MeetingManager:
    """控制 Agent 順序、上下文傳遞與每一步資料保存。"""

    def __init__(
        self,
        repository: MeetingRepository,
        pm_agent: MeetingAgent,
        research_agent: MeetingAgent,
        creative_agent: MeetingAgent,
        finance_agent: MeetingAgent,
        review_agent: MeetingAgent,
        *,
        meeting_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.repository = repository
        # tuple 的位置就是固定發言順序，外部不能在執行途中改動。
        self._agents = (
            pm_agent,
            research_agent,
            creative_agent,
            finance_agent,
            review_agent,
        )
        self._meeting_id_factory = meeting_id_factory or (lambda: str(uuid4()))
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._running_tasks: dict[int, asyncio.Task[object]] = {}

    async def start(
        self,
        guild_id: int,
        project_id: str,
        requirement: str,
    ) -> MeetingRecord:
        """建立新會議並依序執行五位 Agent。"""

        normalized_project_id = project_id.strip().upper()
        normalized_requirement = requirement.strip()
        if not normalized_project_id or not normalized_requirement:
            raise MeetingManagerError("專案 ID 與需求不可為空。")

        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        # 不讓第二個請求排隊後又啟動一次相同 Guild 的會議。
        if lock.locked():
            raise MeetingManagerError("這個伺服器已有會議正在執行。")

        async with lock:
            current = self._get_for_guild(guild_id)
            if current is not None and current.status in {
                MeetingStatus.PENDING,
                MeetingStatus.RUNNING,
                MeetingStatus.FAILED,
            }:
                raise MeetingManagerError("這個伺服器已有尚未結束的會議。")

            record = MeetingRecord.new(
                self._meeting_id_factory(),
                guild_id,
                normalized_project_id,
                normalized_requirement,
            )
            self._save(record)
            return await self._run(record)

    async def _run(
        self,
        record: MeetingRecord,
        *,
        already_running: bool = False,
    ) -> MeetingRecord:
        """執行未完成步驟，並保存成功、失敗或取消狀態。"""

        if not already_running:
            record.transition_to(MeetingStatus.RUNNING)
        record.error = None
        self._save(record)
        current_task = asyncio.current_task()
        if current_task is not None:
            self._running_tasks[record.guild_id] = current_task

        try:
            for index, agent in enumerate(self._agents):
                step = record.steps[index]
                if step.status == MeetingStepStatus.COMPLETED:
                    continue

                record.current_step_index = index
                step.input_text = self._build_input(record, index)
                step.status = MeetingStepStatus.RUNNING
                step.error = None
                self._save(record)

                response = await agent.respond(step.input_text)
                step.output_data = response.model_dump(mode="json")
                step.status = MeetingStepStatus.COMPLETED
                self._save(record)

            record.transition_to(MeetingStatus.COMPLETED)
            record.current_step_index = len(record.steps)
            self._save(record)
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
                self._save(record)
            raise
        except Exception as error:
            step = record.steps[record.current_step_index]
            step.status = MeetingStepStatus.FAILED
            # 不把底層例外細節寫入 JSON，避免日後意外保存敏感資訊。
            safe_error = f"{step.agent_name} 執行失敗。"
            step.error = safe_error
            record.error = safe_error
            record.transition_to(MeetingStatus.FAILED)
            try:
                self._save(record)
            except MeetingManagerError as save_error:
                raise MeetingManagerError("無法保存會議失敗狀態。") from save_error
            raise MeetingManagerError(safe_error) from error
        finally:
            self._running_tasks.pop(record.guild_id, None)

    def cancel(self, guild_id: int) -> MeetingRecord:
        """取消目前會議並中止正在等待 Agent 的 Task。"""

        record = self._get_for_guild(guild_id)
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
        self._save(record)

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
            record = self._get_for_guild(guild_id)
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
            self._save(record)
            return await self._run(record, already_running=True)

    def _build_input(self, record: MeetingRecord, index: int) -> str:
        """依目前步驟組合需求與先前 Agent 的結構化結果。"""

        if index == 0:
            return record.requirement

        previous_outputs = {
            step.agent_name: step.output_data
            for step in record.steps[:index]
            if step.output_data is not None
        }
        context = {
            "project_id": record.project_id,
            "requirement": record.requirement,
            "previous_outputs": previous_outputs,
        }

        # ReviewAgent 有固定的 ReviewRequest 輸入格式。
        if index == 4:
            return json.dumps(
                {
                    "project_id": record.project_id,
                    "draft": json.dumps(context, ensure_ascii=False),
                    "revision_count": 0,
                },
                ensure_ascii=False,
            )
        return json.dumps(context, ensure_ascii=False)

    def _get_for_guild(self, guild_id: int) -> MeetingRecord | None:
        """將 Repository 讀取錯誤轉為服務層的安全訊息。"""

        try:
            return self.repository.get_for_guild(guild_id)
        except MeetingRepositoryError as error:
            raise MeetingManagerError("無法讀取會議狀態。") from error

    def _save(self, record: MeetingRecord) -> None:
        """將 Repository 寫入錯誤轉為服務層的安全訊息。"""

        try:
            self.repository.save(record)
        except MeetingRepositoryError as error:
            raise MeetingManagerError("無法保存會議狀態。") from error
