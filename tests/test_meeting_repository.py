import tempfile
import unittest
from pathlib import Path

from models.meeting import MeetingRecord, MeetingStatus, MeetingStepStatus
from repositories.meeting_repository import (
    JsonMeetingRepository,
    MeetingRepositoryError,
)


class JsonMeetingRepositoryTest(unittest.TestCase):
    """驗證會議寫入檔案後能由另一個 Repository 完整讀回。"""

    def test_save_and_reload_full_meeting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meetings.json"
            repository = JsonMeetingRepository(path)
            record = MeetingRecord.new(
                "meeting-1",
                123,
                "PRJ-001",
                "建立 Discord Bot",
            )
            record.transition_to(MeetingStatus.RUNNING)
            record.steps[0].status = MeetingStepStatus.COMPLETED
            record.steps[0].input_text = "建立 Discord Bot"
            record.steps[0].output_data = {"goal": "完成 Bot"}

            repository.save(record)

            restored_repository = JsonMeetingRepository(path)
            self.assertEqual(restored_repository.get_for_guild(123), record)
            self.assertEqual(restored_repository.get("meeting-1"), record)

    def test_invalid_json_raises_repository_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meetings.json"
            path.write_text("not-json", encoding="utf-8")

            with self.assertRaisesRegex(MeetingRepositoryError, "無法讀取"):
                JsonMeetingRepository(path).get("meeting-1")


if __name__ == "__main__":
    unittest.main()
