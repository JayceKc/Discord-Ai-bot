"""測試 /start 背後的專案會議規則。"""

import unittest
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from models.project import Project, ProjectStatus
from services.project_meeting_service import (
    ProjectMeetingError,
    ProjectMeetingService,
)


def make_project(status: ProjectStatus = ProjectStatus.PENDING) -> Project:
    """建立每個測試共用的簡單專案。"""

    return Project(
        id="PRJ-001",
        category="企業網站",
        title="咖啡店品牌官網",
        requirements=("製作首頁",),
        budget=80000,
        deadline=date(2026, 10, 15),
        acceptance_criteria=("手機版可正常瀏覽",),
        status=status,
    )


class ProjectStatusTest(unittest.TestCase):
    """確認專案只允許定義好的狀態轉換。"""

    def test_legal_status_transitions(self) -> None:
        self.assertTrue(
            ProjectStatus.PENDING.can_transition_to(ProjectStatus.IN_PROGRESS)
        )
        self.assertTrue(
            ProjectStatus.IN_PROGRESS.can_transition_to(ProjectStatus.COMPLETED)
        )
        self.assertFalse(
            ProjectStatus.COMPLETED.can_transition_to(ProjectStatus.IN_PROGRESS)
        )
        self.assertFalse(
            ProjectStatus.CANCELLED.can_transition_to(ProjectStatus.IN_PROGRESS)
        )


class ProjectMeetingServiceTest(unittest.IsolatedAsyncioTestCase):
    """驗證專案 ID、Guild 限制與重複啟動規則。"""

    def setUp(self) -> None:
        self.project = make_project()
        self.updated_project = replace(
            self.project,
            status=ProjectStatus.IN_PROGRESS,
        )
        self.project_repository = SimpleNamespace(
            get_project=Mock(return_value=self.project),
            update_status=Mock(return_value=self.updated_project),
        )
        self.guild_project_store = SimpleNamespace(
            get_current_project_id=Mock(return_value=None),
            set_current_project=Mock(),
        )
        self.service = ProjectMeetingService(
            self.project_repository,
            self.guild_project_store,
        )

    async def test_starts_project_and_saves_current_guild_project(self) -> None:
        result = await self.service.start_project(123456, "prj-001")

        self.assertEqual(result.status, ProjectStatus.IN_PROGRESS)
        self.project_repository.get_project.assert_called_once_with("PRJ-001")
        self.project_repository.update_status.assert_called_once_with(
            "PRJ-001",
            ProjectStatus.IN_PROGRESS,
        )
        self.guild_project_store.set_current_project.assert_called_once_with(
            123456,
            "PRJ-001",
        )

    async def test_rejects_unknown_project_id(self) -> None:
        self.project_repository.get_project.return_value = None

        with self.assertRaisesRegex(ProjectMeetingError, "找不到專案 ID"):
            await self.service.start_project(123456, "PRJ-999")

        self.guild_project_store.set_current_project.assert_not_called()

    async def test_rejects_second_project_in_same_guild(self) -> None:
        self.guild_project_store.get_current_project_id.return_value = "PRJ-002"

        with self.assertRaisesRegex(ProjectMeetingError, "已有進行中會議"):
            await self.service.start_project(123456, "PRJ-001")

        self.project_repository.update_status.assert_not_called()

    async def test_prevents_starting_same_project_twice(self) -> None:
        self.guild_project_store.get_current_project_id.return_value = "PRJ-001"

        with self.assertRaisesRegex(ProjectMeetingError, "已經在這個伺服器進行中"):
            await self.service.start_project(123456, "PRJ-001")

        self.project_repository.update_status.assert_not_called()

    async def test_rejects_project_with_illegal_status_transition(self) -> None:
        self.project_repository.get_project.return_value = make_project(
            ProjectStatus.COMPLETED
        )

        with self.assertRaisesRegex(ProjectMeetingError, "目前是「已完成」"):
            await self.service.start_project(123456, "PRJ-001")

        self.project_repository.update_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
