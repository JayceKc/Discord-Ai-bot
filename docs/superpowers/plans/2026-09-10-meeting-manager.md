# Meeting Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立能依固定順序呼叫五位 Agent、持久保存每一步資料，並處理 Guild 併發、取消、失敗與恢復的純 Python Meeting Manager。

**Architecture:** `models/meeting.py` 定義狀態機與可序列化會議資料；`repositories/meeting_repository.py` 以 JSON 持久化完整紀錄；`services/meeting_manager.py` 負責組合各 Agent 的輸入、依序呼叫並在每一步保存。每個 Guild 使用獨立 `asyncio.Lock`，因此同 Guild 不可重複啟動，不同 Guild 可以並行。

**Tech Stack:** Python 3.10、`asyncio`、標準函式庫 JSON、Pydantic 2.12.5、`unittest.IsolatedAsyncioTestCase`

**Spec:** `docs/superpowers/specs/2026-09-10-meeting-manager-design.md`

## Global Constraints

- Agent 順序固定為 PM、Research、Creative、Finance、Review。
- 每位 Agent 開始前保存輸入，成功後立即保存輸出。
- `failed` 會議只能從第一個未完成或失敗步驟恢復。
- `completed` 與 `cancelled` 是不可恢復的終止狀態。
- DAY 15 不修改 Discord 指令，也不自動執行 Review 修改循環。
- 測試使用 Fake Agent 與暫存檔，不連線 Ollama。

---

### Task 1: 會議資料模型與合法狀態轉換

**Files:**
- Create: `models/meeting.py`
- Modify: `models/__init__.py`
- Test: `tests/test_meeting.py`

**Interfaces:**
- Consumes: Python `Enum`、`dataclass` 與目前專案的 JSON 資料模型風格。
- Produces: `MeetingStatus`、`MeetingStepStatus`、`MeetingStepRecord`、`MeetingRecord`、`MeetingDataError`，以及 `to_dict()`／`from_dict()`。

- [ ] **Step 1: 撰寫會失敗的狀態與序列化測試**

```python
import unittest

from models.meeting import (
    MeetingDataError,
    MeetingRecord,
    MeetingStatus,
    MeetingStepRecord,
    MeetingStepStatus,
)


class MeetingModelTest(unittest.TestCase):
    def test_meeting_status_allows_failure_recovery(self) -> None:
        self.assertTrue(MeetingStatus.PENDING.can_transition_to(MeetingStatus.RUNNING))
        self.assertTrue(MeetingStatus.RUNNING.can_transition_to(MeetingStatus.FAILED))
        self.assertTrue(MeetingStatus.FAILED.can_transition_to(MeetingStatus.RUNNING))
        self.assertFalse(MeetingStatus.COMPLETED.can_transition_to(MeetingStatus.RUNNING))
        self.assertFalse(MeetingStatus.CANCELLED.can_transition_to(MeetingStatus.RUNNING))

    def test_record_round_trip_preserves_agent_input_and_output(self) -> None:
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
        record = MeetingRecord.new("meeting-1", 123, "PRJ-001", "建立 Bot")
        record.transition_to(MeetingStatus.RUNNING)
        record.transition_to(MeetingStatus.COMPLETED)

        with self.assertRaisesRegex(MeetingDataError, "非法的會議狀態轉換"):
            record.transition_to(MeetingStatus.RUNNING)
```

- [ ] **Step 2: 執行測試並確認因模型不存在而失敗**

Run: `.venv/bin/python -m unittest tests.test_meeting -v`

Expected: `ModuleNotFoundError: No module named 'models.meeting'`

- [ ] **Step 3: 實作最小會議模型**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class MeetingDataError(ValueError):
    """會議資料或狀態轉換不合法。"""


class MeetingStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def can_transition_to(self, target: "MeetingStatus") -> bool:
        legal = {
            MeetingStatus.PENDING: {MeetingStatus.RUNNING, MeetingStatus.CANCELLED},
            MeetingStatus.RUNNING: {
                MeetingStatus.COMPLETED,
                MeetingStatus.FAILED,
                MeetingStatus.CANCELLED,
            },
            MeetingStatus.FAILED: {MeetingStatus.RUNNING, MeetingStatus.CANCELLED},
            MeetingStatus.COMPLETED: set(),
            MeetingStatus.CANCELLED: set(),
        }
        return target in legal[self]


class MeetingStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class MeetingStepRecord:
    agent_name: str
    order: int
    status: MeetingStepStatus = MeetingStepStatus.PENDING
    input_text: str | None = None
    output_data: dict[str, object] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_name": self.agent_name,
            "order": self.order,
            "status": self.status.value,
            "input_text": self.input_text,
            "output_data": self.output_data,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "MeetingStepRecord":
        return cls(
            agent_name=_required_str(data, "agent_name"),
            order=_required_int(data, "order"),
            status=MeetingStepStatus(_required_str(data, "status")),
            input_text=_optional_str(data.get("input_text")),
            output_data=_optional_dict(data.get("output_data")),
            error=_optional_str(data.get("error")),
        )


@dataclass
class MeetingRecord:
    meeting_id: str
    guild_id: int
    project_id: str
    requirement: str
    status: MeetingStatus = MeetingStatus.PENDING
    current_step_index: int = 0
    steps: list[MeetingStepRecord] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def new(
        cls, meeting_id: str, guild_id: int, project_id: str, requirement: str
    ) -> "MeetingRecord":
        names = (
            "PM Agent",
            "Research Agent",
            "Creative Agent",
            "Finance Agent",
            "Review Agent",
        )
        return cls(
            meeting_id=meeting_id,
            guild_id=guild_id,
            project_id=project_id.strip().upper(),
            requirement=requirement.strip(),
            steps=[MeetingStepRecord(name, index) for index, name in enumerate(names)],
        )

    def transition_to(self, target: MeetingStatus) -> None:
        if not self.status.can_transition_to(target):
            raise MeetingDataError(
                f"非法的會議狀態轉換：{self.status.value} -> {target.value}"
            )
        self.status = target

    def to_dict(self) -> dict[str, object]:
        return {
            "meeting_id": self.meeting_id,
            "guild_id": self.guild_id,
            "project_id": self.project_id,
            "requirement": self.requirement,
            "status": self.status.value,
            "current_step_index": self.current_step_index,
            "steps": [step.to_dict() for step in self.steps],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "MeetingRecord":
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            raise MeetingDataError("steps 必須是陣列。")
        if any(not isinstance(item, Mapping) for item in raw_steps):
            raise MeetingDataError("steps 的每個項目都必須是物件。")
        return cls(
            meeting_id=_required_str(data, "meeting_id"),
            guild_id=_required_int(data, "guild_id"),
            project_id=_required_str(data, "project_id"),
            requirement=_required_str(data, "requirement"),
            status=MeetingStatus(_required_str(data, "status")),
            current_step_index=_required_int(data, "current_step_index"),
            steps=[MeetingStepRecord.from_dict(item) for item in raw_steps],
            error=_optional_str(data.get("error")),
        )


def _required_str(data: Mapping[str, object], field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise MeetingDataError(f"{field_name} 必須是非空字串。")
    return value.strip()


def _required_int(data: Mapping[str, object], field_name: str) -> int:
    value = data.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MeetingDataError(f"{field_name} 必須是非負整數。")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MeetingDataError("選填文字欄位必須是字串或 null。")
    return value


def _optional_dict(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MeetingDataError("output_data 必須是物件或 null。")
    return value
```

`models/__init__.py` 匯出上述五個公開類別。

- [ ] **Step 4: 執行模型測試**

Run: `.venv/bin/python -m unittest tests.test_meeting -v`

Expected: 3 tests pass。

- [ ] **Step 5: 提交模型與測試**

```bash
git add models/meeting.py models/__init__.py tests/test_meeting.py
git commit -m "feat: add meeting state models"
```

---

### Task 2: JSON Meeting Repository

**Files:**
- Create: `repositories/meeting_repository.py`
- Modify: `repositories/__init__.py`
- Test: `tests/test_meeting_repository.py`

**Interfaces:**
- Consumes: `MeetingRecord.to_dict()` 與 `MeetingRecord.from_dict()`。
- Produces: `MeetingRepository`, `JsonMeetingRepository`, `MeetingRepositoryError`；方法為 `save(record) -> None`、`get(meeting_id) -> MeetingRecord | None`、`get_for_guild(guild_id) -> MeetingRecord | None`。

- [ ] **Step 1: 撰寫會失敗的 Repository 測試**

```python
import tempfile
import unittest
from pathlib import Path

from models.meeting import MeetingRecord, MeetingStatus, MeetingStepStatus
from repositories.meeting_repository import JsonMeetingRepository


class JsonMeetingRepositoryTest(unittest.TestCase):
    def test_save_and_reload_full_meeting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meetings.json"
            repository = JsonMeetingRepository(path)
            record = MeetingRecord.new(
                "meeting-1", 123, "PRJ-001", "建立 Discord Bot"
            )
            record.transition_to(MeetingStatus.RUNNING)
            record.steps[0].status = MeetingStepStatus.COMPLETED
            record.steps[0].input_text = "建立 Discord Bot"
            record.steps[0].output_data = {"goal": "完成 Bot"}
            repository.save(record)

            restored = JsonMeetingRepository(path).get_for_guild(123)

            self.assertEqual(restored, record)
            self.assertEqual(
                JsonMeetingRepository(path).get("meeting-1"),
                record,
            )
```

- [ ] **Step 2: 執行測試並確認 Repository 不存在**

Run: `.venv/bin/python -m unittest tests.test_meeting_repository -v`

Expected: `ModuleNotFoundError: No module named 'repositories.meeting_repository'`

- [ ] **Step 3: 實作 Protocol 與原子 JSON 寫入**

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from models.meeting import MeetingDataError, MeetingRecord


class MeetingRepositoryError(RuntimeError):
    """讀寫會議紀錄失敗。"""


class MeetingRepository(Protocol):
    def save(self, record: MeetingRecord) -> None:
        """建立或覆寫一筆完整會議紀錄。"""

    def get(self, meeting_id: str) -> MeetingRecord | None:
        """依會議 ID 取得紀錄。"""

    def get_for_guild(self, guild_id: int) -> MeetingRecord | None:
        """取得 Guild 目前或最近一場會議。"""


class JsonMeetingRepository:
    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)

    def save(self, record: MeetingRecord) -> None:
        document = self._read_document()
        document["meetings"][record.meeting_id] = record.to_dict()
        document["guilds"][str(record.guild_id)] = record.meeting_id
        self._write_document(document)

    def get(self, meeting_id: str) -> MeetingRecord | None:
        raw = self._read_document()["meetings"].get(meeting_id)
        return self._convert_record(raw)

    def get_for_guild(self, guild_id: int) -> MeetingRecord | None:
        document = self._read_document()
        meeting_id = document["guilds"].get(str(guild_id))
        if meeting_id is None:
            return None
        if not isinstance(meeting_id, str):
            raise MeetingRepositoryError("Guild 的 meeting_id 必須是字串。")
        return self._convert_record(document["meetings"].get(meeting_id))

    def _read_document(self) -> dict[str, dict[str, object]]:
        if not self.file_path.exists():
            return {"guilds": {}, "meetings": {}}
        try:
            document = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MeetingRepositoryError("無法讀取 meetings.json。") from error
        if not isinstance(document, dict):
            raise MeetingRepositoryError("meetings.json 必須是 JSON 物件。")
        guilds = document.get("guilds")
        meetings = document.get("meetings")
        if not isinstance(guilds, dict) or not isinstance(meetings, dict):
            raise MeetingRepositoryError("meetings.json 缺少 guilds 或 meetings 物件。")
        return {"guilds": guilds, "meetings": meetings}

    def _write_document(self, document: dict[str, dict[str, object]]) -> None:
        temporary_path = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self.file_path)
        except OSError as error:
            raise MeetingRepositoryError("無法保存 meetings.json。") from error

    @staticmethod
    def _convert_record(raw: object) -> MeetingRecord | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise MeetingRepositoryError("會議紀錄必須是 JSON 物件。")
        try:
            return MeetingRecord.from_dict(raw)
        except (MeetingDataError, ValueError) as error:
            raise MeetingRepositoryError("會議紀錄格式不正確。") from error
```

在 `repositories/__init__.py` 匯出 `MeetingRepository`、`JsonMeetingRepository` 與 `MeetingRepositoryError`。

- [ ] **Step 4: 執行 Repository 與模型測試**

Run: `.venv/bin/python -m unittest tests.test_meeting tests.test_meeting_repository -v`

Expected: 4 tests pass。

- [ ] **Step 5: 提交 Repository 與測試**

```bash
git add repositories/meeting_repository.py repositories/__init__.py tests/test_meeting_repository.py
git commit -m "feat: persist meeting records"
```

---

### Task 3: 固定 Agent 順序與逐步保存

**Files:**
- Create: `services/meeting_manager.py`
- Modify: `services/__init__.py`
- Test: `tests/test_meeting_manager.py`

**Interfaces:**
- Consumes: 五個具有 `async respond(user_input: str) -> BaseModel` 的 Agent，以及 `MeetingRepository`。
- Produces: `MeetingManager.start(guild_id: int, project_id: str, requirement: str) -> MeetingRecord`、`MeetingManagerError`。

- [ ] **Step 1: 撰寫固定順序與逐步保存的失敗測試**

```python
import json
import unittest
from pydantic import BaseModel

from services.meeting_manager import MeetingManager


class FakeResult(BaseModel):
    agent: str


class FakeAgent:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    async def respond(self, user_input: str) -> FakeResult:
        self.calls.append(self.name)
        return FakeResult(agent=self.name)


class MemoryMeetingRepository:
    def __init__(self) -> None:
        self.records = {}
        self.guilds = {}
        self.snapshots = []

    def save(self, record):
        import copy
        self.records[record.meeting_id] = copy.deepcopy(record)
        self.guilds[record.guild_id] = record.meeting_id
        self.snapshots.append(copy.deepcopy(record))

    def get(self, meeting_id):
        return self.records.get(meeting_id)

    def get_for_guild(self, guild_id):
        meeting_id = self.guilds.get(guild_id)
        return self.records.get(meeting_id) if meeting_id else None


class MeetingManagerTest(unittest.IsolatedAsyncioTestCase):
    async def test_calls_agents_in_fixed_order_and_saves_io(self) -> None:
        calls = []
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
            ["PM Agent", "Research Agent", "Creative Agent", "Finance Agent", "Review Agent"],
        )
        self.assertEqual(result.status.value, "completed")
        self.assertTrue(all(step.input_text for step in result.steps))
        self.assertTrue(all(step.output_data for step in result.steps))
        for index in range(5):
            self.assertTrue(
                any(
                    snapshot.steps[index].status.value == "running"
                    and snapshot.steps[index].input_text is not None
                    and snapshot.steps[index].output_data is None
                    for snapshot in repository.snapshots
                )
            )
        review_input = json.loads(result.steps[4].input_text)
        self.assertEqual(review_input["revision_count"], 0)
        self.assertIn("draft", review_input)
```

- [ ] **Step 2: 執行測試並確認 Manager 不存在**

Run: `.venv/bin/python -m unittest tests.test_meeting_manager.MeetingManagerTest.test_calls_agents_in_fixed_order_and_saves_io -v`

Expected: `ModuleNotFoundError: No module named 'services.meeting_manager'`

- [ ] **Step 3: 實作固定步驟、輸入組合與逐步保存**

```python
from __future__ import annotations

import asyncio
import json
from typing import Callable, Protocol
from uuid import uuid4

from pydantic import BaseModel

from models.meeting import MeetingRecord, MeetingStatus, MeetingStepStatus
from repositories.meeting_repository import MeetingRepository, MeetingRepositoryError


class MeetingManagerError(RuntimeError):
    """會議無法啟動、執行、取消或恢復。"""


class MeetingAgent(Protocol):
    async def respond(self, user_input: str) -> BaseModel:
        """依標準輸入產生可序列化的結構化回覆。"""


class MeetingManager:
    def __init__(
        self,
        repository: MeetingRepository,
        pm_agent: MeetingAgent,
        research_agent: MeetingAgent,
        creative_agent: MeetingAgent,
        finance_agent: MeetingAgent,
        review_agent: MeetingAgent,
        *,
        meeting_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.repository = repository
        self._agents = (
            pm_agent,
            research_agent,
            creative_agent,
            finance_agent,
            review_agent,
        )
        self._meeting_id_factory = meeting_id_factory or (lambda: str(uuid4()))
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._running_tasks: dict[int, asyncio.Task[object]] = {}

    async def start(
        self, guild_id: int, project_id: str, requirement: str
    ) -> MeetingRecord:
        project_id = project_id.strip().upper()
        requirement = requirement.strip()
        if not project_id or not requirement:
            raise MeetingManagerError("專案 ID 與需求不可為空。")

        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        if lock.locked():
            raise MeetingManagerError("這個伺服器已有會議正在執行。")

        async with lock:
            current = self.repository.get_for_guild(guild_id)
            if current is not None and current.status in {
                MeetingStatus.PENDING,
                MeetingStatus.RUNNING,
                MeetingStatus.FAILED,
            }:
                raise MeetingManagerError("這個伺服器已有尚未結束的會議。")
            record = MeetingRecord.new(
                self._meeting_id_factory(), guild_id, project_id, requirement
            )
            self.repository.save(record)
            return await self._run(record)

    async def _run(self, record: MeetingRecord) -> MeetingRecord:
        record.transition_to(MeetingStatus.RUNNING)
        record.error = None
        self.repository.save(record)
        current_task = asyncio.current_task()
        if current_task is not None:
            self._running_tasks[record.guild_id] = current_task

        try:
            for index, agent in enumerate(self._agents):
                step = record.steps[index]
                if step.status == MeetingStepStatus.COMPLETED:
                    continue
                record.current_step_index = index
                step.input_text = self._build_input(record, index)
                step.status = MeetingStepStatus.RUNNING
                step.error = None
                self.repository.save(record)

                response = await agent.respond(step.input_text)
                step.output_data = response.model_dump(mode="json")
                step.status = MeetingStepStatus.COMPLETED
                self.repository.save(record)

            record.transition_to(MeetingStatus.COMPLETED)
            record.current_step_index = len(record.steps)
            self.repository.save(record)
            return record
        finally:
            self._running_tasks.pop(record.guild_id, None)

    def _build_input(self, record: MeetingRecord, index: int) -> str:
        if index == 0:
            return record.requirement
        outputs = {
            step.agent_name: step.output_data
            for step in record.steps[:index]
            if step.output_data is not None
        }
        context = {
            "project_id": record.project_id,
            "requirement": record.requirement,
            "previous_outputs": outputs,
        }
        if index == 4:
            return json.dumps(
                {
                    "project_id": record.project_id,
                    "draft": json.dumps(context, ensure_ascii=False),
                    "revision_count": 0,
                },
                ensure_ascii=False,
            )
        return json.dumps(context, ensure_ascii=False)
```

在 `services/__init__.py` 匯出 `MeetingManager` 與 `MeetingManagerError`。錯誤與取消處理在 Task 4 補齊；此 Task 只建立能通過固定順序測試的最小成功路徑。

- [ ] **Step 4: 執行固定順序測試**

Run: `.venv/bin/python -m unittest tests.test_meeting_manager.MeetingManagerTest.test_calls_agents_in_fixed_order_and_saves_io -v`

Expected: 1 test passes。

- [ ] **Step 5: 提交成功路徑**

```bash
git add services/meeting_manager.py services/__init__.py tests/test_meeting_manager.py
git commit -m "feat: orchestrate meeting agents in order"
```

---

### Task 4: Guild Lock、取消、失敗與恢復

**Files:**
- Modify: `services/meeting_manager.py`
- Modify: `tests/test_meeting_manager.py`

**Interfaces:**
- Consumes: Task 3 的 `MeetingManager`、`_run()` 與每 Guild Lock。
- Produces: `cancel(guild_id: int) -> MeetingRecord`、`resume(guild_id: int) -> MeetingRecord`，以及完整錯誤狀態保存。

- [ ] **Step 1: 加入重複啟動、失敗恢復與取消測試**

```python
import asyncio


class BlockingAgent(FakeAgent):
    def __init__(self, name, calls):
        super().__init__(name, calls)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def respond(self, user_input: str) -> FakeResult:
        self.calls.append(self.name)
        self.started.set()
        await self.release.wait()
        return FakeResult(agent=self.name)


class FailOnceAgent(FakeAgent):
    def __init__(self, name, calls):
        super().__init__(name, calls)
        self.failed = False

    async def respond(self, user_input: str) -> FakeResult:
        self.calls.append(self.name)
        if not self.failed:
            self.failed = True
            raise RuntimeError("暫時失敗")
        return FakeResult(agent=self.name)


def build_manager(repository, calls, **replacements):
    agents = {
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


class ConcurrentAgent(FakeAgent):
    def __init__(self, name, calls):
        super().__init__(name, calls)
        self.active = 0
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()

    async def respond(self, user_input: str) -> FakeResult:
        self.calls.append(self.name)
        self.active += 1
        if self.active == 2:
            self.both_started.set()
        await self.release.wait()
        return FakeResult(agent=self.name)


class MeetingManagerControlTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_guild_cannot_start_twice(self):
        calls = []
        repository = MemoryMeetingRepository()
        blocker = BlockingAgent("PM Agent", calls)
        manager = build_manager(repository, calls, pm_agent=blocker)
        first = asyncio.create_task(manager.start(123, "PRJ-001", "建立 Bot"))
        await blocker.started.wait()

        with self.assertRaisesRegex(MeetingManagerError, "正在執行"):
            await manager.start(123, "PRJ-002", "建立網站")

        blocker.release.set()
        await first

    async def test_different_guilds_can_run_at_the_same_time(self):
        calls = []
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

    async def test_failure_is_saved_and_resume_starts_from_failed_agent(self):
        calls = []
        repository = MemoryMeetingRepository()
        finance = FailOnceAgent("Finance Agent", calls)
        manager = build_manager(repository, calls, finance_agent=finance)

        with self.assertRaisesRegex(MeetingManagerError, "Finance Agent"):
            await manager.start(123, "PRJ-001", "建立 Bot")

        failed = repository.get_for_guild(123)
        self.assertEqual(failed.status, MeetingStatus.FAILED)
        self.assertEqual(failed.current_step_index, 3)

        result = await manager.resume(123)

        self.assertEqual(result.status, MeetingStatus.COMPLETED)
        self.assertEqual(
            calls,
            ["PM Agent", "Research Agent", "Creative Agent", "Finance Agent", "Finance Agent", "Review Agent"],
        )

    async def test_cancel_stops_current_meeting(self):
        calls = []
        repository = MemoryMeetingRepository()
        blocker = BlockingAgent("PM Agent", calls)
        manager = build_manager(repository, calls, pm_agent=blocker)
        running = asyncio.create_task(manager.start(123, "PRJ-001", "建立 Bot"))
        await blocker.started.wait()

        cancelled = manager.cancel(123)

        self.assertEqual(cancelled.status, MeetingStatus.CANCELLED)
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(calls, ["PM Agent"])
```

此測試檔必須從 `models.meeting` 匯入 `MeetingStatus`，並從 `services.meeting_manager` 匯入 `MeetingManagerError`。

- [ ] **Step 2: 執行新增測試並確認因缺少錯誤處理、`cancel()` 與 `resume()` 而失敗**

Run: `.venv/bin/python -m unittest tests.test_meeting_manager -v`

Expected: 至少三個測試失敗，錯誤分別指出第二次啟動未阻擋或 `cancel`／`resume` 不存在。

- [ ] **Step 3: 將 `_run()` 擴充為完整的成功、失敗與取消流程**

```python
    async def _run(
        self,
        record: MeetingRecord,
        *,
        already_running: bool = False,
    ) -> MeetingRecord:
        if not already_running:
            record.transition_to(MeetingStatus.RUNNING)
        record.error = None
        self.repository.save(record)
        current_task = asyncio.current_task()
        if current_task is not None:
            self._running_tasks[record.guild_id] = current_task

        try:
            for index, agent in enumerate(self._agents):
                step = record.steps[index]
                if step.status == MeetingStepStatus.COMPLETED:
                    continue
                record.current_step_index = index
                step.input_text = self._build_input(record, index)
                step.status = MeetingStepStatus.RUNNING
                step.error = None
                self.repository.save(record)

                response = await agent.respond(step.input_text)
                step.output_data = response.model_dump(mode="json")
                step.status = MeetingStepStatus.COMPLETED
                self.repository.save(record)

            record.transition_to(MeetingStatus.COMPLETED)
            record.current_step_index = len(record.steps)
            self.repository.save(record)
            return record
        except asyncio.CancelledError:
            if record.status != MeetingStatus.CANCELLED:
                record.transition_to(MeetingStatus.CANCELLED)
                record.steps[record.current_step_index].status = (
                    MeetingStepStatus.CANCELLED
                )
                self.repository.save(record)
            raise
        except Exception as error:
            step = record.steps[record.current_step_index]
            step.status = MeetingStepStatus.FAILED
            safe_error = f"{step.agent_name} 執行失敗。"
            step.error = safe_error
            record.error = safe_error
            record.transition_to(MeetingStatus.FAILED)
            try:
                self.repository.save(record)
            except MeetingRepositoryError as save_error:
                raise MeetingManagerError("無法保存會議失敗狀態。") from save_error
            raise MeetingManagerError(record.error) from error
        finally:
            self._running_tasks.pop(record.guild_id, None)
```

- [ ] **Step 4: 實作取消與恢復**

```python
    def cancel(self, guild_id: int) -> MeetingRecord:
        record = self.repository.get_for_guild(guild_id)
        if record is None:
            raise MeetingManagerError("這個伺服器沒有可取消的會議。")
        if record.status not in {
            MeetingStatus.PENDING,
            MeetingStatus.RUNNING,
            MeetingStatus.FAILED,
        }:
            raise MeetingManagerError("這場會議目前無法取消。")

        record.transition_to(MeetingStatus.CANCELLED)
        if record.current_step_index < len(record.steps):
            step = record.steps[record.current_step_index]
            if step.status in {MeetingStepStatus.PENDING, MeetingStepStatus.RUNNING}:
                step.status = MeetingStepStatus.CANCELLED
        self.repository.save(record)
        task = self._running_tasks.get(guild_id)
        if task is not None:
            task.cancel()
        return record

    async def resume(self, guild_id: int) -> MeetingRecord:
        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        if lock.locked():
            raise MeetingManagerError("這個伺服器已有會議正在執行。")
        async with lock:
            record = self.repository.get_for_guild(guild_id)
            if record is None or record.status != MeetingStatus.FAILED:
                raise MeetingManagerError("這個伺服器沒有可恢復的失敗會議。")
            for step in record.steps:
                if step.status == MeetingStepStatus.FAILED:
                    step.status = MeetingStepStatus.PENDING
                    step.error = None
                    step.output_data = None
                    break
            record.transition_to(MeetingStatus.RUNNING)
            # _run 接收 already_running=True，避免重複執行 RUNNING -> RUNNING。
            return await self._run(record, already_running=True)
```

把 `_run()` 改為 `_run(record, *, already_running: bool = False)`；`already_running=False` 時才呼叫 `record.transition_to(MeetingStatus.RUNNING)`。開始執行前若 Repository 讀寫失敗，一律轉為不洩漏檔案內容的 `MeetingManagerError`。

- [ ] **Step 5: 執行 Meeting Manager 測試**

Run: `.venv/bin/python -m unittest tests.test_meeting_manager -v`

Expected: 固定順序、重複啟動、失敗恢復及取消測試全部通過。

- [ ] **Step 6: 執行完整驗證**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: 所有既有測試與 DAY 15 測試通過，0 failures、0 errors。

Run: `.venv/bin/python -m compileall -q agents models repositories services tests bot.py config.py`

Expected: exit code 0，沒有語法錯誤。

Run: `git diff --check`

Expected: exit code 0，沒有空白格式錯誤。

- [ ] **Step 7: 提交錯誤處理與恢復功能**

```bash
git add services/meeting_manager.py tests/test_meeting_manager.py
git commit -m "feat: handle meeting cancellation and recovery"
```
