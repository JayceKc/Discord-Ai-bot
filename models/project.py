"""定義專案與需求變更的資料欄位。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Mapping


class ProjectDataError(ValueError):
    """projects.json 的欄位缺少或格式不正確。"""


class ProjectStatus(str, Enum):
    """專案生命週期中允許使用的狀態。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        """取得適合顯示在 Discord 的繁體中文名稱。"""

        labels = {
            ProjectStatus.PENDING: "待處理",
            ProjectStatus.IN_PROGRESS: "進行中",
            ProjectStatus.COMPLETED: "已完成",
            ProjectStatus.CANCELLED: "已取消",
        }
        return labels[self]

    def can_transition_to(self, target: "ProjectStatus") -> bool:
        """判斷目前狀態是否可以合法轉換成 target。"""

        legal_transitions = {
            ProjectStatus.PENDING: {
                ProjectStatus.IN_PROGRESS,
                ProjectStatus.CANCELLED,
            },
            ProjectStatus.IN_PROGRESS: {
                ProjectStatus.COMPLETED,
                ProjectStatus.CANCELLED,
            },
            ProjectStatus.COMPLETED: set(),
            ProjectStatus.CANCELLED: set(),
        }
        return target in legal_transitions[self]

    @classmethod
    def from_value(cls, value: object) -> "ProjectStatus":
        """將 JSON 字串轉成 ProjectStatus。"""

        if not isinstance(value, str):
            raise ProjectDataError("status 必須是字串。")
        try:
            return cls(value)
        except ValueError as error:
            allowed = "、".join(status.value for status in cls)
            raise ProjectDataError(f"status 必須是以下其中之一：{allowed}。") from error


@dataclass(frozen=True)
class RequirementChange:
    """客戶針對既有專案提出的一筆需求變更。"""

    id: str  # 需求變更編號，例如 CHG-001。
    project_id: str  # 這筆變更屬於哪一個專案。
    description: str  # 想增加、刪除或調整的內容。
    reason: str  # 提出需求變更的原因。
    requested_at: date  # 提出日期。
    status: str  # 目前狀態，例如待評估、已核准或已完成。

    def to_dict(self) -> dict[str, str]:
        """轉成會議紀錄與 JSON Repository 可以保存的格式。"""

        return {
            "id": self.id,
            "project_id": self.project_id,
            "description": self.description,
            "reason": self.reason,
            "requested_at": self.requested_at.isoformat(),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "RequirementChange":
        """將 JSON 字典轉成 RequirementChange，並驗證必要欄位。"""

        return cls(
            id=_required_str(data, "id"),
            project_id=_required_str(data, "project_id"),
            description=_required_str(data, "description"),
            reason=_required_str(data, "reason"),
            requested_at=_required_date(data, "requested_at"),
            status=_required_str(data, "status"),
        )


@dataclass(frozen=True)
class Project:
    """一筆可由 `/projects` 顯示的專案資料。"""

    id: str  # 專案唯一編號，例如 PRJ-001。
    category: str  # 網站、Discord Bot、資料分析等類別。
    title: str  # 專案名稱。
    requirements: tuple[str, ...]  # 客戶的需求清單。
    budget: int  # 預算，範例資料統一使用新臺幣。
    deadline: date  # 專案期限。
    acceptance_criteria: tuple[str, ...]  # 判定專案完成的驗收條件。
    status: ProjectStatus  # 待處理、進行中、已完成或已取消。
    requirement_changes: tuple[RequirementChange, ...] = ()  # 歷次需求變更。

    def can_transition_to(self, target: ProjectStatus) -> bool:
        """交由目前的 ProjectStatus 判斷狀態轉換是否合法。"""

        return self.status.can_transition_to(target)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "Project":
        """將 projects.json 的一筆資料轉成 Project。"""

        project_id = _required_str(data, "id")
        changes_data = data.get("requirement_changes", [])
        if not isinstance(changes_data, list):
            raise ProjectDataError("requirement_changes 必須是陣列。")

        changes: list[RequirementChange] = []
        for change_data in changes_data:
            if not isinstance(change_data, Mapping):
                raise ProjectDataError("每筆 requirement_changes 必須是物件。")
            change = RequirementChange.from_dict(change_data)
            if change.project_id != project_id:
                raise ProjectDataError(
                    f"需求變更 {change.id} 的 project_id 與專案 {project_id} 不一致。"
                )
            changes.append(change)

        return cls(
            id=project_id,
            category=_required_str(data, "category"),
            title=_required_str(data, "title"),
            requirements=_required_string_tuple(data, "requirements"),
            budget=_required_nonnegative_int(data, "budget"),
            deadline=_required_date(data, "deadline"),
            acceptance_criteria=_required_string_tuple(data, "acceptance_criteria"),
            status=ProjectStatus.from_value(data.get("status")),
            requirement_changes=tuple(changes),
        )


def _required_str(data: Mapping[str, object], field: str) -> str:
    """取得不可為空的字串欄位。"""

    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProjectDataError(f"{field} 必須是非空字串。")
    return value.strip()


def _required_string_tuple(
    data: Mapping[str, object],
    field: str,
) -> tuple[str, ...]:
    """取得至少包含一個非空字串的 JSON 陣列。"""

    value = data.get(field)
    if not isinstance(value, list) or not value:
        raise ProjectDataError(f"{field} 必須是非空陣列。")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ProjectDataError(f"{field} 的每個項目都必須是非空字串。")
    return tuple(item.strip() for item in value)


def _required_nonnegative_int(data: Mapping[str, object], field: str) -> int:
    """取得大於或等於零的整數欄位。"""

    value = data.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProjectDataError(f"{field} 必須是大於或等於 0 的整數。")
    return value


def _required_date(data: Mapping[str, object], field: str) -> date:
    """將 YYYY-MM-DD 格式的字串轉成 date。"""

    raw_value = _required_str(data, field)
    try:
        return date.fromisoformat(raw_value)
    except ValueError as error:
        raise ProjectDataError(f"{field} 必須使用 YYYY-MM-DD 格式。") from error
