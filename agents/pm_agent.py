"""負責整理專案目標與執行工作的 PM Agent。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agents.base_agent import AgentConfig, AgentLLMServiceProtocol
from agents.structured_agent import StructuredAgent, StructuredAgentResponse


# 移除模型不小心產生的前後空白，並拒絕空字串。
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SectionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=240),
]
SummaryText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]


class PMAnalysis(BaseModel):
    """PM 對一項專案需求的結構化分析。"""

    model_config = ConfigDict(extra="forbid")

    goal: NonEmptyText
    constraints: list[NonEmptyText]
    work_items: list[NonEmptyText]
    disagreements: list[NonEmptyText]


class PMProposalSections(BaseModel):
    """整合草案固定使用的五個文章章節。"""

    model_config = ConfigDict(extra="forbid")

    background_and_goal: SectionText
    integrated_solution: SectionText
    execution_plan: SectionText
    risks_and_responses: SectionText
    acceptance_criteria: SectionText


class PMDecision(BaseModel):
    """記錄 PM 對團隊意見的採用、拒絕或折衷決定。"""

    model_config = ConfigDict(extra="forbid")

    topic: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    decision: Literal["採用", "拒絕", "折衷"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    sources: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=45)]] = Field(
        min_length=1,
        max_length=3,
    )


class PMProposalDraft(BaseModel):
    """PM 將兩輪 Agent 意見整合後產生的固定格式提案。"""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    summary: SummaryText
    sections: PMProposalSections
    decisions: list[PMDecision] = Field(min_length=1, max_length=4)


class PMAgent(StructuredAgent[PMAnalysis]):
    """將需求整理為目標、限制、工作項目與分歧點。"""

    def __init__(self, llm_service: AgentLLMServiceProtocol) -> None:
        config = AgentConfig(
            name="PM Agent",
            role="專案經理",
            system_prompt=(
                "分析使用者提出的專案需求。整理一個明確目標、已知限制、"
                "可以執行的工作項目，以及需要團隊討論的分歧點。"
                "沒有分歧時 disagreements 使用空陣列，不可自行捏造資訊。"
                "輸入包含 requirement_change 時，必須針對新限制補充、反對或"
                "修正第一輪方案，不得只表示同意。"
            ),
            temperature=0.2,
            seed=42,
            max_output_tokens=1000,
            # 直接從 Pydantic 模型產生 Ollama 的 JSON Schema，避免兩份格式漂移。
            json_schema=PMAnalysis.model_json_schema(),
        )
        super().__init__(config, llm_service, PMAnalysis)
        proposal_config = AgentConfig(
            name="PM Integration Agent",
            role="負責整合多角色意見的專案經理",
            system_prompt=(
                "根據兩輪摘要與重要原文，整理成一份可直接閱讀的專案提案。"
                "必須處理互相衝突的建議，不可只是逐項重述。"
                "sections 必須完整填寫專案背景與目標、整合方案、執行計畫、"
                "風險與對策、驗收標準五個固定章節。"
                "摘要最多 300 字，每個章節最多 240 字，重要決策最多 4 項。"
                "每項重要取捨都要寫入 decisions，decision 只能是採用、拒絕或折衷，"
                "並在 sources 標示原意見的 Agent 與輪次。不可捏造輸入中沒有的事實。"
                "若輸入包含 review_issues 與 revision_output，必須明確解決審查問題，"
                "並將指定 Agent 的修改整合進最終方案。"
            ),
            # 整合兩輪摘要比單次需求分析耗時，避免沿用 60 秒預設值而過早取消。
            timeout_seconds=180.0,
            temperature=0.2,
            seed=42,
            max_output_tokens=1600,
            json_schema=PMProposalDraft.model_json_schema(),
        )
        self._proposal_agent = StructuredAgent(
            proposal_config,
            llm_service,
            PMProposalDraft,
        )

    async def integrate(self, meeting_input: str) -> PMProposalDraft:
        """將兩輪討論整合成固定章節與決策紀錄。"""

        return await self._proposal_agent.respond(meeting_input)

    async def integrate_with_metadata(
        self,
        meeting_input: str,
    ) -> StructuredAgentResponse[PMProposalDraft]:
        """建立整合草案，並保留 Token 使用量與 1600 Token 上限。"""

        return await self._proposal_agent.respond_with_metadata(meeting_input)
