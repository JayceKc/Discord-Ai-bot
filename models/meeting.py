"""定義會議狀態、Agent 步驟與可保存的會議紀錄。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from models.project import RequirementChange


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


@dataclass(frozen=True)
class AgentSuggestion:
    """記錄一項建議的提出者、來源欄位與內容。"""

    agent_name: str
    category: str
    content: str
    round_number: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_name": self.agent_name,
            "category": self.category,
            "content": self.content,
            "round_number": self.round_number,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "AgentSuggestion":
        return cls(
            agent_name=_required_str(data, "agent_name"),
            category=_required_str(data, "category"),
            content=_required_str(data, "content"),
            round_number=_optional_positive_int(data.get("round_number"), default=1),
        )


@dataclass(frozen=True)
class MeetingRoundSummary:
    """保存指定討論輪次的精簡摘要。"""

    round_number: int
    summary: str

    def to_dict(self) -> dict[str, object]:
        return {
            "round_number": self.round_number,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "MeetingRoundSummary":
        round_number = _required_int(data, "round_number")
        if round_number < 1:
            raise MeetingDataError("round_number 必須大於 0。")
        return cls(
            round_number=round_number,
            summary=_required_str(data, "summary"),
        )


@dataclass
class MeetingContext:
    """保存可供後續 Agent 使用的摘要、建議來源與每輪摘要。"""

    agent_summaries: dict[str, str] = field(default_factory=dict)
    suggestions: list[AgentSuggestion] = field(default_factory=list)
    round_summaries: list[MeetingRoundSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_summaries": dict(self.agent_summaries),
            "suggestions": [item.to_dict() for item in self.suggestions],
            "round_summaries": [item.to_dict() for item in self.round_summaries],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "MeetingContext":
        raw_summaries = data.get("agent_summaries", {})
        raw_suggestions = data.get("suggestions", [])
        raw_round_summaries = data.get("round_summaries", [])
        if not isinstance(raw_summaries, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_summaries.items()
        ):
            raise MeetingDataError("agent_summaries 必須是文字對應表。")
        if not isinstance(raw_suggestions, list) or any(
            not isinstance(item, Mapping) for item in raw_suggestions
        ):
            raise MeetingDataError("suggestions 必須是物件陣列。")
        if not isinstance(raw_round_summaries, list) or any(
            not isinstance(item, Mapping) for item in raw_round_summaries
        ):
            raise MeetingDataError("round_summaries 必須是物件陣列。")
        return cls(
            agent_summaries=dict(raw_summaries),
            suggestions=[AgentSuggestion.from_dict(item) for item in raw_suggestions],
            round_summaries=[
                MeetingRoundSummary.from_dict(item)
                for item in raw_round_summaries
            ],
        )


@dataclass
class MeetingStepRecord:
    """保存單一 Agent 的名稱、輸入、輸出及錯誤。"""

    agent_name: str
    order: int
    round_number: int = 1
    status: MeetingStepStatus = MeetingStepStatus.PENDING
    input_text: str | None = None
    output_data: dict[str, object] | None = None
    input_characters: int | None = None
    output_characters: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    max_output_tokens: int | None = None
    execution_time_seconds: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        """轉成可寫入 JSON 的字典。"""

        return {
            "agent_name": self.agent_name,
            "order": self.order,
            "round_number": self.round_number,
            "status": self.status.value,
            "input_text": self.input_text,
            "output_data": self.output_data,
            "input_characters": self.input_characters,
            "output_characters": self.output_characters,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "max_output_tokens": self.max_output_tokens,
            "execution_time_seconds": self.execution_time_seconds,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "MeetingStepRecord":
        """從 JSON 字典重建並驗證 Agent 步驟。"""

        return cls(
            agent_name=_required_str(data, "agent_name"),
            order=_required_int(data, "order"),
            round_number=_optional_positive_int(data.get("round_number"), default=1),
            status=MeetingStepStatus.from_value(data.get("status")),
            input_text=_optional_str(data.get("input_text")),
            output_data=_optional_dict(data.get("output_data")),
            input_characters=_optional_nonnegative_int(data.get("input_characters")),
            output_characters=_optional_nonnegative_int(data.get("output_characters")),
            prompt_tokens=_optional_nonnegative_int(data.get("prompt_tokens")),
            completion_tokens=_optional_nonnegative_int(
                data.get("completion_tokens")
            ),
            max_output_tokens=_optional_nonnegative_int(
                data.get("max_output_tokens")
            ),
            execution_time_seconds=_optional_nonnegative_float(
                data.get("execution_time_seconds")
            ),
            error=_optional_str(data.get("error")),
        )


@dataclass(frozen=True)
class ProposalMetrics:
    """保存 PM 整合草案的字元、Token 與執行時間統計。"""

    input_characters: int
    output_characters: int
    prompt_tokens: int | None
    completion_tokens: int | None
    max_output_tokens: int | None
    execution_time_seconds: float

    def to_dict(self) -> dict[str, object]:
        return {
            "input_characters": self.input_characters,
            "output_characters": self.output_characters,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "max_output_tokens": self.max_output_tokens,
            "execution_time_seconds": self.execution_time_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ProposalMetrics":
        return cls(
            input_characters=_require_nonnegative_int(
                data.get("input_characters"),
                "input_characters",
            ),
            output_characters=_require_nonnegative_int(
                data.get("output_characters"),
                "output_characters",
            ),
            prompt_tokens=_optional_nonnegative_int(data.get("prompt_tokens")),
            completion_tokens=_optional_nonnegative_int(
                data.get("completion_tokens")
            ),
            max_output_tokens=_optional_nonnegative_int(
                data.get("max_output_tokens")
            ),
            execution_time_seconds=(
                _optional_nonnegative_float(data.get("execution_time_seconds"))
                or 0.0
            ),
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
    meeting_context: MeetingContext = field(default_factory=MeetingContext)
    applied_requirement_change: RequirementChange | None = None
    proposal_draft: dict[str, object] | None = None
    proposal_metrics: ProposalMetrics | None = None
    review_result: dict[str, object] | None = None
    revision_count: int = 0
    revision_agent_name: str | None = None
    revision_output: dict[str, object] | None = None
    final_proposal: dict[str, object] | None = None
    final_proposal_metrics: ProposalMetrics | None = None
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
            "meeting_context": self.meeting_context.to_dict(),
            "applied_requirement_change": (
                self.applied_requirement_change.to_dict()
                if self.applied_requirement_change is not None
                else None
            ),
            "proposal_draft": self.proposal_draft,
            "proposal_metrics": (
                self.proposal_metrics.to_dict()
                if self.proposal_metrics is not None
                else None
            ),
            "review_result": self.review_result,
            "revision_count": self.revision_count,
            "revision_agent_name": self.revision_agent_name,
            "revision_output": self.revision_output,
            "final_proposal": self.final_proposal,
            "final_proposal_metrics": (
                self.final_proposal_metrics.to_dict()
                if self.final_proposal_metrics is not None
                else None
            ),
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
        raw_context = data.get("meeting_context", {})
        if not isinstance(raw_context, Mapping):
            raise MeetingDataError("meeting_context 必須是物件。")
        raw_change = data.get("applied_requirement_change")
        if raw_change is not None and not isinstance(raw_change, Mapping):
            raise MeetingDataError("applied_requirement_change 必須是物件或 null。")
        raw_proposal = data.get("proposal_draft")
        if raw_proposal is not None and not isinstance(raw_proposal, dict):
            raise MeetingDataError("proposal_draft 必須是物件或 null。")
        raw_proposal_metrics = data.get("proposal_metrics")
        if raw_proposal_metrics is not None and not isinstance(
            raw_proposal_metrics,
            Mapping,
        ):
            raise MeetingDataError("proposal_metrics 必須是物件或 null。")
        raw_review_result = data.get("review_result")
        raw_revision_output = data.get("revision_output")
        raw_final_proposal = data.get("final_proposal")
        for field_name, value in (
            ("review_result", raw_review_result),
            ("revision_output", raw_revision_output),
            ("final_proposal", raw_final_proposal),
        ):
            if value is not None and not isinstance(value, dict):
                raise MeetingDataError(f"{field_name} 必須是物件或 null。")
        raw_final_metrics = data.get("final_proposal_metrics")
        if raw_final_metrics is not None and not isinstance(raw_final_metrics, Mapping):
            raise MeetingDataError("final_proposal_metrics 必須是物件或 null。")

        return cls(
            meeting_id=_required_str(data, "meeting_id"),
            guild_id=_required_int(data, "guild_id"),
            project_id=_required_str(data, "project_id"),
            requirement=_required_str(data, "requirement"),
            status=MeetingStatus.from_value(data.get("status")),
            current_step_index=_required_int(data, "current_step_index"),
            steps=[MeetingStepRecord.from_dict(item) for item in raw_steps],
            meeting_context=MeetingContext.from_dict(raw_context),
            applied_requirement_change=(
                RequirementChange.from_dict(raw_change)
                if raw_change is not None
                else None
            ),
            proposal_draft=(dict(raw_proposal) if raw_proposal is not None else None),
            proposal_metrics=(
                ProposalMetrics.from_dict(raw_proposal_metrics)
                if raw_proposal_metrics is not None
                else None
            ),
            review_result=(
                dict(raw_review_result) if raw_review_result is not None else None
            ),
            revision_count=_optional_revision_count(data.get("revision_count")),
            revision_agent_name=_optional_str(data.get("revision_agent_name")),
            revision_output=(
                dict(raw_revision_output) if raw_revision_output is not None else None
            ),
            final_proposal=(
                dict(raw_final_proposal) if raw_final_proposal is not None else None
            ),
            final_proposal_metrics=(
                ProposalMetrics.from_dict(raw_final_metrics)
                if raw_final_metrics is not None
                else None
            ),
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


def _optional_positive_int(value: object, *, default: int) -> int:
    """讀取向下相容的正整數欄位；舊紀錄缺少時使用預設值。"""

    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MeetingDataError("round_number 必須是正整數。")
    return value


def _optional_revision_count(value: object) -> int:
    """舊紀錄沒有修改次數時視為 0，且永遠不得超過一次。"""

    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value not in {0, 1}:
        raise MeetingDataError("revision_count 只能是 0 或 1。")
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


def _optional_nonnegative_float(value: object) -> float | None:
    """讀取可選的非負秒數，並支援 JSON 整數或浮點數。"""

    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < 0
    ):
        raise MeetingDataError("execution_time_seconds 必須是非負數字或 null。")
    return float(value)


def _optional_nonnegative_int(value: object) -> int | None:
    """讀取可選的非負整數統計；舊紀錄缺少欄位時回傳 None。"""

    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MeetingDataError("Agent 使用量統計必須是非負整數或 null。")
    return value
