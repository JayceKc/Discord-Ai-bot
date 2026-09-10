"""負責區分證據、推論與未知事項的 Research Agent。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from agents.base_agent import AgentConfig, AgentLLMServiceProtocol
from agents.structured_agent import StructuredAgent


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ResearchAnalysis(BaseModel):
    """Research Agent 對專案需求的資訊分類結果。"""

    model_config = ConfigDict(extra="forbid")

    known_information: list[NonEmptyText]
    reasonable_inferences: list[NonEmptyText]
    items_to_verify: list[NonEmptyText]


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
            ),
            temperature=0.2,
            seed=42,
            max_output_tokens=500,
            json_schema=ResearchAnalysis.model_json_schema(),
        )
        super().__init__(config, llm_service, ResearchAnalysis)
