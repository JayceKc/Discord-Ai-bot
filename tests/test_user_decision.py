"""Day 25：決策影響、授權、評分、持久化與 Discord 操作。"""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from pydantic import BaseModel, ValidationError

from agents.pm_agent import PMProposalDraft, UserChoiceProposal
from agents.structured_agent import StructuredAgentResponse
from bot import create_bot, format_proposal_embed, format_review_embed
from config import Settings
from models.meeting import MeetingRecord
from models.project import RequirementChange
from models.user_decision import DecisionQuestion
from repositories.meeting_repository import JsonMeetingRepository, MeetingRepositoryError
from services.llm_service import LLMUsage
from services.meeting_manager import MeetingManager, MeetingManagerError
from views.decision_view import ApprovalView, DecisionPresenter, DecisionView


class Suggestion(BaseModel):
    conclusion: str = "先做核心流程，互動功能需要分階段交付"


class Review(BaseModel):
    status: str
    checklist: dict
    issues: list
    revision_allowed: bool


def proposal():
    return PMProposalDraft(title="網站方案", summary="依預算完成網站",
        sections={key: "原本同時完成所有功能" for key in (
            "background_and_goal", "integrated_solution", "execution_plan", "risks_and_responses", "acceptance_criteria")},
        decisions=[{"topic": "功能範圍", "decision": "折衷", "reason": "預算限制", "sources": ["Finance Agent 第一輪"]}])


def question():
    return DecisionQuestion(topic="功能範圍與交付順序", why_user_decision_needed="有限預算需要選擇交付方式",
        sources=[{"step_index": 0, "quote": "先做核心流程"}],
        options=[{"option_id": code, "label": label, "description": description,
            "benefits": "可以明確安排交付", "tradeoffs": "部分功能延後", "expected_changes": ["execution_plan"]}
            for code, label, description in (("A", "核心優先", "本次只完成核心功能"), ("B", "分階段交付", "先做核心，下階段加入互動"))])


class ScriptedPM:
    def __init__(self):
        self.apply_calls = []
        self.question_calls = 0
        self.fail_apply = False
        self.no_change = False
        self.wrong_option = False
        self.gate = None

    async def respond(self, prompt):
        return Suggestion()

    async def integrate(self, prompt):
        return proposal()

    async def create_decision(self, prompt):
        self.question_calls += 1
        return question()

    async def apply_decision(self, prompt):
        data = json.loads(prompt)
        self.apply_calls.append(data)
        if self.gate:
            await self.gate.wait()
        if self.fail_apply:
            self.fail_apply = False
            raise RuntimeError("測試模型中斷")
        output = proposal().model_dump()
        if not self.no_change:
            output["sections"]["execution_plan"] = (
                "本次只交付核心功能，互動功能不列入首版驗收" if data["user_choice"]["option_id"] == "A"
                else "先驗收核心功能，下階段再加入互動功能並分別驗收")
            if data.get("revision_output"):
                output["sections"]["execution_plan"] += "；補充負責人與驗收步驟"
        return StructuredAgentResponse(output=UserChoiceProposal(
            proposal=output, option_id="B" if self.wrong_option else data["user_choice"]["option_id"],
            decision_impact=[{"section": "execution_plan", "reason": "依照選擇調整交付與驗收"}]),
            usage=LLMUsage(prompt_tokens=50, completion_tokens=100, request_duration_seconds=0.01, total_duration_ns=None, load_duration_ns=None), max_output_tokens=1800)


class ScriptedReview:
    def __init__(self):
        self.inputs = []
        self.fail_candidate = False
        self.fail_revision_review = False
        self.needs_revision = False

    async def respond(self, prompt):
        data = json.loads(prompt)
        self.inputs.append(data)
        if self.fail_candidate and data.get("user_choice"):
            self.fail_candidate = False
            raise RuntimeError("評分中斷")
        if self.fail_revision_review and data["revision_count"] == 1:
            self.fail_revision_review = False
            raise RuntimeError("修改後評分中斷")
        fails = self.needs_revision and data.get("user_choice") and data["revision_count"] == 0
        score = 2 if fails else 4 if data.get("user_choice") else 3
        return Review(status="需要修改" if fails else "通過",
            checklist={key: {"score": score, "reason": "檢查交付方式與可行性"} for key in (
                "completeness", "creativity", "credibility", "feasibility")},
            issues=[{"problem": "缺少負責人", "required_change": "補齊負責人", "priority": "高", "assigned_agent": "Finance Agent"}] if fails else [],
            revision_allowed=bool(fails))


class UserDecisionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = JsonMeetingRepository(Path(self.directory.name)/"meetings.json")
        self.pm, self.review = ScriptedPM(), ScriptedReview()
        self.manager = self.new_manager()
        self.service = self.manager.decision_service
        await self.manager.start_first_round(42, "CUSTOM-1", "預算1000、年底交付", decision_owner_user_id=7)
        await self.manager.create_proposal_draft(42)
        self.result = await self.manager.review_and_finalize(42)

    def new_manager(self):
        return MeetingManager(self.repo, self.pm, ScriptedPM(), ScriptedPM(), ScriptedPM(), self.review,
            retry_delay_seconds=0)

    async def test_choice_changes_plan_and_requires_approval(self):
        self.assertIsNone(self.result.final_proposal)
        self.assertEqual(self.result.decision_status, "awaiting_choice")
        before = self.repo.get_for_guild(42)
        chosen = await self.service.act(42, 7, "A")
        self.assertIsNone(chosen.final_proposal)
        self.assertEqual(chosen.decision_status, "awaiting_approval")
        self.assertNotEqual(chosen.proposal_draft["sections"]["execution_plan"], chosen.candidate_proposal["sections"]["execution_plan"])
        self.assertEqual(chosen.review_result["quality_evaluation"]["outputs"]["quality_index"], 60)
        self.assertEqual(chosen.candidate_review["quality_evaluation"]["outputs"]["quality_index"], 80)
        self.assertEqual(self.review.inputs[-1]["user_choice"]["option_id"], "A")
        approved = await self.service.act(42, 7, "approve")
        self.assertEqual(approved.final_proposal, chosen.candidate_proposal)
        self.assertEqual(approved.user_decision["history"][-1]["outcome"], "approved")
        self.assertGreater(approved.decision_version, before.decision_version)
        with self.assertRaises(MeetingManagerError):
            await self.service.act(42, 7, "approve")

    async def test_reject_preserves_history_and_alternative_changes_final(self):
        a = await self.service.act(42, 7, "A")
        rejected = await self.service.act(42, 7, "reject")
        self.assertIsNone(rejected.candidate_proposal)
        self.assertIsNone(rejected.final_proposal)
        self.assertEqual(rejected.decision_status, "awaiting_choice")
        self.assertEqual(rejected.user_decision["history"][-1]["proposal"], a.candidate_proposal)
        with self.assertRaisesRegex(MeetingManagerError, "已駁回"):
            await self.service.act(42, 7, "A")
        b = await self.service.act(42, 7, "B")
        self.assertNotEqual(a.candidate_proposal, b.candidate_proposal)
        final = await self.service.act(42, 7, "approve")
        self.assertEqual([h["outcome"] for h in final.user_decision["history"]], ["rejected", "approved"])

    async def test_authorization_and_stale_context(self):
        record = await self.service.get(42)
        for kwargs in ({"user_id": 8}, {"meeting_id": "old"}, {"version": 0}, {"message_id": 999}):
            args = {"guild_id": 42, "user_id": 7, "choice": "A", **kwargs}
            with self.assertRaises(MeetingManagerError):
                await self.service.act(**args)
        for guild in (None, 99):
            with self.assertRaises(MeetingManagerError):
                self.service.authorize(record, guild, 7)
        self.assertFalse(self.pm.apply_calls)

    async def test_invalid_choice_and_wrong_stage(self):
        for choice in ("C", "approve", "reject", "garbage"):
            with self.assertRaises(MeetingManagerError):
                await self.service.act(42, 7, choice)
        await self.service.act(42, 7, "A")
        with self.assertRaises(MeetingManagerError):
            await self.service.act(42, 7, "B")

    async def test_no_effect_or_wrong_option_is_rejected(self):
        self.pm.no_change = True
        with self.assertRaisesRegex(MeetingManagerError, "實際改變"):
            await self.service.act(42, 7, "A")
        self.assertIsNone(self.repo.get_for_guild(42).candidate_proposal)
        self.pm.no_change = False
        self.pm.wrong_option = True
        with self.assertRaisesRegex(MeetingManagerError, "不一致"):
            await self.service.act(42, 7, "A")

    async def test_apply_failure_retries_saved_selection(self):
        self.pm.fail_apply = True
        with self.assertRaises(MeetingManagerError):
            await self.service.act(42, 7, "A")
        failed = self.repo.get_for_guild(42)
        self.assertEqual(failed.user_decision["selection"]["option_id"], "A")
        self.assertEqual(failed.decision_status, "applying_choice")
        with self.assertRaises(MeetingManagerError):
            await self.new_manager().decision_service.act(42, 7, "B")
        recovered = await self.new_manager().decision_service.act(42, 7, "A")
        self.assertEqual(recovered.decision_status, "awaiting_approval")
        self.assertEqual(recovered.user_decision["selection"], failed.user_decision["selection"])

    async def test_review_retry_reuses_candidate(self):
        self.review.fail_candidate = True
        with self.assertRaises(MeetingManagerError):
            await self.service.act(42, 7, "A")
        self.assertEqual(len(self.pm.apply_calls), 1)
        await self.new_manager().decision_service.act(42, 7, "A")
        self.assertEqual(len(self.pm.apply_calls), 1)

    async def test_revision_reassesses_and_keeps_one_revision_budget(self):
        self.review.needs_revision = True
        result = await self.service.act(42, 7, "A")
        self.assertEqual(result.revision_count, 1)
        self.assertEqual(len(result.user_decision["evaluations"]), 2)
        self.assertEqual(result.user_decision["evaluations"][0]["status"], "需要修改")
        self.assertEqual(result.candidate_review["status"], "通過")
        self.assertEqual(self.pm.apply_calls[-1]["user_choice"]["option_id"], "A")
        await self.service.act(42, 7, "reject")
        b = await self.service.act(42, 7, "B")
        self.assertEqual(b.revision_count, 1)
        self.assertEqual(len(b.user_decision["evaluations"]), 1)
        self.assertEqual(len(b.user_decision["history"][0]["evaluations"]), 2)

    async def test_revision_review_failure_resumes_without_reintegrating(self):
        self.review.needs_revision = True
        self.review.fail_revision_review = True
        with self.assertRaises(MeetingManagerError):
            await self.service.act(42, 7, "A")
        self.assertEqual(len(self.pm.apply_calls), 2)
        result = await self.new_manager().decision_service.act(42, 7, "A")
        self.assertEqual(len(self.pm.apply_calls), 2)
        self.assertEqual(result.candidate_review["status"], "通過")

    async def test_tampered_candidate_or_review_version_cannot_be_approved(self):
        await self.service.act(42, 7, "A")
        record = self.repo.get_for_guild(42)
        record.candidate_proposal["summary"] = "未評分的新內容"
        self.repo.save(record)
        with self.assertRaisesRegex(MeetingManagerError, "對應評分"):
            await self.service.act(42, 7, "approve")

    async def test_waiting_blocks_new_meeting_and_late_second_round(self):
        with self.assertRaisesRegex(MeetingManagerError, "尚待"):
            await self.manager.start_first_round(42, "CUSTOM-2", "另一案", decision_owner_user_id=7)
        from datetime import date
        change = RequirementChange(id="CHG-1", project_id="CUSTOM-1", description="增加功能", reason="需求", requested_at=date.today(), status="pending")
        with self.assertRaisesRegex(MeetingManagerError, "決策階段"):
            await self.manager.start_second_round(42, change)

    async def test_cas_blocks_stale_saves_and_unapproved_final(self):
        stale = self.repo.get_for_guild(42)
        await self.service.act(42, 7, "A")
        with self.assertRaises(MeetingRepositoryError):
            self.repo.save(stale)
        latest = self.repo.get_for_guild(42)
        latest.final_proposal = copy.deepcopy(latest.candidate_proposal)
        with self.assertRaisesRegex(MeetingRepositoryError, "尚未批准"):
            self.repo.save(latest)

    async def test_double_click_and_cross_manager_claim(self):
        self.pm.gate = asyncio.Event()
        task = asyncio.create_task(self.service.act(42, 7, "A"))
        for _ in range(20):
            await asyncio.sleep(0)
            if self.pm.apply_calls:
                break
        with self.assertRaises(MeetingManagerError):
            await self.service.act(42, 7, "B")
        with self.assertRaisesRegex(MeetingManagerError, "正在處理"):
            await self.new_manager().decision_service.act(42, 7, "A")
        self.pm.gate.set()
        await task
        self.assertEqual(len(self.pm.apply_calls), 1)

    async def test_persistent_view_and_restart_restoration(self):
        bot = SimpleNamespace(add_view=lambda view, **kwargs: restored.append((view, kwargs)))
        restored = []
        presenter = DecisionPresenter(bot, self.service, format_proposal_embed, format_review_embed)
        channel = SimpleNamespace(id=100, send=AsyncMock(return_value=SimpleNamespace(id=200)))
        record = self.repo.get_for_guild(42)
        self.assertTrue(await presenter.present(record, channel))
        await presenter.restore()
        self.assertEqual(len(restored), 1)
        self.assertTrue(restored[0][0].is_persistent())
        self.assertTrue(all(len(c.custom_id) <= 100 for c in restored[0][0].children))
        selected = await self.service.act(42, 7, "A", meeting_id=record.meeting_id,
            version=record.decision_version, message_id=200)
        self.assertTrue(ApprovalView(presenter, selected).is_persistent())
        with self.assertRaises(MeetingManagerError):
            await self.service.act(42, 7, "approve", version=record.decision_version, message_id=200)

    async def test_view_checks_owner_and_button_uses_shared_service(self):
        presenter = DecisionPresenter(SimpleNamespace(), self.service, format_proposal_embed, format_review_embed)
        record = self.repo.get_for_guild(42)
        await self.service.remember_message(42, record.meeting_id, record.decision_version, record.decision_status, 100, 200)
        view = DecisionView(presenter, record)
        interaction = SimpleNamespace(guild_id=42, user=SimpleNamespace(id=8), message=SimpleNamespace(id=200, edit=AsyncMock()),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
            channel=SimpleNamespace(id=100, send=AsyncMock(return_value=SimpleNamespace(id=201))))
        self.assertFalse(await view.interaction_check(interaction))
        self.assertFalse(self.pm.apply_calls)
        interaction.user.id = 7
        self.assertTrue(await view.interaction_check(interaction))
        await view.children[0].callback(interaction)
        self.assertEqual(self.repo.get_for_guild(42).decision_status, "awaiting_approval")
        interaction.response.defer.assert_awaited_once()
        self.assertTrue(all(child.disabled for child in view.children))

    async def test_slash_fallback_and_review_redisplay(self):
        bot = create_bot(Settings(discord_token="test"), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), self.manager)
        self.addAsyncCleanup(bot.close)
        channel = SimpleNamespace(id=100, send=AsyncMock(return_value=SimpleNamespace(id=201)))
        interaction = SimpleNamespace(guild_id=42, user=SimpleNamespace(id=7), channel=channel,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await bot.tree.get_command("review").callback(interaction)
        self.assertEqual(self.pm.question_calls, 1)
        await bot.tree.get_command("decide").callback(interaction, "A")
        self.assertEqual(self.repo.get_for_guild(42).decision_status, "awaiting_approval")
        await bot.tree.get_command("decide").callback(interaction, "approve")
        self.assertEqual(self.repo.get_for_guild(42).decision_status, "approved")

    async def test_public_meeting_entry_saves_owner_and_constraints(self):
        from datetime import date
        from models.project import Project, ProjectStatus
        change = RequirementChange(id="CHG-2", project_id="CUSTOM-2", description="補充驗收流程", reason="新需求",
            requested_at=date.today(), status="pending")
        project = Project(id="CUSTOM-2", category="網站", title="另一個測試", requirements=("核心與互動功能",),
            budget=1000, deadline=date(2026, 12, 31), acceptance_criteria=("核心可驗收",),
            status=ProjectStatus.PENDING, requirement_changes=(change,))
        repository = SimpleNamespace(list_for_guild=AsyncMock(return_value=[project]))
        meeting_service = SimpleNamespace(prepare_project=AsyncMock(return_value=project))
        bot = create_bot(Settings(discord_token="test"), SimpleNamespace(), repository, meeting_service, self.manager)
        self.addAsyncCleanup(bot.close)
        channel = SimpleNamespace(id=101, send=AsyncMock(return_value=SimpleNamespace(id=202)))
        interaction = SimpleNamespace(guild_id=99, user=SimpleNamespace(id=8), channel=channel,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await bot.tree.get_command("meeting").callback(interaction, "CUSTOM-2", "CHG-2")
        record = self.repo.get_for_guild(99)
        self.assertEqual(record.decision_owner_user_id, 8)
        self.assertEqual(record.decision_status, "awaiting_choice")
        self.assertIsNone(record.final_proposal)
        self.assertIn("硬性預算上限：1000", record.requirement)
        self.assertIn("硬性交付期限：2026-12-31", record.requirement)
        self.assertEqual(record.applied_requirement_change.id, "CHG-2")

    async def test_missing_source_is_not_accepted(self):
        record = self.repo.get_for_guild(42)
        record.user_decision = None
        record.decision_status = "preparing"
        self.repo.save(record)
        bad = question().model_dump()
        bad["sources"][0]["quote"] = "Agent 從未說過的內容"
        self.pm.create_decision = AsyncMock(return_value=DecisionQuestion.model_validate(bad))
        with self.assertRaisesRegex(MeetingManagerError, "逐字引用"):
            await self.manager.review_and_finalize(42)
        self.assertIsNone(self.repo.get_for_guild(42).user_decision)

    async def test_saved_review_step_is_reused_after_restart(self):
        await self.service.act(42, 7, "A")
        calls = len(self.review.inputs)
        record = self.repo.get_for_guild(42)
        record.candidate_review = None
        record.decision_status = "applying_choice"
        record.user_decision["lease_until"] = 0
        self.repo.save(record)
        await self.new_manager().decision_service.act(42, 7, "A")
        self.assertEqual(len(self.review.inputs), calls)

    async def test_expired_processing_lease_can_resume_after_restart(self):
        record = self.repo.get_for_guild(42)
        record.decision_status = "applying_choice"
        record.user_decision["selection"] = {"option_id": "A", "user_id": 7, "selected_at": "saved"}
        record.user_decision["lease_until"] = time.time()-1
        self.repo.save(record)
        result = await self.new_manager().decision_service.act(42, 7, "A")
        self.assertEqual(result.decision_status, "awaiting_approval")
        self.assertEqual(result.user_decision["selection"]["selected_at"], "saved")

    async def test_message_failure_keeps_choice_available(self):
        presenter = DecisionPresenter(SimpleNamespace(), self.service, format_proposal_embed, format_review_embed)
        response = SimpleNamespace(status=403, reason="Forbidden")
        channel = SimpleNamespace(id=100, send=AsyncMock(side_effect=discord.HTTPException(response, "blocked")))
        self.assertFalse(await presenter.present(self.repo.get_for_guild(42), channel))
        result = await self.service.act(42, 7, "A")
        self.assertEqual(result.decision_status, "awaiting_approval")


class DecisionSchemaTest(unittest.TestCase):
    def test_option_count_ids_and_duplicate_descriptions(self):
        for mutate in (
            lambda q: q.update(options=q["options"][:1]),
            lambda q: q["options"][1].update(option_id="A"),
            lambda q: q["options"][1].update(description=q["options"][0]["description"]),
        ):
            q = question().model_dump()
            mutate(q)
            with self.assertRaises(ValidationError):
                DecisionQuestion.model_validate(q)

    def test_invalid_decision_owner_or_status_is_rejected(self):
        data = MeetingRecord.new("meeting-1", 42, "CUSTOM-1", "測試").to_dict()
        for changes in ({"decision_status": "bad"}, {"decision_status": "awaiting_choice"}, {"decision_owner_user_id": True}):
            with self.assertRaises(ValueError):
                MeetingRecord.from_dict({**data, **changes})


class DecisionAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_agent_wrappers_use_decision_schemas(self):
        from agents.pm_agent import PMAgent
        from tests.test_project_agents import make_response
        choice = UserChoiceProposal(proposal=proposal(), option_id="A",
            decision_impact=[{"section": "execution_plan", "reason": "調整順序"}])
        llm = SimpleNamespace(chat=AsyncMock(side_effect=[
            make_response(question().model_dump_json()), make_response(choice.model_dump_json())]))
        agent = PMAgent(llm)
        result = await agent.create_decision("討論資料")
        self.assertEqual(len(result.options), 2)
        applied = await agent.apply_decision("選擇 A")
        self.assertEqual(applied.output.option_id, "A")
        self.assertEqual(applied.max_output_tokens, 1800)
        self.assertEqual(llm.chat.await_count, 2)
