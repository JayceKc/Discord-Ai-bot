import copy
import json
import unittest

from pydantic import BaseModel

from models.meeting import MeetingRecord, MeetingStatus
from services.meeting_manager import MeetingManager


class FakeResult(BaseModel):
    """模擬所有專業 Agent 都會回傳的 Pydantic 結構。"""

    agent: str


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


class MeetingManagerTest(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
