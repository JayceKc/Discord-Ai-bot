import unittest

from models.meeting import (
    MeetingDataError,
    MeetingRecord,
    MeetingStatus,
    MeetingStepRecord,
    MeetingStepStatus,
)


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
                    status=MeetingStepStatus.COMPLETED,
                    input_text="建立 Discord Bot",
                    output_data={"goal": "完成 Bot"},
                )
            ],
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
