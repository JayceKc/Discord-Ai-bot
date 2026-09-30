"""自然語言專案入口：範圍、表單與會議串接。"""

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from agents.errors import AgentTimeoutError
from agents.project_intent_agent import ProjectIntent
from bot import create_bot
from config import Settings
from models.project import Project, ProjectStatus, RequirementChange
from models.workspace import ProjectPriority, Workspace
from services.project_intake import ProjectIntake
from services.meeting_manager import MeetingManagerError


class ProjectIntakeStateTest(unittest.TestCase):
    def test_start_and_validate_fields(self):
        intake = ProjectIntake()
        session = intake.start(42, 100, 7, "我要做一個網站")
        self.assertEqual(session.fields["title"], "網站")
        self.assertEqual(session.missing_field(), "category")
        self.assertEqual(intake.accept(session, "網站"), None)
        self.assertEqual(intake.accept(session, "首頁\n商品頁"), None)
        self.assertIn("預算", intake.accept(session, "一萬元"))
        self.assertEqual(intake.accept(session, "10000"), None)
        self.assertIn("日期", intake.accept(session, "2026-02-30"))
        self.assertEqual(intake.accept(session, "2026-12-31"), None)
        self.assertEqual(intake.accept(session, "首頁可用"), None)
        self.assertTrue(session.awaiting_confirmation)
        self.assertEqual(session.fields["requirements"], ("首頁", "商品頁"))

    def test_unrelated_message_does_not_start(self):
        self.assertIsNone(ProjectIntake().start(42, 100, 7, "今天好嗎"))


class ProjectIntakeBotTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        change = RequirementChange("CHG-TEST", "CUS-TEST", "增加搜尋", "測試", date.today(), "待評估")
        self.project = Project("CUS-TEST", "網站", "網站", ("首頁",), 10000,
                               date(2026, 12, 31), ("首頁可用",), ProjectStatus.PENDING,
                               (), 42)
        self.projects = SimpleNamespace(
            create_custom_project=AsyncMock(return_value=self.project),
            add_requirement_change=AsyncMock(return_value=change),
        )
        self.meeting_service = SimpleNamespace(prepare_project=AsyncMock(return_value=self.project))
        self.manager = SimpleNamespace(
            start_first_round=AsyncMock(),
            start_second_round=AsyncMock(),
            resume=AsyncMock(),
            create_proposal_draft=AsyncMock(return_value={"title": "第一輪草案", "summary": "先做首頁"}),
            get_proposal_metrics=Mock(return_value=None),
            review_and_finalize=AsyncMock(return_value=SimpleNamespace(
                review={"status": "通過"}, revision_performed=False,
                final_proposal={"title": "網站方案", "summary": "完成"},
            )),
            get_final_proposal_metrics=Mock(return_value=None),
        )
        self.workspace = SimpleNamespace(get_or_create=AsyncMock(return_value=Workspace(
            42, "測試", ProjectPriority.GROWTH)))
        self.intent_classifier = SimpleNamespace(classify=AsyncMock(return_value=ProjectIntent(
            intent="not_project",
            title=None,
            confidence=0.95,
            reason="不是建立專案的要求",
        )))
        self.bot = create_bot(Settings(discord_token="test", project_intake_channel_id=100),
                              SimpleNamespace(chat=AsyncMock()), self.projects,
                              self.meeting_service, self.manager,
                              workspace_repository=self.workspace,
                              intent_classifier=self.intent_classifier)
        self.listener = self.bot.extra_events["on_message"][0]
        self.send = AsyncMock()

    async def asyncTearDown(self):
        await self.bot.close()

    def message(self, content, channel_id=100, user_id=7):
        return SimpleNamespace(content=content, guild=SimpleNamespace(id=42, name="測試"),
                               channel=SimpleNamespace(id=channel_id, send=self.send),
                               author=SimpleNamespace(id=user_id, bot=False, mention="<@7>"))

    async def test_only_configured_channel_and_no_commands(self):
        await self.listener(self.message("我要做一個網站", 101))
        await self.listener(self.message("!ask 我要做一個網站"))
        self.send.assert_not_awaited()

    async def test_explicit_pattern_uses_fast_path_without_model(self):
        await self.listener(self.message("我要做一個網站"))

        self.intent_classifier.classify.assert_not_awaited()
        self.assertIn("專案類型", self.send.await_args.args[0])

    async def test_model_intent_requires_confirmation_before_collecting(self):
        self.intent_classifier.classify.return_value = ProjectIntent(
            intent="create_project",
            title="咖啡廳",
            confidence=0.96,
            reason="使用者明確表示想開設咖啡廳",
        )

        await self.listener(self.message("我要開一間咖啡廳"))

        self.intent_classifier.classify.assert_awaited_once_with("我要開一間咖啡廳")
        self.assertIn("我理解你想建立「咖啡廳」專案", self.send.await_args.args[0])
        self.projects.create_custom_project.assert_not_awaited()

        await self.listener(self.message("確認"))

        self.assertIn("專案類型", self.send.await_args.args[0])
        self.projects.create_custom_project.assert_not_awaited()

    async def test_uncertain_intent_returns_examples_without_starting(self):
        self.intent_classifier.classify.return_value = ProjectIntent(
            intent="uncertain",
            title=None,
            confidence=0.55,
            reason="只有模糊想法，沒有明確建立要求",
        )

        await self.listener(self.message("咖啡廳好像不錯"))

        self.assertIn("我要開一間咖啡廳", self.send.await_args.args[0])
        self.projects.create_custom_project.assert_not_awaited()

    async def test_classifier_failure_returns_fixed_guidance(self):
        self.intent_classifier.classify.side_effect = AgentTimeoutError("逾時")

        await self.listener(self.message("能不能幫我規劃新的事業"))

        self.assertIn("我暫時無法判斷", self.send.await_args.args[0])
        self.projects.create_custom_project.assert_not_awaited()

    async def test_other_user_cannot_fill_someone_elses_draft(self):
        await self.listener(self.message("我要做一個網站"))
        await self.listener(self.message("網站", user_id=8))
        self.assertEqual(self.send.await_count, 2)
        self.assertIn("專案建立入口", self.send.await_args.args[0])
        await self.listener(self.message("網站"))
        self.assertEqual(self.send.await_count, 3)
        self.assertIn("請列出需求", self.send.await_args.args[0])

    async def test_guided_flow_waits_for_feedback_before_finalizing(self):
        for content in ("我要做一個網站", "網站", "首頁", "10000", "2026-12-31",
                        "首頁可用"):
            await self.listener(self.message(content))
        self.projects.create_custom_project.assert_not_awaited()
        await self.listener(self.message("確認"))
        self.projects.create_custom_project.assert_awaited_once()
        self.assertNotIn("change_description", self.projects.create_custom_project.await_args.kwargs)
        self.meeting_service.prepare_project.assert_awaited_once_with(42, "CUS-TEST")
        self.manager.start_first_round.assert_awaited_once()
        self.manager.review_and_finalize.assert_not_awaited()
        self.assertIn("📄 第一輪草案", [call.kwargs["embed"].title for call in self.send.await_args_list if call.kwargs.get("embed") is not None])
        await self.listener(self.message("直接定稿"))
        self.manager.review_and_finalize.assert_awaited_once_with(42)
        self.projects.add_requirement_change.assert_not_awaited()

    async def test_requested_change_runs_second_round_then_finalizes(self):
        for content in ("我要做一個網站", "網站", "首頁", "10000", "2026-12-31",
                        "首頁可用", "確認"):
            await self.listener(self.message(content))
        await self.listener(self.message("修改：增加搜尋"))
        self.projects.add_requirement_change.assert_awaited_once_with(42, "CUS-TEST", "增加搜尋")
        self.manager.start_second_round.assert_awaited_once()
        self.manager.review_and_finalize.assert_awaited_once_with(42)

    async def test_second_round_can_retry_after_interrupt(self):
        self.manager.start_second_round.side_effect = [MeetingManagerError("暫時中斷"), None]
        for content in ("我要做一個網站", "網站", "首頁", "10000", "2026-12-31",
                        "首頁可用", "確認", "修改：增加搜尋"):
            await self.listener(self.message(content))
        self.manager.review_and_finalize.assert_not_awaited()
        await self.listener(self.message("重試"))
        self.assertEqual(self.manager.start_second_round.await_count, 2)
        self.manager.review_and_finalize.assert_awaited_once_with(42)

    async def test_cancel_does_not_write(self):
        await self.listener(self.message("我要做一個網站"))
        await self.listener(self.message("取消"))
        self.projects.create_custom_project.assert_not_awaited()

    async def test_current_project_blocks_creation(self):
        self.workspace.get_or_create.return_value = Workspace(
            42, "測試", ProjectPriority.GROWTH, current_project_id="PRJ-001")
        for content in ("我要做一個網站", "網站", "首頁", "10000", "2026-12-31",
                        "首頁可用", "確認"):
            await self.listener(self.message(content))
        self.projects.create_custom_project.assert_not_awaited()
