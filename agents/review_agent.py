"""檢查專案草案品質並控制修改次數的 Review Agent。"""

from __future__ import annotations
from core.errors import ErrorCode

from dataclasses import replace
import json
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
from agents.structured_agent import (
    AgentSchemaValidationError, StructuredAgent, StructuredAgentResponse,
)


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
    user_choice: dict[str, object] | None = None
    project_priority: str | None = None
    priority_guidance: str | None = None
    recent_experiences: list[dict[str, str]] = Field(default_factory=list)
    requirement: str | None = None
    requirement_change: dict[str, object] | None = None
    allowed_sources: list[str] = Field(default_factory=list)


class ChecklistItem(BaseModel):
    """檢查表中單一項目的 1～5 級評分與判斷原因。"""

    model_config = ConfigDict(extra="forbid")

    score: Annotated[int, Field(strict=True, ge=1, le=5)]
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
    # 預設保留舊資料的必要修改語意；優先目標與歷史參考不能自行變成驗收條件。
    basis: Literal["requirement", "user_choice", "priority", "reference"] = "requirement"


class ReviewAdvisory(ReviewIssue):
    """非必要建議的生成 Schema 不允許偽裝硬性需求問題。"""

    basis: Literal["priority", "reference"] = Field(...)


class ReviewAnalysis(BaseModel):
    """Review Agent 經 Pydantic 驗證後的固定輸出格式。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["通過", "需要修改"]
    checklist: ReviewChecklist
    issues: list[ReviewIssue] = Field(max_length=4)
    revision_allowed: bool
    advisories: list[ReviewAdvisory] = Field(default_factory=list, max_length=4)

    @model_validator(mode="before")
    @classmethod
    def separate_optional_advice(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        issues = data.get("issues", [])
        if not isinstance(issues, list):
            return data
        existing_advice = data.get("advisories", [])
        checks = data.get("checklist", {})
        if not isinstance(existing_advice, list) or not isinstance(checks, dict):
            # 保留不合法值讓 Pydantic 回報 Schema 錯誤，不在正規化時拋 TypeError。
            return data
        advice = [item for item in issues if isinstance(item, dict) and item.get("basis") in {"priority", "reference"}]
        if advice:
            data["issues"] = [item for item in issues if item not in advice]
            data["advisories"] = [*existing_advice, *advice]
            if not data["issues"] and all(isinstance(checks.get(name), dict) and isinstance(checks[name].get("score"), int) and checks[name]["score"] >= 3 for name in ("completeness", "credibility", "feasibility")):
                data["status"] = "通過"
                data["revision_allowed"] = False
        return data

    @model_validator(mode="after")
    def validate_review_consistency(self) -> "ReviewAnalysis":
        """確保審查狀態、檢查表與問題清單彼此一致。"""

        if any(item.basis not in {"priority", "reference"} for item in self.advisories):
            raise ValueError("硬性需求或使用者選擇的問題不能放入非必要改善建議。")

        checks = (
            self.checklist.completeness,
            self.checklist.creativity,
            self.checklist.credibility,
            self.checklist.feasibility,
        )
        all_passed = all(item.score >= 3 for item in checks)

        if self.status == "通過":
            hard_checks_passed = all(item.score >= 3 for item in (
                self.checklist.completeness, self.checklist.credibility, self.checklist.feasibility,
            ))
            if not hard_checks_passed:
                raise ValueError("通過時完整度、可信度與可行性必須通過。")
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
                "1 到 5 的整數 score 與具體原因；1 代表嚴重不足、3 代表達到基本標準、"
                "5 代表表現優秀。status 只能輸出「通過」或「需要修改」；"
                "必要需求未達標才退修；優先目標偏好或歷史參考不足不能自行變成硬性驗收。"
                # JSON Schema 無法表達跨欄位的一致性；必須把 validator 規則也告訴模型。
                "issues 只列必要修改；每項 basis 用 requirement 或 user_choice 指明依據。"
                "通過時 issues 必須是空陣列。"
                "非必要改善放 advisories，basis 用 priority 或 reference。創意偏好未滿足時可以保留低創意分數與原因，建議放 advisories；必要三項達標且無硬性問題即可通過。"
                "需要修改時，至少一項 score 必須為 1 或 2，且 issues 至少一項。"
                "如果只是非必要改善，應通過且 issues 為空；不能把選擇保守可行方案本身視為缺陷。"
                "若硬性限制或必要需求未達標，應在對應面向給 1 或 2 分並列出問題。"
                "逐條對照 requirement 與 requirement_change 中的需求及驗收條件，不能只看草案自述。"
                "檢查五章節是否完整收尾、是否存在句中截斷，並檢查 summary、sections、decisions 是否互相矛盾。"
                "有 user_choice 時，必須檢查整份方案與取捨紀錄是否仍殘留已拒絕的舊方向；"
                "例如 CLI 核心不能仍寫 Web 超支才改 CLI，靜態報告不能承諾即時互動。"
                "來源只可引用 allowed_sources 中已完成的發言輪次。必要章節截斷應在完整度給 1 或 2 分。"
                "若需要修改，必須列出問題、修改要求及高、中、低優先順序。"
                "每個問題必須用 assigned_agent 指定 PM Agent、Research Agent、"
                "Creative Agent 或 Finance Agent 其中一位負責修改。"
                "問題清單最多 4 項；需要修改時至少列出 1 項。"
                "輸出必須同時符合 status、四項 score 與 issues 的一致性規則，僅輸出要求的 JSON。"
                "專案最多只能修改一次。若有 user_choice，必須檢查方案是否具體落實選項及遵守硬性限制；不符合時列入問題。"
                "revision_count=1 也不能改變評分規則，不可為了無法再修改而強行通過。"
                "是否仍允許自動修改由程式控制；未解決的問題仍須如實評分與列出。"
                "若輸入包含 project_priority 與 recent_experiences，請在審查理由中"
                "評估方案是否符合優先目標；歷史經驗僅供參考，不可當成本案事實。"
                "字數限制在500字內"
            ),
            # 審查工作使用低溫度，讓判斷與輸出格式較穩定。
            temperature=0.0,
            seed=42,
            max_output_tokens=1200,
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

        return await self._respond_review(user_input)

    async def respond_with_validation_feedback(
        self, user_input: str, validation_feedback: str,
    ) -> StructuredAgentResponse[ReviewAnalysis]:
        """供 Manager 的既有重試使用；這裡不新增重試迴圈。"""

        return await self._respond_review(user_input, validation_feedback)

    async def _respond_review(
        self, user_input: str, validation_feedback: str | None = None,
    ) -> StructuredAgentResponse[ReviewAnalysis]:

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
            raise AgentError(message, code=ErrorCode.INVALID_ARGUMENT) from error

        # 傳入驗證後的標準 JSON，讓模型收到固定而乾淨的欄位格式。
        # 建立這次呼叫專用的設定，不能修改跨 Guild 共用的 Agent。
        responder = super().respond_with_metadata
        if validation_feedback:
            repair_config = replace(self.config, system_prompt=(
                self.config.system_prompt + "\n上次驗證失敗：" + validation_feedback[:500]
                + "\n請重新核對草案並修正跨欄位矛盾。不要為了通過驗證而隱藏問題；"
                "若必要需求未達標，對應面向必須低於 3 分。只輸出完整 JSON。"
            ))
            responder = StructuredAgent(
                repair_config, self.llm_service, ReviewAnalysis,
            ).respond_with_metadata
        try:
            response = await responder(request.model_dump_json())
        except AgentSchemaValidationError as error:
            cause = error.__cause__
            if isinstance(cause, ValidationError):
                # 不附模型原文、input 或 ctx，也不將回覆內容寫進日誌。
                details = cause.errors(include_input=False, include_context=False, include_url=False)
                error.validation_feedback = "；".join(
                    str(detail["msg"]) for detail in details[:3]
                )[:500]
            raise
        result = response.output

        try:
            proposal = json.loads(request.draft)
        except (TypeError, ValueError):
            proposal = None
        if isinstance(proposal, dict):
            from services.proposal_quality import incomplete_sections, stale_cli_decisions
            incomplete = incomplete_sections(proposal)
            if incomplete:
                data = result.model_dump(mode="json")
                data["status"] = "需要修改"
                data["checklist"]["completeness"] = {"score": 2, "reason": "必要章節有未完整收尾的文字；程式完整性檢查未通過。"}
                data["issues"] = [{"problem": "章節疑似截斷：" + "、".join(incomplete),
                    "required_change": "重新寫出完整短句，保留驗收要求；請縮短表達而非在字元上限處切斷。",
                    "priority": "高", "assigned_agent": "PM Agent"}, *data["issues"]][:4]
                result = ReviewAnalysis.model_validate(data)
            if stale_cli_decisions(proposal, request.requirement, request.user_choice):
                data = result.model_dump(mode="json")
                data["status"] = "需要修改"
                data["checklist"]["credibility"] = {"score": 2, "reason": "已選 CLI 且禁止即時 Web，但決策仍保留優先 Web 或超支才改 CLI。"}
                data["issues"] = [{"problem": "舊 Web 取捨與 CLI 硬性限制矛盾。", "required_change": "同步重寫 summary、sections、decisions，落實 CLI 與靜態報告；不保留即時 Web 備案。", "priority": "高", "assigned_agent": "PM Agent", "basis": "user_choice"}, *data["issues"]][:4]
                result = ReviewAnalysis.model_validate(data)

        # 是否能再修改由程式決定，不採信模型自行輸出的布林值。
        revision_allowed = result.status == "需要修改" and request.revision_count == 0
        return StructuredAgentResponse(
            output=result.model_copy(update={"revision_allowed": revision_allowed}),
            usage=response.usage,
            max_output_tokens=response.max_output_tokens,
        )
