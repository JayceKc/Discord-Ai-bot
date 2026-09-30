import asyncio
import copy
import json
import unittest
from datetime import date
from unittest.mock import AsyncMock

from pydantic import BaseModel

from models.meeting import (
    AgentSuggestion,
    MeetingRecord,
    MeetingStatus,
    MeetingStepRecord,
)
from models.project import RequirementChange
from services.meeting_manager import MeetingManager, MeetingManagerError, ReviewWorkflowResult


class FakeResult(BaseModel):
    """模擬所有專業 Agent 都會回傳的 Pydantic 結構。"""

    agent: str


class PMFakeResult(BaseModel):
    goal: str
    constraints: list[str]
    work_items: list[str]
    disagreements: list[str]


class ResearchFakeResult(BaseModel):
    known_information: list[str]
    reasonable_inferences: list[str]
    items_to_verify: list[str]


class CreativeFakeResult(BaseModel):
    proposals: list[dict[str, object]]


class FinanceFakeResult(BaseModel):
    cost_considerations: list[str]
    constraints: list[str]
    risks: list[str]
    alternatives: list[str]


class ProposalFakeResult(BaseModel):
    title: str
    summary: str
    sections: dict[str, str]
    decisions: list[dict[str, object]]


class ReviewFakeResult(BaseModel):
    status: str
    checklist: dict[str, dict[str, object]]
    issues: list[dict[str, object]]
    revision_allowed: bool


class FakeAgent:
    """記錄名稱與輸入，不會連線到 Ollama。"""

    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls
        self.inputs: list[str] = []

    async def respond(self, user_input: str) -> FakeResult:
        self.calls.append(self.name)
        self.inputs.append(user_input)
        return FakeResult(agent=self.name)


class StaticResultAgent(FakeAgent):
    """回傳指定結構，方便檢查共享內容挑選與來源紀錄。"""

    def __init__(
        self,
        name: str,
        calls: list[str],
        result: BaseModel,
    ) -> None:
        super().__init__(name, calls)
        self.result = result

    async def respond(self, user_input: str) -> BaseModel:
        self.calls.append(self.name)
        self.inputs.append(user_input)
        return self.result


class IntegratingPMAgent(FakeAgent):
    """同時支援一般 PM 回應與 DAY 19 草案整合。"""

    def __init__(self, calls: list[str]) -> None:
        super().__init__("PM Agent", calls)
        self.integration_inputs: list[str] = []

    async def integrate(self, meeting_input: str) -> ProposalFakeResult:
        self.integration_inputs.append(meeting_input)
        return ProposalFakeResult(
            title="Discord AI 專案提案",
            summary="整合兩輪意見後採用分階段交付。",
            sections={
                "background_and_goal": "完成 Discord AI Bot。",
                "integrated_solution": "整合研究、創意與財務建議。",
                "execution_plan": "先完成 MVP。",
                "risks_and_responses": "透過測試降低風險。",
                "acceptance_criteria": "指令可正常使用。",
            },
            decisions=[
                {
                    "topic": "交付方式",
                    "decision": "折衷",
                    "reason": "平衡時程與品質。",
                    "sources": ["Finance Agent 第 2 輪"],
                }
            ],
        )


class BlockingAgent(FakeAgent):
    """停在指定 Agent，讓測試有時間發出重複啟動或取消。"""

    def __init__(self, name: str, calls: list[str]) -> None:
        super().__init__(name, calls)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def respond(self, user_input: str) -> FakeResult:
        self.calls.append(self.name)
        self.inputs.append(user_input)
        self.started.set()
        await self.release.wait()
        return FakeResult(agent=self.name)


class FailOnceAgent(FakeAgent):
    """第一次失敗、恢復時成功，用來確認續跑位置。"""

    def __init__(self, name: str, calls: list[str]) -> None:
        super().__init__(name, calls)
        self.has_failed = False

    async def respond(self, user_input: str) -> FakeResult:
        self.calls.append(self.name)
        self.inputs.append(user_input)
        if not self.has_failed:
            self.has_failed = True
            raise RuntimeError("測試用暫時失敗")
        return FakeResult(agent=self.name)


class ConcurrentAgent(FakeAgent):
    """兩個 Guild 都進入 PM 後才釋放，用來證明鎖不是全域鎖。"""

    def __init__(self, name: str, calls: list[str]) -> None:
        super().__init__(name, calls)
        self.active_count = 0
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()

    async def respond(self, user_input: str) -> FakeResult:
        self.calls.append(self.name)
        self.inputs.append(user_input)
        self.active_count += 1
        if self.active_count == 2:
            self.both_started.set()
        await self.release.wait()
        return FakeResult(agent=self.name)


class MemoryMeetingRepository:
    """保存深拷貝快照，讓測試能觀察每次 save 當下的狀態。"""

    def __init__(self) -> None:
        self.records: dict[str, MeetingRecord] = {}
        self.guilds: dict[int, str] = {}
        self.snapshots: list[MeetingRecord] = []

    def save(self, record: MeetingRecord) -> None:
        saved = copy.deepcopy(record)
        self.records[record.meeting_id] = saved
        self.guilds[record.guild_id] = record.meeting_id
        self.snapshots.append(saved)

    def get(self, meeting_id: str) -> MeetingRecord | None:
        record = self.records.get(meeting_id)
        return copy.deepcopy(record) if record is not None else None

    def get_for_guild(self, guild_id: int) -> MeetingRecord | None:
        meeting_id = self.guilds.get(guild_id)
        if meeting_id is None:
            return None
        return copy.deepcopy(self.records[meeting_id])


def build_manager(
    repository: MemoryMeetingRepository,
    calls: list[str],
    **replacements: FakeAgent,
) -> MeetingManager:
    """以五個 Fake Agent 建立可替換任一步驟的 Manager。"""

    agents: dict[str, FakeAgent] = {
        "pm_agent": FakeAgent("PM Agent", calls),
        "research_agent": FakeAgent("Research Agent", calls),
        "creative_agent": FakeAgent("Creative Agent", calls),
        "finance_agent": FakeAgent("Finance Agent", calls),
        "review_agent": FakeAgent("Review Agent", calls),
    }
    agents.update(replacements)
    return MeetingManager(
        repository,
        agents["pm_agent"],
        agents["research_agent"],
        agents["creative_agent"],
        agents["finance_agent"],
        agents["review_agent"],
        meeting_id_factory=lambda: f"meeting-{len(repository.records) + 1}",
    )


class MeetingManagerTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def requirement_change() -> RequirementChange:
        """建立固定需求變更，供第二輪測試共用。"""

        return RequirementChange(
            id="CHG-TEST",
            project_id="PRJ-001",
            description="預算縮減 20%，必須保留核心功能",
            reason="客戶調整本季預算",
            requested_at=date(2026, 9, 17),
            status="待評估",
        )

    async def test_new_meeting_can_follow_previous_final_proposal(self) -> None:
        repository = MemoryMeetingRepository()
        previous = MeetingRecord.new("old", 123, "PRJ-001", "舊需求")
        previous.status = MeetingStatus.COMPLETED
        previous.final_proposal = {"title": "舊案最終方案"}
        repository.save(previous)
        manager = build_manager(repository, [])

        next_record = await manager.start_first_round(123, "PRJ-002", "新需求")

        self.assertEqual(next_record.project_id, "PRJ-002")
        self.assertEqual(repository.get("old").final_proposal["title"], "舊案最終方案")
        self.assertEqual(repository.get_for_guild(123).meeting_id, next_record.meeting_id)

    async def test_full_workflow_starts_new_project_after_previous_final(self) -> None:
        repository = MemoryMeetingRepository()
        manager = build_manager(repository, [])
        previous = MeetingRecord.new("old", 123, "PRJ-001", "舊需求")
        previous.status = MeetingStatus.COMPLETED
        previous.final_proposal = {"title": "舊案"}
        next_record = MeetingRecord.new("new", 123, "PRJ-002", "新需求")
        next_record.status = MeetingStatus.COMPLETED
        next_record.final_proposal = {"title": "新案"}
        manager._get_for_guild = AsyncMock(side_effect=[previous, next_record])
        manager.start_first_round = AsyncMock(return_value=next_record)
        manager.start_second_round = AsyncMock(return_value=next_record)
        manager.create_proposal_draft = AsyncMock(return_value={"title": "草案"})
        manager.review_and_finalize = AsyncMock(return_value=ReviewWorkflowResult(
            review={"status": "通過"}, revision_performed=False,
            revision_agent_name=None, final_proposal=next_record.final_proposal,
        ))
        change = RequirementChange("CHG-NEW", "PRJ-002", "增加需求", "測試", date.today(), "待評估")

        result = await manager.run_full_meeting(123, "PRJ-002", "新需求", change)

        manager.start_first_round.assert_awaited_once_with(123, "PRJ-002", "新需求", on_step=None)
        self.assertEqual(result.record.meeting_id, "new")

    async def test_second_round_applies_one_change_and_calls_each_agent_once(self) -> None:
        """第二輪保存需求變更，四位 Agent 各回應一次並建立摘要。"""

        calls: list[str] = []
        progress: list[tuple[int, int, str]] = []
        repository = MemoryMeetingRepository()
        manager = build_manager(repository, calls)
        await manager.start_first_round(123, "PRJ-001", "建立 Discord Bot")

        async def record_progress(
            current: int,
            total: int,
            step: MeetingStepRecord,
        ) -> None:
            progress.append((current, total, step.agent_name))

        result = await manager.start_second_round(
            123,
            self.requirement_change(),
            on_step=record_progress,
        )

        self.assertEqual(
            calls,
            [
                "PM Agent",
                "Research Agent",
                "Creative Agent",
                "Finance Agent",
                "PM Agent",
                "Research Agent",
                "Creative Agent",
                "Finance Agent",
            ],
        )
        self.assertEqual(
            progress,
            [
                (1, 4, "PM Agent"),
                (2, 4, "Research Agent"),
                (3, 4, "Creative Agent"),
                (4, 4, "Finance Agent"),
            ],
        )
        self.assertEqual(result.applied_requirement_change, self.requirement_change())
        self.assertEqual([step.round_number for step in result.steps], [1] * 4 + [2] * 4)
        self.assertEqual(
            [summary.round_number for summary in result.meeting_context.round_summaries],
            [1, 2],
        )
        second_round_input = json.loads(result.steps[4].input_text or "{}")
        self.assertEqual(second_round_input["requirement_change"]["id"], "CHG-TEST")
        self.assertIn("不得只回覆", second_round_input["response_rules"]["forbidden_response"])
        for step in result.steps[4:]:
            second_round_context = json.loads(
                step.input_text or "{}"
            )["meeting_context"]
            self.assertEqual(
                set(second_round_context),
                {"previous_outputs"},
            )

    async def test_second_round_rejects_a_second_requirement_change(self) -> None:
        """同一場會議最多只能套用一次需求變更。"""

        calls: list[str] = []
        repository = MemoryMeetingRepository()
        manager = build_manager(repository, calls)
        await manager.start_first_round(123, "PRJ-001", "建立 Discord Bot")
        await manager.start_second_round(123, self.requirement_change())

        with self.assertRaisesRegex(MeetingManagerError, "已經加入過一次"):
            await manager.start_second_round(123, self.requirement_change())

    async def test_pm_integrates_two_rounds_and_saves_proposal_decisions(self) -> None:
        """完成兩輪後，PM 收到摘要與重要原文並保存整合草案。"""

        calls: list[str] = []
        repository = MemoryMeetingRepository()
        pm = IntegratingPMAgent(calls)
        manager = build_manager(repository, calls, pm_agent=pm)
        await manager.start_first_round(123, "PRJ-001", "建立 Discord Bot")
        await manager.start_second_round(123, self.requirement_change())

        record = repository.get_for_guild(123)
        assert record is not None
        record.meeting_context.suggestions.extend(
            [
                AgentSuggestion(
                    "Creative Agent",
                    "proposals",
                    "加入互動式導覽",
                    round_number=1,
                ),
                AgentSuggestion(
                    "Finance Agent",
                    "alternatives",
                    "改為分階段交付",
                    round_number=2,
                ),
            ]
        )
        repository.save(record)

        proposal = await manager.create_proposal_draft(123)

        self.assertEqual(proposal["title"], "Discord AI 專案提案")
        self.assertEqual(proposal["decisions"][0]["decision"], "折衷")
        saved = repository.get_for_guild(123)
        assert saved is not None
        self.assertEqual(saved.proposal_draft, proposal)
        self.assertIsNotNone(saved.proposal_metrics)
        assert saved.proposal_metrics is not None
        self.assertGreater(saved.proposal_metrics.input_characters, 0)
        self.assertGreater(saved.proposal_metrics.output_characters, 0)
        self.assertGreaterEqual(saved.proposal_metrics.execution_time_seconds, 0)
        self.assertEqual(
            await manager.get_proposal_metrics(123),
            saved.proposal_metrics,
        )
        integration_input = json.loads(pm.integration_inputs[0])
        self.assertEqual(
            {item["round_number"] for item in integration_input["round_summaries"]},
            {1, 2},
        )
        self.assertEqual(
            {item["content"] for item in integration_input["important_originals"]},
            {"加入互動式導覽", "改為分階段交付"},
        )

        cached = await manager.create_proposal_draft(123)
        self.assertEqual(cached, proposal)
        self.assertEqual(len(pm.integration_inputs), 1)

    async def test_first_round_draft_is_rebuilt_after_real_change(self) -> None:
        calls: list[str] = []
        repository = MemoryMeetingRepository()
        pm = IntegratingPMAgent(calls)
        manager = build_manager(repository, calls, pm_agent=pm)
        await manager.start_first_round(123, "PRJ-001", "建立網站")

        first_draft = await manager.create_proposal_draft(123)
        self.assertEqual(len(pm.integration_inputs), 1)
        self.assertEqual(
            {item["round_number"] for item in json.loads(pm.integration_inputs[0])["round_summaries"]},
            {1},
        )
        await manager.start_second_round(123, self.requirement_change())
        after_change = repository.get_for_guild(123)
        self.assertIsNone(after_change.proposal_draft)
        second_draft = await manager.create_proposal_draft(123)
        self.assertEqual(first_draft, second_draft)  # Fake PM 回相同內容，但確實重新呼叫。
        self.assertEqual(len(pm.integration_inputs), 2)
        self.assertEqual(
            {item["round_number"] for item in json.loads(pm.integration_inputs[1])["round_summaries"]},
            {1, 2},
        )

    async def test_first_round_can_finalize_without_second_round(self) -> None:
        calls: list[str] = []
        repository = MemoryMeetingRepository()
        review = StaticResultAgent(
            "Review Agent", calls,
            ReviewFakeResult(status="通過", checklist={
                key: {"score": 4, "reason": "可執行"}
                for key in ("completeness", "creativity", "credibility", "feasibility")
            }, issues=[], revision_allowed=True),
        )
        manager = build_manager(
            repository, calls, pm_agent=IntegratingPMAgent(calls), review_agent=review,
        )
        await manager.start_first_round(123, "PRJ-001", "建立網站")
        await manager.create_proposal_draft(123)

        result = await manager.review_and_finalize(123)

        self.assertEqual(result.review["status"], "通過")
        self.assertIsNotNone(repository.get_for_guild(123).final_proposal)
        self.assertFalse(any(step.round_number == 2 for step in repository.get_for_guild(123).steps))

    async def test_review_sends_high_priority_change_to_agent_only_once(self) -> None:
        """不完整草案只退給指定 Agent 一次，再由 PM 產生最終方案。"""

        calls: list[str] = []
        repository = MemoryMeetingRepository()
        pm = IntegratingPMAgent(calls)
        creative = StaticResultAgent(
            "Creative Agent",
            calls,
            CreativeFakeResult(
                proposals=[
                    {
                        "title": "修正版方案",
                        "description": "補上明確操作流程",
                        "research_basis": ["Review Agent 的高優先要求"],
                        "implementation_steps": ["補充步驟"],
                    }
                ]
            ),
        )
        review = StaticResultAgent(
            "Review Agent",
            calls,
            ReviewFakeResult(
                status="需要修改",
                checklist={
                    "completeness": {"score": 2, "reason": "缺少操作步驟"},
                    "creativity": {"score": 4, "reason": "方案具體"},
                    "credibility": {"score": 4, "reason": "來源清楚"},
                    "feasibility": {"score": 4, "reason": "可執行"},
                },
                issues=[
                    {
                        "problem": "缺少操作步驟",
                        "required_change": "補上具體實作步驟",
                        "priority": "高",
                        "assigned_agent": "Creative Agent",
                    }
                ],
                revision_allowed=True,
            ),
        )
        manager = build_manager(
            repository,
            calls,
            pm_agent=pm,
            creative_agent=creative,
            review_agent=review,
        )
        await manager.start_first_round(123, "PRJ-001", "建立 Discord Bot")
        await manager.start_second_round(123, self.requirement_change())
        await manager.create_proposal_draft(123)

        result = await manager.review_and_finalize(123)

        self.assertTrue(result.revision_performed)
        self.assertEqual(result.revision_agent_name, "Creative Agent")
        saved = repository.get_for_guild(123)
        assert saved is not None
        self.assertEqual(saved.revision_count, 1)
        self.assertEqual(saved.revision_agent_name, "Creative Agent")
        self.assertIsNotNone(saved.revision_output)
        self.assertIsNotNone(saved.final_proposal)
        review_steps = [step for step in saved.steps if step.round_number == 3]
        self.assertEqual(
            [step.agent_name for step in review_steps],
            ["Review Agent", "Creative Agent", "Review Agent"],
        )
        final_review = saved.review_result["post_revision_review"]
        self.assertEqual(final_review["status"], "需要修改")
        self.assertEqual(final_review["quality_evaluation"]["evaluated_artifact"], "final_proposal")
        self.assertEqual(json.loads(review.inputs[-1])["revision_count"], 1)
        self.assertEqual(
            json.loads(json.loads(review.inputs[-1])["draft"]),
            saved.final_proposal,
        )

        step_count = len(saved.steps)
        cached = await manager.review_and_finalize(123)
        self.assertEqual(cached.final_proposal, result.final_proposal)
        saved_again = repository.get_for_guild(123)
        assert saved_again is not None
        self.assertEqual(len(saved_again.steps), step_count)
        self.assertEqual(saved_again.revision_count, 1)

    async def test_review_passes_without_calling_revision_agent(self) -> None:
        """四項評估都通過時直接採用初稿，不消耗修改機會。"""

        calls: list[str] = []
        repository = MemoryMeetingRepository()
        pm = IntegratingPMAgent(calls)
        passed_item = {"score": 4, "reason": "符合要求"}
        review = StaticResultAgent(
            "Review Agent",
            calls,
            ReviewFakeResult(
                status="通過",
                checklist={
                    "completeness": passed_item,
                    "creativity": passed_item,
                    "credibility": passed_item,
                    "feasibility": passed_item,
                },
                issues=[],
                revision_allowed=False,
            ),
        )
        manager = build_manager(
            repository,
            calls,
            pm_agent=pm,
            review_agent=review,
        )
        await manager.start_first_round(123, "PRJ-001", "建立 Discord Bot")
        await manager.start_second_round(123, self.requirement_change())
        draft = await manager.create_proposal_draft(123)

        result = await manager.review_and_finalize(123)

        self.assertFalse(result.revision_performed)
        self.assertEqual(result.final_proposal, draft)
        saved = repository.get_for_guild(123)
        assert saved is not None
        self.assertEqual(saved.revision_count, 0)
        self.assertEqual(
            [step.agent_name for step in saved.steps if step.round_number == 3],
            ["Review Agent"],
        )

    def test_second_round_rejects_agreement_only_output(self) -> None:
        """只說同意不算有意義的第二輪內容。"""

        with self.assertRaisesRegex(MeetingManagerError, "補充、反對或修正"):
            MeetingManager._validate_second_round_output({"answer": "我同意。"})

    async def test_first_round_builds_shared_context_with_selected_sources(self) -> None:
        """後發言 Agent 只收到需要的前文，並保存摘要與建議來源。"""

        calls: list[str] = []
        repository = MemoryMeetingRepository()
        pm = StaticResultAgent(
            "PM Agent",
            calls,
            PMFakeResult(
                goal="完成營運儀表板",
                constraints=["預算有限"],
                work_items=["建立營收圖表"],
                disagreements=[],
            ),
        )
        research = StaticResultAgent(
            "Research Agent",
            calls,
            ResearchFakeResult(
                known_information=["需要每日營收"],
                reasonable_inferences=["需要圖表元件"],
                items_to_verify=["確認資料來源"],
            ),
        )
        creative = StaticResultAgent(
            "Creative Agent",
            calls,
            CreativeFakeResult(
                proposals=[
                    {
                        "title": "營運快照",
                        "description": "使用卡片顯示關鍵數字",
                        "research_basis": ["需要每日營收"],
                        "implementation_steps": ["建立卡片元件"],
                    }
                ]
            ),
        )
        finance = StaticResultAgent(
            "Finance Agent",
            calls,
            FinanceFakeResult(
                cost_considerations=["控制圖表套件成本"],
                constraints=["預算有限"],
                risks=["資料延遲"],
                alternatives=["先製作基本圖表"],
            ),
        )
        manager = build_manager(
            repository,
            calls,
            pm_agent=pm,
            research_agent=research,
            creative_agent=creative,
            finance_agent=finance,
        )

        result = await manager.start_first_round(
            123,
            "PRJ-003",
            "建立每日營收儀表板",
        )

        research_context = json.loads(research.inputs[0])["meeting_context"]
        creative_context = json.loads(creative.inputs[0])["meeting_context"]
        finance_context = json.loads(finance.inputs[0])["meeting_context"]
        self.assertEqual(
            set(research_context["previous_outputs"]),
            {"PM Agent"},
        )
        self.assertEqual(
            set(creative_context["previous_outputs"]),
            {"Research Agent"},
        )
        self.assertEqual(
            set(finance_context["previous_outputs"]),
            {"PM Agent", "Research Agent", "Creative Agent"},
        )
        self.assertEqual(
            set(finance_context["previous_outputs"]["PM Agent"]),
            {"constraints"},
        )
        self.assertTrue(result.meeting_context.round_summaries)
        self.assertEqual(result.meeting_context.round_summaries[0].round_number, 1)
        self.assertEqual(
            {item.agent_name for item in result.meeting_context.suggestions},
            {"PM Agent", "Research Agent", "Creative Agent", "Finance Agent"},
        )

    async def test_prompt_and_response_character_budgets_are_enforced(self) -> None:
        """輸入或輸出超過會議預算時，不繼續執行後續 Agent。"""

        prompt_calls: list[str] = []
        prompt_repository = MemoryMeetingRepository()
        prompt_manager = build_manager(prompt_repository, prompt_calls)
        prompt_manager.max_prompt_characters = 5

        with self.assertRaisesRegex(MeetingManagerError, "PM Agent"):
            await prompt_manager.start(123, "PRJ-001", "超過五個字的專案需求")
        self.assertEqual(prompt_calls, [])

        response_calls: list[str] = []
        response_repository = MemoryMeetingRepository()
        response_manager = build_manager(response_repository, response_calls)
        response_manager.max_response_characters = 5

        with self.assertRaisesRegex(MeetingManagerError, "PM Agent"):
            await response_manager.start(456, "PRJ-001", "需求")
        self.assertEqual(response_calls, ["PM Agent"])

    async def test_first_round_runs_four_agents_and_reports_progress(self) -> None:
        """第一輪只執行 PM、Research、Creative、Finance，並依序回報進度。"""

        calls: list[str] = []
        progress: list[tuple[int, int, str]] = []
        repository = MemoryMeetingRepository()
        manager = build_manager(repository, calls)

        async def record_progress(
            current: int,
            total: int,
            step: MeetingStepRecord,
        ) -> None:
            progress.append((current, total, step.agent_name))

        result = await manager.start_first_round(
            123,
            "PRJ-001",
            "建立 Discord Bot",
            on_step=record_progress,
        )

        self.assertEqual(
            calls,
            ["PM Agent", "Research Agent", "Creative Agent", "Finance Agent"],
        )
        self.assertEqual(
            progress,
            [
                (1, 4, "PM Agent"),
                (2, 4, "Research Agent"),
                (3, 4, "Creative Agent"),
                (4, 4, "Finance Agent"),
            ],
        )
        self.assertEqual(len(result.steps), 4)
        self.assertEqual(result.status, MeetingStatus.COMPLETED)
        self.assertTrue(
            all(step.execution_time_seconds is not None for step in result.steps)
        )
        self.assertTrue(
            all(step.input_characters is not None for step in result.steps)
        )
        self.assertTrue(
            all(step.output_characters is not None for step in result.steps)
        )

    async def test_calls_agents_in_fixed_order_and_saves_io(self) -> None:
        calls: list[str] = []
        repository = MemoryMeetingRepository()
        agents = [
            FakeAgent("PM Agent", calls),
            FakeAgent("Research Agent", calls),
            FakeAgent("Creative Agent", calls),
            FakeAgent("Finance Agent", calls),
            FakeAgent("Review Agent", calls),
        ]
        manager = MeetingManager(
            repository,
            *agents,
            meeting_id_factory=lambda: "meeting-1",
        )

        result = await manager.start(123, "PRJ-001", "建立 Discord Bot")

        self.assertEqual(
            calls,
            [
                "PM Agent",
                "Research Agent",
                "Creative Agent",
                "Finance Agent",
                "Review Agent",
            ],
        )
        self.assertEqual(result.status, MeetingStatus.COMPLETED)
        self.assertTrue(all(step.input_text for step in result.steps))
        self.assertTrue(all(step.output_data for step in result.steps))

        # 每位 Agent 呼叫前都必須先保存 input，當時 output 尚未產生。
        for index in range(5):
            self.assertTrue(
                any(
                    snapshot.steps[index].status.value == "running"
                    and snapshot.steps[index].input_text is not None
                    and snapshot.steps[index].output_data is None
                    for snapshot in repository.snapshots
                )
            )

        review_input = json.loads(result.steps[4].input_text or "{}")
        self.assertEqual(review_input["project_id"], "PRJ-001")
        self.assertEqual(review_input["revision_count"], 0)
        self.assertIn("draft", review_input)

    async def test_same_guild_cannot_start_twice(self) -> None:
        calls: list[str] = []
        repository = MemoryMeetingRepository()
        blocker = BlockingAgent("PM Agent", calls)
        manager = build_manager(repository, calls, pm_agent=blocker)
        first = asyncio.create_task(
            manager.start(123, "PRJ-001", "建立 Discord Bot")
        )
        await blocker.started.wait()

        with self.assertRaisesRegex(MeetingManagerError, "正在執行"):
            await manager.start(123, "PRJ-002", "建立網站")

        blocker.release.set()
        await first

    async def test_different_guilds_can_run_at_the_same_time(self) -> None:
        calls: list[str] = []
        repository = MemoryMeetingRepository()
        concurrent = ConcurrentAgent("PM Agent", calls)
        manager = build_manager(repository, calls, pm_agent=concurrent)
        first = asyncio.create_task(manager.start(123, "PRJ-001", "建立 Bot"))
        second = asyncio.create_task(manager.start(456, "PRJ-002", "建立網站"))

        await asyncio.wait_for(concurrent.both_started.wait(), timeout=1)
        concurrent.release.set()
        first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(first_result.status, MeetingStatus.COMPLETED)
        self.assertEqual(second_result.status, MeetingStatus.COMPLETED)

    async def test_failure_is_saved_and_resume_starts_from_failed_agent(self) -> None:
        calls: list[str] = []
        repository = MemoryMeetingRepository()
        finance = FailOnceAgent("Finance Agent", calls)
        manager = build_manager(repository, calls, finance_agent=finance)

        with self.assertRaisesRegex(MeetingManagerError, "Finance Agent"):
            await manager.start(123, "PRJ-001", "建立 Bot")

        failed = repository.get_for_guild(123)
        self.assertIsNotNone(failed)
        assert failed is not None
        self.assertEqual(failed.status, MeetingStatus.FAILED)
        self.assertEqual(failed.current_step_index, 3)
        self.assertTrue(all(step.output_data for step in failed.steps[:3]))
        self.assertIsNotNone(failed.steps[3].execution_time_seconds)
        self.assertIn("耗時", failed.steps[3].error or "")

        result = await manager.resume(123)

        self.assertEqual(result.status, MeetingStatus.COMPLETED)
        self.assertEqual(
            calls,
            [
                "PM Agent",
                "Research Agent",
                "Creative Agent",
                "Finance Agent",
                "Finance Agent",
                "Review Agent",
            ],
        )

    async def test_cancel_stops_current_meeting(self) -> None:
        calls: list[str] = []
        repository = MemoryMeetingRepository()
        blocker = BlockingAgent("PM Agent", calls)
        manager = build_manager(repository, calls, pm_agent=blocker)
        running = asyncio.create_task(
            manager.start(123, "PRJ-001", "建立 Discord Bot")
        )
        await blocker.started.wait()

        cancelled = await manager.cancel(123)

        self.assertEqual(cancelled.status, MeetingStatus.CANCELLED)
        with self.assertRaises(asyncio.CancelledError):
            await running
        saved = repository.get_for_guild(123)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.status, MeetingStatus.CANCELLED)
        self.assertEqual(calls, ["PM Agent"])


if __name__ == "__main__":
    unittest.main()
