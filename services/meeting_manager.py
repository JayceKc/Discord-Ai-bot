"""依固定順序協調多位專業 Agent 的會議服務。"""

from __future__ import annotations

import asyncio
import json
from typing import Callable, Protocol
from uuid import uuid4

from pydantic import BaseModel

from models.meeting import MeetingRecord, MeetingStatus, MeetingStepStatus
from repositories.meeting_repository import MeetingRepository


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
            current = self.repository.get_for_guild(guild_id)
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
            self.repository.save(record)
            return await self._run(record)

    async def _run(self, record: MeetingRecord) -> MeetingRecord:
        """執行尚未完成的步驟；本階段先提供完整成功路徑。"""

        record.transition_to(MeetingStatus.RUNNING)
        record.error = None
        self.repository.save(record)
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
                self.repository.save(record)

                response = await agent.respond(step.input_text)
                step.output_data = response.model_dump(mode="json")
                step.status = MeetingStepStatus.COMPLETED
                self.repository.save(record)

            record.transition_to(MeetingStatus.COMPLETED)
            record.current_step_index = len(record.steps)
            self.repository.save(record)
            return record
        finally:
            self._running_tasks.pop(record.guild_id, None)

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
