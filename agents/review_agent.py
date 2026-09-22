"""檢查專案草案品質並控制修改次數的 Review Agent。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from agents.base_agent import AgentConfig, AgentError, AgentLLMServiceProtocol
from agents.structured_agent import StructuredAgent, StructuredAgentResponse


# 去除文字前後空白，並禁止空字串出現在輸入與審查結果中。
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ReviewReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
]
# 0 代表初稿，1 代表已修改過一次；其他數字都不允許。
RevisionCount = Annotated[int, Field(strict=True, ge=0, le=1)]


class ReviewRequest(BaseModel):
    """Review Agent 接收的專案草案與目前修改次數。"""

    model_config = ConfigDict(extra="forbid")

    project_id: NonEmptyText
    draft: NonEmptyText
    revision_count: RevisionCount = 0


class ChecklistItem(BaseModel):
    """檢查表中單一項目的通過狀態與判斷原因。"""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    reason: ReviewReason


class ReviewChecklist(BaseModel):
    """固定包含 DAY 14 要求的四個審查面向。"""

    model_config = ConfigDict(extra="forbid")

    completeness: ChecklistItem
    creativity: ChecklistItem
    credibility: ChecklistItem
    feasibility: ChecklistItem


class ReviewIssue(BaseModel):
    """記錄草案問題、必要修改內容與處理優先順序。"""

    model_config = ConfigDict(extra="forbid")

    problem: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
    ]
    required_change: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=180),
    ]
    priority: Literal["高", "中", "低"]
    assigned_agent: Literal[
        "PM Agent",
        "Research Agent",
        "Creative Agent",
        "Finance Agent",
    ]


class ReviewAnalysis(BaseModel):
    """Review Agent 經 Pydantic 驗證後的固定輸出格式。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["通過", "需要修改"]
    checklist: ReviewChecklist
    issues: list[ReviewIssue] = Field(max_length=4)
    revision_allowed: bool

    @model_validator(mode="after")
    def validate_review_consistency(self) -> "ReviewAnalysis":
        """確保審查狀態、檢查表與問題清單彼此一致。"""

        checks = (
            self.checklist.completeness,
            self.checklist.creativity,
            self.checklist.credibility,
            self.checklist.feasibility,
        )
        all_passed = all(item.passed for item in checks)

        if self.status == "通過":
            if not all_passed:
                raise ValueError("通過時四項檢查都必須通過。")
            if self.issues:
                raise ValueError("通過時問題清單必須為空。")
        else:
            if all_passed:
                raise ValueError("需要修改時至少必須有一項檢查未通過。")
            if not self.issues:
                raise ValueError("需要修改時必須列出至少一個問題。")

        return self


class ReviewAgent(StructuredAgent[ReviewAnalysis]):
    """以四項檢查表審查草案，並限制最多只修改一次。"""

    def __init__(self, llm_service: AgentLLMServiceProtocol) -> None:
        config = AgentConfig(
            name="Review Agent",
            role="專案品質審查員",
            system_prompt=(
                "輸入是包含 project_id、draft 與 revision_count 的 JSON。"
                "請分別檢查草案的完整度、創意、可信度與可行性，並為每項提供"
                "通過狀態與具體原因。status 只能輸出「通過」或「需要修改」；"
                "若需要修改，必須列出問題、修改要求及高、中、低優先順序。"
                "每個問題必須用 assigned_agent 指定 PM Agent、Research Agent、"
                "Creative Agent 或 Finance Agent 其中一位負責修改。"
                "專案最多只能修改一次。"
                "字數限制在500字內"
            ),
            # 審查工作使用低溫度，讓判斷與輸出格式較穩定。
            temperature=0.0,
            seed=42,
            max_output_tokens=700,
            json_schema=ReviewAnalysis.model_json_schema(),
        )
        super().__init__(config, llm_service, ReviewAnalysis)

    async def respond(self, user_input: str) -> ReviewAnalysis:
        """先驗證草案與修改次數，再呼叫模型並套用修改上限。"""

        response = await self.respond_with_metadata(user_input)
        return response.output

    async def respond_with_metadata(
        self,
        user_input: str,
    ) -> StructuredAgentResponse[ReviewAnalysis]:
        """審查草案並保留 Token 統計，同時由程式控制修改上限。"""

        try:
            request = ReviewRequest.model_validate_json(user_input)
        except ValidationError as error:
            # 修改次數錯誤提供明確訊息；其他欄位則回傳一般輸入格式錯誤。
            revision_error = any(
                detail["loc"] == ("revision_count",) for detail in error.errors()
            )
            message = (
                "修改次數只能是 0 或 1。"
                if revision_error
                else "Review Agent 輸入格式不正確。"
            )
            raise AgentError(message) from error

        # 傳入驗證後的標準 JSON，讓模型收到固定而乾淨的欄位格式。
        response = await super().respond_with_metadata(request.model_dump_json())
        result = response.output

        # 是否能再修改由程式決定，不採信模型自行輸出的布林值。
        revision_allowed = result.status == "需要修改" and request.revision_count == 0
        return StructuredAgentResponse(
            output=result.model_copy(update={"revision_allowed": revision_allowed}),
            usage=response.usage,
            max_output_tokens=response.max_output_tokens,
        )
