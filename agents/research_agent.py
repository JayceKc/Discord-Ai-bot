"""負責區分證據、推論與未知事項的 Research Agent。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agents.base_agent import AgentConfig, AgentLLMServiceProtocol
from agents.structured_agent import StructuredAgent


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
# 每個分類最多三項，避免小模型在 500 Token 上限內產生過長而被截斷。
LimitedResearchItems = Annotated[list[NonEmptyText], Field(max_length=3)]


class ResearchAnalysis(BaseModel):
    """Research Agent 對專案需求的資訊分類結果。"""

    model_config = ConfigDict(extra="forbid")

    known_information: LimitedResearchItems
    reasonable_inferences: LimitedResearchItems
    items_to_verify: LimitedResearchItems


class ResearchAgent(StructuredAgent[ResearchAnalysis]):
    """將需求拆成已知資訊、合理推論與待查證事項。"""

    def __init__(self, llm_service: AgentLLMServiceProtocol) -> None:
        config = AgentConfig(
            name="Research Agent",
            role="研究分析員",
            system_prompt=(
                "分析使用者提供的專案需求，嚴格區分三種類型：使用者明確說明的"
                "已知資訊、根據內容得到但尚非事實的合理推論，以及開始執行前"
                "仍需查證的事項。不可把推論寫成已知事實。"
                "輸入包含 PM Agent 前文時，至少引用一項 PM Agent 的工作或限制，"
                "並說明是延續分析或仍需查證。"
                "輸入包含 requirement_change 時，必須說明新限制改變了哪些已知"
                "資訊、推論或待查證事項，不得只表示同意。"
                "每個分類最多列出 3 項，每項只寫一句精簡內容。"
            ),
            temperature=0.2,
            seed=42,
            max_output_tokens=1000,
            json_schema=ResearchAnalysis.model_json_schema(),
        )
        super().__init__(config, llm_service, ResearchAnalysis)
