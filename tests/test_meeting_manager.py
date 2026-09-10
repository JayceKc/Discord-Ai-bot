import asyncio
import copy
import json
import unittest

from pydantic import BaseModel

from models.meeting import MeetingRecord, MeetingStatus
from services.meeting_manager import MeetingManager, MeetingManagerError


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

        cancelled = manager.cancel(123)

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
