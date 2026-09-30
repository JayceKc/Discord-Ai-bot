"""DAY 21：從兩輪討論到最終方案的端到端測試。"""

import copy
import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from pydantic import BaseModel

from agents.errors import AgentInvalidJSONError, AgentTimeoutError
from bot import safe_followup_send
from models.meeting import MeetingRecord
from models.project import RequirementChange
from services.meeting_manager import MeetingManager


class AgentOutput(BaseModel):
    """測試用的通用結構化 Agent 輸出。"""

    conclusion: str


class ProposalOutput(BaseModel):
    """測試用 PM 整合輸出。"""

    title: str
    summary: str
    sections: dict[str, str]
    decisions: list[dict[str, str]]


class ReviewOutput(BaseModel):
    """測試用 Review 結構化輸出。"""

    status: str
    checklist: dict[str, dict[str, object]]
    issues: list[dict[str, str]]
    revision_allowed: bool


class MemoryRepository:
    """只保存深拷貝快照的會議 Repository。"""

    def __init__(self) -> None:
        self.records: dict[str, MeetingRecord] = {}
        self.guilds: dict[int, str] = {}

    def save(self, record: MeetingRecord) -> None:
        self.records[record.meeting_id] = copy.deepcopy(record)
        self.guilds[record.guild_id] = record.meeting_id

    def get_for_guild(self, guild_id: int) -> MeetingRecord | None:
        meeting_id = self.guilds.get(guild_id)
        if meeting_id is None:
            return None
        return copy.deepcopy(self.records[meeting_id])


class ScriptedAgent:
    """可在指定次數注入可重試錯誤的 Agent。"""

    def __init__(
        self,
        name: str,
        calls: list[str],
        *,
        failures: list[Exception] | None = None,
    ) -> None:
        self.name = name
        self.calls = calls
        self.failures = list(failures or [])
        self.integration_calls = 0

    async def respond(self, prompt: str) -> BaseModel:
        self.calls.append(self.name)
        if self.failures:
            raise self.failures.pop(0)
        return AgentOutput(conclusion=f"{self.name} 的具體建議")

    async def integrate(self, prompt: str) -> ProposalOutput:
        self.integration_calls += 1
        suffix = "最終" if self.integration_calls > 1 else "草案"
        return ProposalOutput(
            title=f"端到端測試{suffix}",
            summary="完整會議已產生可執行方案。",
            sections={"execution_plan": "分階段交付"},
            decisions=[{"topic": "範圍", "decision": "採用", "reason": "可行"}],
        )


class ScriptedReviewAgent(ScriptedAgent):
    """依測試情境回傳通過或一次修改要求。"""

    def __init__(self, calls: list[str], *, needs_revision: bool = False) -> None:
        super().__init__("Review Agent", calls)
        self.needs_revision = needs_revision

    async def respond(self, prompt: str) -> ReviewOutput:
        self.calls.append(self.name)
        passing = {
            "completeness": {"score": 4, "reason": "完整"},
            "creativity": {"score": 4, "reason": "具體"},
            "credibility": {"score": 4, "reason": "可信"},
            "feasibility": {"score": 4, "reason": "可行"},
        }
        if self.needs_revision and json.loads(prompt)["revision_count"] == 0:
            failing = dict(passing)
            failing["completeness"] = {"score": 2, "reason": "缺少步驟"}
            return ReviewOutput(
                status="需要修改",
                checklist=failing,
                issues=[
                    {
                        "problem": "步驟不足",
                        "required_change": "補上交付步驟",
                        "priority": "高",
                        "assigned_agent": "Creative Agent",
                    }
                ],
                revision_allowed=True,
            )
        return ReviewOutput(
            status="通過",
            checklist=passing,
            issues=[],
            revision_allowed=False,
        )


def build_manager(
    *,
    pm_failures: list[Exception] | None = None,
    needs_revision: bool = False,
) -> tuple[MeetingManager, list[str], MemoryRepository]:
    calls: list[str] = []
    repository = MemoryRepository()
    manager = MeetingManager(
        repository,
        ScriptedAgent("PM Agent", calls, failures=pm_failures),
        ScriptedAgent("Research Agent", calls),
        ScriptedAgent("Creative Agent", calls),
        ScriptedAgent("Finance Agent", calls),
        ScriptedReviewAgent(calls, needs_revision=needs_revision),
        meeting_id_factory=lambda: "day21-e2e",
        retry_delay_seconds=0,
    )
    return manager, calls, repository


def change() -> RequirementChange:
    return RequirementChange(
        id="CHG-001",
        project_id="PRJ-001",
        description="預算縮減 20%，保留核心功能",
        reason="客戶調整預算",
        requested_at=date(2026, 9, 22),
        status="待評估",
    )


class Day21EndToEndTest(unittest.IsolatedAsyncioTestCase):
    """證明單一服務入口能完成驗收所需的完整會議。"""

    async def test_full_meeting_passes_review_and_saves_final_proposal(self) -> None:
        manager, calls, repository = build_manager()

        result = await manager.run_full_meeting(
            1001, "PRJ-001", "建立 Discord AI 助理", change()
        )

        self.assertEqual(result.review["status"], "通過")
        self.assertFalse(result.revision_performed)
        self.assertEqual(result.record.revision_count, 0)
        self.assertIsNotNone(result.record.final_proposal)
        evaluation = result.review["quality_evaluation"]
        self.assertEqual(evaluation["formula_version"], "day24-v1")
        self.assertEqual(evaluation["outputs"]["quality_index"], 80.0)
        self.assertEqual(evaluation["priority"]["value"], "growth")
        self.assertEqual(
            repository.get_for_guild(1001).review_result["quality_evaluation"],
            evaluation,
        )
        self.assertEqual(calls[:8], [
            "PM Agent", "Research Agent", "Creative Agent", "Finance Agent",
            "PM Agent", "Research Agent", "Creative Agent", "Finance Agent",
        ])
        self.assertEqual(repository.get_for_guild(1001).final_proposal, result.final_proposal)

    async def test_full_meeting_revises_once_then_reuses_final_result(self) -> None:
        manager, calls, _ = build_manager(needs_revision=True)

        first = await manager.run_full_meeting(
            1002, "PRJ-001", "建立 Discord AI 助理", change()
        )
        call_count = len(calls)
        second = await manager.run_full_meeting(
            1002, "PRJ-001", "建立 Discord AI 助理", change()
        )

        self.assertTrue(first.revision_performed)
        self.assertEqual(first.revision_agent_name, "Creative Agent")
        self.assertEqual(first.record.revision_count, 1)
        self.assertEqual(calls.count("Creative Agent"), 3)
        self.assertEqual(calls.count("Review Agent"), 2)
        self.assertEqual(first.review["quality_evaluation"]["outputs"]["quality_index"], 68.0)
        self.assertEqual(
            first.review["post_revision_review"]["quality_evaluation"]["outputs"]["quality_index"],
            80.0,
        )
        self.assertEqual(
            first.review["post_revision_review"]["quality_evaluation"]["evaluated_artifact"],
            "final_proposal",
        )
        self.assertEqual(len(calls), call_count)
        self.assertEqual(second.final_proposal, first.final_proposal)

    async def test_timeout_is_retried_without_replaying_completed_steps(self) -> None:
        manager, calls, _ = build_manager(
            pm_failures=[AgentTimeoutError("逾時")]
        )

        result = await manager.run_full_meeting(
            1003, "PRJ-001", "建立 Discord AI 助理", change()
        )

        self.assertIsNotNone(result.final_proposal)
        self.assertEqual(calls.count("PM Agent"), 3)  # 首輪重試一次 + 第二輪一次。
        self.assertEqual(calls.count("Research Agent"), 2)
        self.assertEqual(calls.count("Creative Agent"), 2)
        self.assertEqual(calls.count("Finance Agent"), 2)

    async def test_invalid_json_is_retried_and_final_proposal_is_saved(self) -> None:
        manager, calls, repository = build_manager(
            pm_failures=[AgentInvalidJSONError("無效 JSON")]
        )

        result = await manager.run_full_meeting(
            1004, "PRJ-001", "建立 Discord AI 助理", change()
        )

        self.assertIsNotNone(result.final_proposal)
        self.assertEqual(calls.count("PM Agent"), 3)
        self.assertEqual(repository.get_for_guild(1004).final_proposal, result.final_proposal)

    async def test_followup_failure_uses_public_channel_fallback(self) -> None:
        response = SimpleNamespace(status=500, reason="temporary failure")
        error = discord.HTTPException(response, "temporary failure")
        channel = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            guild_id=1005,
            followup=SimpleNamespace(send=AsyncMock(side_effect=[error, error])),
            channel=channel,
        )

        delivered = await safe_followup_send(interaction, "會議進度")

        self.assertTrue(delivered)
        self.assertEqual(interaction.followup.send.await_count, 2)
        channel.send.assert_awaited_once_with("會議進度")


if __name__ == "__main__":
    unittest.main()
