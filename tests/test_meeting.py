import unittest
from datetime import date

from models.meeting import (
    AgentSuggestion,
    MeetingContext,
    MeetingDataError,
    MeetingRecord,
    MeetingRoundSummary,
    MeetingStatus,
    MeetingStepRecord,
    MeetingStepStatus,
    ProposalMetrics,
)
from models.project import RequirementChange


class MeetingModelTest(unittest.TestCase):
    """驗證會議狀態規則與 JSON 往返時的資料完整性。"""

    def test_meeting_status_allows_failure_recovery(self) -> None:
        """失敗可以恢復，但完成或取消後不能重新執行。"""

        self.assertTrue(MeetingStatus.PENDING.can_transition_to(MeetingStatus.RUNNING))
        self.assertTrue(MeetingStatus.RUNNING.can_transition_to(MeetingStatus.FAILED))
        self.assertTrue(MeetingStatus.FAILED.can_transition_to(MeetingStatus.RUNNING))
        self.assertFalse(
            MeetingStatus.COMPLETED.can_transition_to(MeetingStatus.RUNNING)
        )
        self.assertFalse(
            MeetingStatus.CANCELLED.can_transition_to(MeetingStatus.RUNNING)
        )

    def test_record_round_trip_preserves_agent_input_and_output(self) -> None:
        """序列化再讀回後，每位 Agent 的輸入與輸出不可遺失。"""

        record = MeetingRecord(
            meeting_id="meeting-1",
            guild_id=123,
            project_id="PRJ-001",
            requirement="建立 Discord Bot",
            status=MeetingStatus.FAILED,
            current_step_index=1,
            steps=[
                MeetingStepRecord(
                    agent_name="PM Agent",
                    order=0,
                    round_number=2,
                    status=MeetingStepStatus.COMPLETED,
                    input_text="建立 Discord Bot",
                    output_data={"goal": "完成 Bot"},
                    input_characters=14,
                    output_characters=18,
                    prompt_tokens=120,
                    completion_tokens=80,
                    max_output_tokens=1000,
                    execution_time_seconds=12.345,
                )
            ],
            meeting_context=MeetingContext(
                agent_summaries={"PM Agent": "確認專案目標"},
                suggestions=[
                    AgentSuggestion(
                        agent_name="PM Agent",
                        category="work_items",
                        content="建立 Discord 指令",
                        round_number=2,
                    )
                ],
                round_summaries=[
                    MeetingRoundSummary(
                        round_number=1,
                        summary="PM 已完成需求拆解。",
                    )
                ],
            ),
            applied_requirement_change=RequirementChange(
                id="CHG-001",
                project_id="PRJ-001",
                description="預算縮減 20%",
                reason="客戶調整預算",
                requested_at=date(2026, 9, 17),
                status="待評估",
            ),
            proposal_metrics=ProposalMetrics(
                input_characters=5800,
                output_characters=3400,
                prompt_tokens=1480,
                completion_tokens=1210,
                max_output_tokens=1600,
                execution_time_seconds=72.35,
            ),
            error="Research Agent 失敗",
        )

        restored = MeetingRecord.from_dict(record.to_dict())

        self.assertEqual(restored, record)

    def test_illegal_transition_raises_error(self) -> None:
        """終止狀態不得轉回 running。"""

        record = MeetingRecord.new("meeting-1", 123, "PRJ-001", "建立 Bot")
        record.transition_to(MeetingStatus.RUNNING)
        record.transition_to(MeetingStatus.COMPLETED)

        with self.assertRaisesRegex(MeetingDataError, "非法的會議狀態轉換"):
            record.transition_to(MeetingStatus.RUNNING)


if __name__ == "__main__":
    unittest.main()
