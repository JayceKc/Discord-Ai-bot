"""使用 Pydantic 驗證模型 JSON 回覆的共用 Agent。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from agents.base_agent import (
    AgentConfig,
    AgentError,
    AgentLLMServiceProtocol,
    BaseAgent,
)
from services.llm_service import LLMUsage


# 每個專業 Agent 都能指定自己的 Pydantic 輸出模型。
OutputModel = TypeVar("OutputModel", bound=BaseModel)


@dataclass(frozen=True)
class StructuredAgentResponse(Generic[OutputModel]):
    """保留驗證後的輸出，以及這次模型呼叫的 Token 統計。"""

    output: OutputModel
    usage: LLMUsage
    max_output_tokens: int


class StructuredAgent(Generic[OutputModel]):
    """共用 BaseAgent 呼叫流程，並將 JSON 驗證成指定的 Pydantic 模型。"""

    def __init__(
        self,
        config: AgentConfig,
        llm_service: AgentLLMServiceProtocol,
        output_model: type[OutputModel],
    ) -> None:
        self.config = config
        self.llm_service = llm_service
        self.output_model = output_model
        # PM 與 Research 都透過同一個 BaseAgent 使用傳入的 LLM Service。
        self._base_agent = BaseAgent(config, llm_service)

    async def respond(self, user_input: str) -> OutputModel:
        """使用統一 respond() 介面產生並驗證結構化回覆。"""

        response = await self.respond_with_metadata(user_input)
        return response.output

    async def respond_with_metadata(
        self,
        user_input: str,
    ) -> StructuredAgentResponse[OutputModel]:
        """產生結構化回覆，並保留 Token 使用量與輸出上限。"""

        response = await self._base_agent.respond(user_input)
        try:
            # model_validate_json 同時解析 JSON 並檢查必要欄位與資料型別。
            output = self.output_model.model_validate_json(response.content)
        except ValidationError as error:
            raise AgentError(
                f"{self.config.name} 回覆格式不正確，請重新產生。"
            ) from error
        return StructuredAgentResponse(
            output=output,
            usage=response.usage,
            max_output_tokens=self.config.max_output_tokens,
        )
