"""評估專案成本與執行風險的 Finance Agent。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agents.base_agent import AgentConfig, AgentLLMServiceProtocol
from agents.structured_agent import StructuredAgent


NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
]


class FinanceAnalysis(BaseModel):
    """Finance Agent 對成本、限制、風險與替代方案的檢查結果。"""

    model_config = ConfigDict(extra="forbid")

    cost_considerations: list[NonEmptyText] = Field(max_length=2)
    constraints: list[NonEmptyText] = Field(max_length=2)
    risks: list[NonEmptyText] = Field(max_length=2)
    alternatives: list[NonEmptyText] = Field(min_length=1, max_length=2)


class FinanceAgent(StructuredAgent[FinanceAnalysis]):
    """從成本與風險角度評估同一份專案研究資料。"""

    def __init__(self, llm_service: AgentLLMServiceProtocol) -> None:
        config = AgentConfig(
            name="Finance Agent",
            role="財務與風險分析員",
            system_prompt=(
                "輸入會包含專案需求與 Research Agent 的研究結果。"
                "請保守檢查成本考量、已知限制與執行風險，並提出至少一個"
                "成本較低或風險較小的替代方案。缺少數字時不可自行捏造金額。"
                "至少針對 Creative Agent 的一項方案明確說明採用、延續或反對，"
                "並把 Agent 名稱寫入對應的風險或替代方案。"
                "輸入包含 requirement_change 時，必須重新評估新限制造成的成本或"
                "風險，不得只表示同意。"
                "每個分類最多列出 2 項，每項不超過 80 字。"
            ),
            # Finance 使用低溫度，讓成本與風險判斷更穩定。
            temperature=0.1,
            seed=42,
            max_output_tokens=1000,
            json_schema=FinanceAnalysis.model_json_schema(),
        )
        super().__init__(config, llm_service, FinanceAnalysis)
