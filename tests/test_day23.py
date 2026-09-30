"""Day 23 command and meeting snapshot tests without Discord or MySQL network access."""

import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bot import create_bot
from config import Settings
from models.meeting import MeetingContext
from models.project import Project, ProjectStatus
from models.workspace import ProjectPriority, Workspace
from repositories.mysql_project_repository import MySQLProjectRepository
from repositories.project_repository import ProjectRepositoryError
from services.meeting_manager import MeetingManager
from tests.test_meeting_manager import FakeAgent, MemoryMeetingRepository


class Day23BotTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.projects = SimpleNamespace(
            list_projects=Mock(return_value=[]),
        )
        self.workspaces = SimpleNamespace(
            get_or_create=AsyncMock(return_value=Workspace(42, "測試伺服器", ProjectPriority.GROWTH)),
            set_default_priority=AsyncMock(return_value=Workspace(42, "測試伺服器", ProjectPriority.INNOVATION)),
        )
        self.bot = create_bot(
            Settings(discord_token="test-token"),
            SimpleNamespace(chat=AsyncMock()),
            self.projects,
            SimpleNamespace(start_project=AsyncMock()),
            SimpleNamespace(),
            workspace_repository=self.workspaces,
        )

    async def asyncTearDown(self):
        await self.bot.close()

    @staticmethod
    def interaction(*, can_manage=False):
        return SimpleNamespace(
            guild_id=42,
            guild=SimpleNamespace(name="測試伺服器"),
            user=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=can_manage)),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_custom_project_slash_command_is_removed(self):
        self.assertIsNone(self.bot.tree.get_command("custom_project"))

    async def test_projects_hides_other_guild_custom_projects(self):
        def project(identifier, owner):
            return Project(identifier, "網站", identifier, ("首頁",), 100, date.today(),
                           ("可用",), ProjectStatus.PENDING, (), owner)
        self.projects.list_projects.return_value = [
            project("PRJ-SEED", None), project("CUS-MINE", 42), project("CUS-OTHER", 99)
        ]
        interaction = self.interaction()
        await self.bot.tree.get_command("projects").callback(interaction)
        embed = interaction.response.send_message.await_args.kwargs["embed"]
        self.assertEqual(embed.description, "目前共有 2 個專案。")
        self.assertNotIn("CUS-OTHER", [field.name for field in embed.fields])

    async def test_priority_requires_manage_guild(self):
        interaction = self.interaction(can_manage=False)
        choice = SimpleNamespace(value=ProjectPriority.INNOVATION.value)
        await self.bot.tree.get_command("priority").callback(interaction, choice)
        self.workspaces.set_default_priority.assert_not_awaited()
        self.assertIn("管理員", interaction.response.send_message.await_args.args[0])

    async def test_priority_updates_next_meeting_default(self):
        interaction = self.interaction(can_manage=True)
        choice = SimpleNamespace(value=ProjectPriority.INNOVATION.value)
        await self.bot.tree.get_command("priority").callback(interaction, choice)
        self.workspaces.set_default_priority.assert_awaited_once_with(42, ProjectPriority.INNOVATION)
        self.assertIn("下一場", interaction.response.send_message.await_args.args[0])


class Day23MeetingTest(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_is_saved_before_first_agent_and_reused(self):
        repository = MemoryMeetingRepository()
        calls = []
        pm = FakeAgent("PM Agent", calls)
        workspaces = SimpleNamespace(
            get_or_create=AsyncMock(return_value=Workspace(42, "測試", ProjectPriority.COST_EFFICIENCY)),
            recent_experiences=AsyncMock(return_value=[{
                "meeting_id": "old", "project_id": "CUS-OLD", "title": "舊案", "summary": "控制成本"
            }]),
        )
        manager = MeetingManager(
            repository, pm, FakeAgent("Research Agent", calls), FakeAgent("Creative Agent", calls),
            FakeAgent("Finance Agent", calls), FakeAgent("Review Agent", calls),
            meeting_id_factory=lambda: "new-meeting", workspace_repository=workspaces,
        )
        record = await manager.start_first_round(42, "CUS-NEW", "建立網站")
        self.assertEqual(repository.snapshots[0].meeting_context.priority_snapshot,
                         ProjectPriority.COST_EFFICIENCY.value)
        self.assertEqual(len(repository.snapshots[0].meeting_context.experience_snapshot), 1)
        self.assertEqual(json.loads(pm.inputs[0])["project_priority"], "成本效益")
        self.assertEqual(MeetingContext.from_dict(record.meeting_context.to_dict()).priority_snapshot,
                         ProjectPriority.COST_EFFICIENCY.value)
        workspaces.recent_experiences.assert_awaited_once_with(
            42, "CUS-NEW", exclude_meeting_id="new-meeting"
        )

    async def test_repository_rejects_invalid_custom_project_before_transaction(self):
        database = SimpleNamespace(transaction=Mock(side_effect=AssertionError("unexpected transaction")))
        repository = MySQLProjectRepository(database)
        with self.assertRaises(ProjectRepositoryError):
            await repository.create_custom_project(
                42, title=" ", category="網站", requirements=("首頁",), budget=10,
                deadline=date.today(), acceptance_criteria=("可用",), change_description="變更"
            )
        database.transaction.assert_not_called()


if __name__ == "__main__":
    unittest.main()
