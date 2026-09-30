"""處理專案會議啟動規則與狀態轉換。"""

from __future__ import annotations

import asyncio
import inspect

from models.project import Project, ProjectStatus
from repositories.guild_project_store import (
    GuildProjectStore,
    GuildProjectStoreError,
)
from repositories.project_repository import (
    ProjectRepository,
    ProjectRepositoryError,
)


class ProjectMeetingError(RuntimeError):
    """無法啟動專案會議時，提供給 Discord 使用者的安全訊息。"""


class ProjectMeetingService:
    """驗證專案與 Guild 狀態，再啟動一場專案會議。"""

    def __init__(
        self,
        project_repository: ProjectRepository,
        guild_project_store: GuildProjectStore,
    ) -> None:
        self.project_repository = project_repository
        self.guild_project_store = guild_project_store
        # 防止同一個 Bot 程序內兩個 /start 同時通過檢查。
        self._start_lock = asyncio.Lock()

    async def start_project(self, guild_id: int, project_id: str) -> Project:
        """驗證所有規則，將專案轉為進行中並保存 Guild 狀態。"""

        normalized_id = project_id.strip().upper()
        if not normalized_id:
            raise ProjectMeetingError("請提供專案 ID，例如 PRJ-001。")

        atomic_claim = getattr(self.project_repository, "claim_for_guild", None)
        if callable(atomic_claim):
            try:
                return await atomic_claim(
                    guild_id,
                    normalized_id,
                    allow_existing=False,
                )
            except ProjectRepositoryError as error:
                raise ProjectMeetingError(str(error)) from error

        async with self._start_lock:
            try:
                current_project_id = await _resolve(
                    self.guild_project_store.get_current_project_id(guild_id)
                )
                project = await _resolve(self.project_repository.get_project(normalized_id))
            except (GuildProjectStoreError, ProjectRepositoryError) as error:
                raise ProjectMeetingError("無法讀取專案狀態，請稍後再試。") from error

            if current_project_id == normalized_id:
                raise ProjectMeetingError(
                    f"專案 {normalized_id} 已經在這個伺服器進行中。"
                )
            if current_project_id is not None:
                raise ProjectMeetingError(
                    f"這個伺服器已有進行中會議：{current_project_id}。"
                )
            if project is None:
                raise ProjectMeetingError(
                    f"找不到專案 ID：{normalized_id}，請先使用 /projects 查看。"
                )
            if not project.can_transition_to(ProjectStatus.IN_PROGRESS):
                raise ProjectMeetingError(
                    f"專案 {normalized_id} 目前是「{project.status.label}」，無法啟動。"
                )

            try:
                updated_project = await _resolve(
                    self.project_repository.update_status(
                        normalized_id,
                        ProjectStatus.IN_PROGRESS,
                    )
                )
                await _resolve(
                    self.guild_project_store.set_current_project(guild_id, normalized_id)
                )
            except (GuildProjectStoreError, ProjectRepositoryError) as error:
                # Guild 狀態保存失敗時，盡力將專案復原為啟動前的狀態。
                try:
                    await _resolve(
                        self.project_repository.update_status(normalized_id, project.status)
                    )
                except ProjectRepositoryError:
                    pass
                raise ProjectMeetingError("無法保存專案狀態，請稍後再試。") from error

            return updated_project

    async def prepare_project(self, guild_id: int, project_id: str) -> Project:
        """啟動新專案，或在同一專案已進行中時安全接續流程。"""

        normalized_id = project_id.strip().upper()
        if not normalized_id:
            raise ProjectMeetingError("請提供專案 ID，例如 PRJ-001。")

        atomic_claim = getattr(self.project_repository, "claim_for_guild", None)
        if callable(atomic_claim):
            try:
                return await atomic_claim(
                    guild_id,
                    normalized_id,
                    allow_existing=True,
                )
            except ProjectRepositoryError as error:
                raise ProjectMeetingError(str(error)) from error

        async with self._start_lock:
            try:
                current_project_id = await _resolve(
                    self.guild_project_store.get_current_project_id(guild_id)
                )
                project = await _resolve(self.project_repository.get_project(normalized_id))
            except (GuildProjectStoreError, ProjectRepositoryError) as error:
                raise ProjectMeetingError("無法讀取專案狀態，請稍後再試。") from error

            if current_project_id == normalized_id:
                if project is None:
                    raise ProjectMeetingError(f"找不到專案 ID：{normalized_id}。")
                return project
            if current_project_id is not None:
                raise ProjectMeetingError(
                    f"這個伺服器已有進行中會議：{current_project_id}。"
                )

        # 不在同一把 Lock 內再次取得 Lock，沿用既有啟動與補償邏輯。
        return await self.start_project(guild_id, normalized_id)


async def _resolve(value: object) -> object:
    """相容尚未升級的 JSON Repository 與非同步 MySQL Repository。"""

    return await value if inspect.isawaitable(value) else value
