"""根據研究資訊提出具體方案的 Creative Agent。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agents.base_agent import AgentConfig, AgentLLMServiceProtocol
from agents.structured_agent import StructuredAgent


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CreativeProposal(BaseModel):
    """一項能追溯研究依據並可實際執行的創意方案。"""

    model_config = ConfigDict(extra="forbid")

    title: NonEmptyText
    description: NonEmptyText
    research_basis: list[NonEmptyText]
    implementation_steps: list[NonEmptyText] = Field(min_length=1)


class CreativeAnalysis(BaseModel):
    """Creative Agent 對專案提出的一組具體方案。"""

    model_config = ConfigDict(extra="forbid")

    proposals: list[CreativeProposal] = Field(min_length=1)


class CreativeAgent(StructuredAgent[CreativeAnalysis]):
    """使用研究結果發想具體、可執行且有依據的創意方案。"""

    def __init__(self, llm_service: AgentLLMServiceProtocol) -> None:
        config = AgentConfig(
            name="Creative Agent",
            role="創意企劃",
            system_prompt=(
                "輸入會包含專案需求與 Research Agent 的研究結果。"
                "請根據已知資訊及合理推論提出具體創意方案，每個方案都要說明"
                "研究依據與執行步驟；待查證資訊不可當成已確定事實。"
                "research_basis 至少引用 Research Agent 的一項內容，"
                "說明方案延續了哪一項研究結果。"
                "輸入包含 requirement_change 時，必須補充、反對或修正既有方案，"
                "不得只表示同意。"
                "每個分類最多列出 3 項，每項只寫一句精簡內容。"
            ),
            # Creative 使用較高溫度與不同 Seed，增加方案的變化性。
            temperature=0.8,
            seed=7,
            max_output_tokens=1000,
            json_schema=CreativeAnalysis.model_json_schema(),
        )
        super().__init__(config, llm_service, CreativeAnalysis)
