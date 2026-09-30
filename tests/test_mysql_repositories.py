"""MySQL Repository 整合測試；需啟動本機 Docker MySQL 才會執行。"""

from __future__ import annotations

import asyncio
import os
import uuid
import unittest
from datetime import date

from config import load_settings
from database.db import MySQLDatabase
from models.meeting import MeetingRecord, MeetingStatus
from repositories.mysql_meeting_repository import MySQLMeetingRepository
from repositories.mysql_project_repository import MySQLProjectRepository
from repositories.mysql_workspace_repository import MySQLWorkspaceRepository
from models.workspace import ProjectPriority
from services.quality_evaluator import evaluate_review


@unittest.skipUnless(
    os.getenv("RUN_MYSQL_TESTS") == "1",
    "需設定 RUN_MYSQL_TESTS=1 並啟動 Docker MySQL",
)
class MySQLRepositoryIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """驗證正式 MySQL 可讀取匯入資料且會議能跨 Repository instance 保存。"""

    async def asyncSetUp(self) -> None:
        settings = load_settings()
        self.database = MySQLDatabase(
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
            min_size=1,
            max_size=1,
        )
        await self.database.open()
        self.projects = MySQLProjectRepository(self.database)
        self.meetings = MySQLMeetingRepository(self.database)
        self.workspaces = MySQLWorkspaceRepository(self.database)
        self.meeting_id = str(uuid.uuid4())
        self.guild_id = 999_999_001
        self.created_project_ids: list[str] = []

    async def asyncTearDown(self) -> None:
        async with self.database.transaction() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM guild_latest_meetings WHERE guild_id = %s",
                    (self.guild_id,),
                )
                await cursor.execute("DELETE FROM guild_current_projects WHERE guild_id = %s", (self.guild_id,))
                await cursor.execute("DELETE FROM meetings WHERE id = %s", (self.meeting_id,))
                for project_id in self.created_project_ids:
                    await cursor.execute("DELETE FROM projects WHERE id = %s", (project_id,))
                await cursor.execute("DELETE FROM workspaces WHERE guild_id = %s", (self.guild_id,))
        await self.database.close()

    async def build_day25_meeting(self):
        from repositories.mysql_guild_project_store import MySQLGuildProjectStore
        from services.project_meeting_service import ProjectMeetingService
        from services.meeting_manager import MeetingManager
        from tests.test_user_decision import ScriptedPM, ScriptedReview
        project = await self.projects.create_custom_project(
            self.guild_id, title="Day 25 決策測試", category="DAY25_TEST",
            requirements=("建立核心與互動功能",), budget=1000, deadline=date(2026, 12, 31),
            acceptance_criteria=("核心流程可驗收",))
        self.created_project_ids.append(project.id)
        await ProjectMeetingService(self.projects, MySQLGuildProjectStore(self.database)).start_project(self.guild_id, project.id)
        manager = MeetingManager(self.meetings, ScriptedPM(), ScriptedPM(), ScriptedPM(), ScriptedPM(), ScriptedReview(),
            meeting_id_factory=lambda: self.meeting_id, retry_delay_seconds=0)
        await manager.start_first_round(self.guild_id, project.id, "預算1000、年底交付", decision_owner_user_id=7)
        await manager.create_proposal_draft(self.guild_id)
        await manager.review_and_finalize(self.guild_id)
        return project, manager

    async def test_day25_choice_approval_and_fresh_repository(self):
        project, manager = await self.build_day25_meeting()
        waiting = await MySQLMeetingRepository(self.database).get_for_guild(self.guild_id)
        self.assertEqual(waiting.decision_status, "awaiting_choice")
        self.assertEqual(waiting.decision_owner_user_id, 7)
        selected = await manager.decision_service.act(self.guild_id, 7, "A")
        workspace = await self.workspaces.get_or_create(self.guild_id, "Day 25 測試")
        self.assertEqual(workspace.current_project_id, project.id)
        self.assertEqual(workspace.completed_custom_projects, 0)
        self.assertEqual(await self.workspaces.recent_experiences(self.guild_id, project.id), [])
        fresh = await MySQLMeetingRepository(self.database).get_for_guild(self.guild_id)
        self.assertEqual(fresh, selected)
        self.assertEqual(fresh.candidate_review["quality_evaluation"]["outputs"]["quality_index"], 80)
        self.assertIn(self.meeting_id, [r.meeting_id for r in await self.meetings.pending_decisions()])
        await manager.decision_service.act(self.guild_id, 7, "reject")
        restored = await MySQLMeetingRepository(self.database).get_for_guild(self.guild_id)
        self.assertEqual(restored.user_decision["history"][0]["outcome"], "rejected")
        await manager.decision_service.act(self.guild_id, 7, "B")
        final = await manager.decision_service.act(self.guild_id, 7, "approve")
        self.assertNotEqual(selected.candidate_proposal, final.final_proposal)
        restored = await MySQLMeetingRepository(self.database).get_for_guild(self.guild_id)
        self.assertEqual(restored, final)
        workspace = await self.workspaces.get(self.guild_id)
        self.assertIsNone(workspace.current_project_id)
        self.assertEqual(workspace.completed_custom_projects, 1)
        self.assertEqual(len(await self.workspaces.recent_experiences(self.guild_id, project.id)), 1)

    async def test_day25_approval_transaction_rollback(self):
        from services.meeting_manager import MeetingManagerError
        project, manager = await self.build_day25_meeting()
        await manager.decision_service.act(self.guild_id, 7, "A")
        async with self.database.transaction() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("UPDATE projects SET status = 'cancelled' WHERE id = %s", (project.id,))
        with self.assertRaises(MeetingManagerError):
            await manager.decision_service.act(self.guild_id, 7, "approve")
        restored = await MySQLMeetingRepository(self.database).get_for_guild(self.guild_id)
        self.assertEqual(restored.decision_status, "awaiting_approval")
        self.assertIsNone(restored.final_proposal)
        self.assertFalse(restored.user_decision["history"])
        workspace = await self.workspaces.get_or_create(self.guild_id, "測試")
        self.assertEqual(workspace.current_project_id, project.id)

    async def test_day25_mysql_rejects_stale_writer(self):
        from repositories.meeting_repository import MeetingRepositoryError
        _, manager = await self.build_day25_meeting()
        stale = await MySQLMeetingRepository(self.database).get_for_guild(self.guild_id)
        await manager.decision_service.act(self.guild_id, 7, "A")
        with self.assertRaisesRegex(MeetingRepositoryError, "已更新"):
            await self.meetings.save(stale)
        restored = await MySQLMeetingRepository(self.database).get_for_guild(self.guild_id)
        self.assertEqual(restored.decision_status, "awaiting_approval")

    async def test_imported_projects_can_be_loaded(self) -> None:
        projects = await self.projects.list_projects()

        self.assertEqual(len(projects), 5)
        self.assertEqual(projects[0].id, "PRJ-001")
        self.assertTrue(projects[0].requirements)
        self.assertTrue(projects[0].requirement_changes)

    async def test_meeting_round_trip_uses_fresh_repository_instance(self) -> None:
        record = MeetingRecord.new(
            self.meeting_id,
            self.guild_id,
            "PRJ-001",
            "MySQL Repository 整合測試",
        )
        review = {
            "status": "通過",
            "checklist": {
                "completeness": {"score": 4, "reason": "完整"},
                "creativity": {"score": 5, "reason": "有創意"},
                "credibility": {"score": 3, "reason": "基本可信"},
                "feasibility": {"score": 4, "reason": "可執行"},
            },
            "issues": [],
            "revision_allowed": False,
        }
        review["quality_evaluation"] = evaluate_review(review, "innovation")
        record.review_result = review
        await self.meetings.save(record)

        restored = await MySQLMeetingRepository(self.database).get_for_guild(
            self.guild_id
        )

        self.assertEqual(restored, record)
        self.assertEqual(
            restored.review_result["quality_evaluation"]["outputs"]["quality_index"],
            86.0,
        )

    async def test_day23_custom_project_workspace_and_experience(self) -> None:
        project = await self.projects.create_custom_project(
            self.guild_id,
            title="Day 23 MySQL 測試專案",
            category="測試分類",
            requirements=("建立首頁",),
            budget=1000,
            deadline=date(2026, 12, 31),
            acceptance_criteria=("首頁可用",),
            change_description="第二輪加入搜尋",
        )
        self.created_project_ids.append(project.id)
        self.assertEqual(project.owner_guild_id, self.guild_id)
        self.assertEqual(len(project.requirement_changes), 1)
        self.assertIn(project.id, {item.id for item in await self.projects.list_for_guild(self.guild_id)})
        self.assertNotIn(project.id, {item.id for item in await self.projects.list_for_guild(self.guild_id + 1)})

        workspace = await self.workspaces.get_or_create(self.guild_id, "Day 23 測試工作區")
        self.assertEqual(workspace.default_priority, ProjectPriority.GROWTH)
        await self.workspaces.set_default_priority(self.guild_id, ProjectPriority.INNOVATION)
        restored = await MySQLWorkspaceRepository(self.database).get(self.guild_id)
        self.assertEqual(restored.default_priority, ProjectPriority.INNOVATION)

        record = MeetingRecord.new(self.meeting_id, self.guild_id, project.id, "建立首頁")
        record.meeting_context.priority_snapshot = ProjectPriority.INNOVATION.value
        record.final_proposal = {
            "title": "測試最終方案", "summary": "先做首頁", "decisions": [
                {"topic": "範圍", "decision": "採用", "reason": "可驗證"}
            ],
        }
        await self.meetings.save(record)
        experiences = await self.workspaces.recent_experiences(self.guild_id, project.id)
        self.assertEqual(experiences[0]["meeting_id"], self.meeting_id)
        self.assertIn("範圍", experiences[0]["tradeoffs"])
        self.assertEqual((await self.workspaces.get(self.guild_id)).projects_with_final_proposal, 1)

    async def test_final_proposal_completes_and_releases_project(self) -> None:
        first = await self.projects.create_custom_project(
            self.guild_id, title="自動釋放 A", category="測試", requirements=("A",),
            budget=0, deadline=date(2026, 12, 31),
            acceptance_criteria=("完成 A",), change_description="增加 A",
        )
        self.created_project_ids.append(first.id)
        second = await self.projects.create_custom_project(
            self.guild_id, title="自動釋放 B", category="測試", requirements=("B",),
            budget=0, deadline=date(2026, 12, 31),
            acceptance_criteria=("完成 B",), change_description="增加 B",
        )
        self.created_project_ids.append(second.id)
        await self.projects.claim_for_guild(self.guild_id, first.id, allow_existing=False)
        record = MeetingRecord.new(self.meeting_id, self.guild_id, first.id, "A")
        await self.meetings.save(record)
        self.assertEqual((await self.workspaces.get_or_create(self.guild_id, "測試")).current_project_id, first.id)

        record.status = MeetingStatus.COMPLETED
        await self.meetings.save(record)  # 單輪完成但尚無最終方案，不能釋放。
        self.assertEqual((await self.workspaces.get(self.guild_id)).current_project_id, first.id)
        record.final_proposal = {"title": "A 最終方案", "summary": "已完成"}
        await self.meetings.save(record)
        await self.meetings.save(record)  # 重複保存不得影響其他專案。
        self.assertEqual((await self.projects.get_for_guild(self.guild_id, first.id)).status.value, "completed")
        self.assertIsNone((await self.workspaces.get(self.guild_id)).current_project_id)
        self.assertEqual((await self.meetings.get_for_guild(self.guild_id)).meeting_id, self.meeting_id)
        await self.projects.claim_for_guild(self.guild_id, second.id, allow_existing=False)
        self.assertEqual((await self.workspaces.get(self.guild_id)).current_project_id, second.id)

    async def test_optional_change_is_created_only_after_feedback(self) -> None:
        project = await self.projects.create_custom_project(
            self.guild_id, title="先看草案", category="網站", requirements=("首頁",),
            budget=0, deadline=date(2026, 12, 31),
            acceptance_criteria=("首頁可用",),
        )
        self.created_project_ids.append(project.id)
        self.assertEqual(project.requirement_changes, ())
        with self.assertRaisesRegex(Exception, "本伺服器"):
            await self.projects.add_requirement_change(self.guild_id + 1, project.id, "跨伺服器修改")
        await self.projects.claim_for_guild(self.guild_id, project.id, allow_existing=False)
        change = await self.projects.add_requirement_change(self.guild_id, project.id, "加入搜尋")
        self.assertEqual(change.description, "加入搜尋")
        self.assertEqual(len((await self.projects.get_for_guild(self.guild_id, project.id)).requirement_changes), 1)
        with self.assertRaisesRegex(Exception, "已有第二輪"):
            await self.projects.add_requirement_change(self.guild_id, project.id, "再加登入")


if __name__ == "__main__":
    unittest.main()
