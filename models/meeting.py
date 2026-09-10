"""定義會議狀態、Agent 步驟與可保存的會議紀錄。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class MeetingDataError(ValueError):
    """會議資料格式或狀態轉換不合法。"""


class MeetingStatus(str, Enum):
    """一場 Agent 會議可能處於的狀態。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def can_transition_to(self, target: "MeetingStatus") -> bool:
        """判斷目前會議能否合法轉換成 target。"""

        legal_transitions = {
            MeetingStatus.PENDING: {
                MeetingStatus.RUNNING,
                MeetingStatus.CANCELLED,
            },
            MeetingStatus.RUNNING: {
                MeetingStatus.COMPLETED,
                MeetingStatus.FAILED,
                MeetingStatus.CANCELLED,
            },
            MeetingStatus.FAILED: {
                MeetingStatus.RUNNING,
                MeetingStatus.CANCELLED,
            },
            MeetingStatus.COMPLETED: set(),
            MeetingStatus.CANCELLED: set(),
        }
        return target in legal_transitions[self]

    @classmethod
    def from_value(cls, value: object) -> "MeetingStatus":
        """將 JSON 值轉為 MeetingStatus，並統一錯誤訊息。"""

        raw_value = _required_string_value(value, "status")
        try:
            return cls(raw_value)
        except ValueError as error:
            raise MeetingDataError("status 是未知的會議狀態。") from error


class MeetingStepStatus(str, Enum):
    """單一 Agent 步驟的執行狀態。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def from_value(cls, value: object) -> "MeetingStepStatus":
        """將 JSON 值轉為 MeetingStepStatus。"""

        raw_value = _required_string_value(value, "step status")
        try:
            return cls(raw_value)
        except ValueError as error:
            raise MeetingDataError("step status 是未知的步驟狀態。") from error


@dataclass
class MeetingStepRecord:
    """保存單一 Agent 的名稱、輸入、輸出及錯誤。"""

    agent_name: str
    order: int
    status: MeetingStepStatus = MeetingStepStatus.PENDING
    input_text: str | None = None
    output_data: dict[str, object] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        """轉成可寫入 JSON 的字典。"""

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
        """從 JSON 字典重建並驗證 Agent 步驟。"""

        return cls(
            agent_name=_required_str(data, "agent_name"),
            order=_required_int(data, "order"),
            status=MeetingStepStatus.from_value(data.get("status")),
            input_text=_optional_str(data.get("input_text")),
            output_data=_optional_dict(data.get("output_data")),
            error=_optional_str(data.get("error")),
        )


@dataclass
class MeetingRecord:
    """保存一場 Guild 會議的狀態與所有 Agent 步驟。"""

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
        cls,
        meeting_id: str,
        guild_id: int,
        project_id: str,
        requirement: str,
    ) -> "MeetingRecord":
        """建立一場包含五位固定 Agent 的新會議。"""

        names = (
            "PM Agent",
            "Research Agent",
            "Creative Agent",
            "Finance Agent",
            "Review Agent",
        )
        return cls(
            meeting_id=_require_nonempty_text(meeting_id, "meeting_id"),
            guild_id=_require_nonnegative_int(guild_id, "guild_id"),
            project_id=_require_nonempty_text(project_id, "project_id").upper(),
            requirement=_require_nonempty_text(requirement, "requirement"),
            steps=[
                MeetingStepRecord(agent_name=name, order=index)
                for index, name in enumerate(names)
            ],
        )

    def transition_to(self, target: MeetingStatus) -> None:
        """執行合法轉換，非法轉換時不修改原狀態。"""

        if not self.status.can_transition_to(target):
            raise MeetingDataError(
                "非法的會議狀態轉換："
                f"{self.status.value} -> {target.value}"
            )
        self.status = target

    def to_dict(self) -> dict[str, object]:
        """轉成 Repository 可以保存的 JSON 字典。"""

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
        """從 Repository 的 JSON 字典重建完整會議。"""

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
            status=MeetingStatus.from_value(data.get("status")),
            current_step_index=_required_int(data, "current_step_index"),
            steps=[MeetingStepRecord.from_dict(item) for item in raw_steps],
            error=_optional_str(data.get("error")),
        )


def _required_str(data: Mapping[str, object], field_name: str) -> str:
    """讀取不可為空的字串欄位。"""

    return _require_nonempty_text(data.get(field_name), field_name)


def _required_int(data: Mapping[str, object], field_name: str) -> int:
    """讀取非負整數欄位。"""

    return _require_nonnegative_int(data.get(field_name), field_name)


def _require_nonempty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MeetingDataError(f"{field_name} 必須是非空字串。")
    return value.strip()


def _required_string_value(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise MeetingDataError(f"{field_name} 必須是字串。")
    return value


def _require_nonnegative_int(value: object, field_name: str) -> int:
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
