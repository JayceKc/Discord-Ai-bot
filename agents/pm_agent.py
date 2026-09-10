"""負責整理專案目標與執行工作的 PM Agent。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from agents.base_agent import AgentConfig, AgentLLMServiceProtocol
from agents.structured_agent import StructuredAgent


# 移除模型不小心產生的前後空白，並拒絕空字串。
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PMAnalysis(BaseModel):
    """PM 對一項專案需求的結構化分析。"""

    model_config = ConfigDict(extra="forbid")

    goal: NonEmptyText
    constraints: list[NonEmptyText]
    work_items: list[NonEmptyText]
    disagreements: list[NonEmptyText]


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
            ),
            temperature=0.2,
            seed=42,
            max_output_tokens=500,
            # 直接從 Pydantic 模型產生 Ollama 的 JSON Schema，避免兩份格式漂移。
            json_schema=PMAnalysis.model_json_schema(),
        )
        super().__init__(config, llm_service, PMAnalysis)
