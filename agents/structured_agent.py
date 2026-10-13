"""使用 Pydantic 驗證模型 JSON 回覆的共用 Agent。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from agents.base_agent import (
    AgentConfig,
    AgentError,
    AgentLLMServiceProtocol,
    BaseAgent,
)
from agents.errors import RetryableAgentError
from core.errors import ErrorCode
from core.text_quality import incomplete_paths
from services.llm_service import LLMUsage


# 每個專業 Agent 都能指定自己的 Pydantic 輸出模型。
OutputModel = TypeVar("OutputModel", bound=BaseModel)


class AgentSchemaValidationError(RetryableAgentError):
    """模型 JSON 可解析但不符合 Agent 的固定輸出 Schema。"""


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

    async def respond_with_validation_feedback(self, user_input: str, validation_feedback: str):
        """既有有限重試的局部修正提示，不修改跨 Guild 共用設定。"""
        config = replace(self.config, system_prompt=self.config.system_prompt
            + "\n上次驗證失敗：" + validation_feedback[:500]
            + "\n請縮短表達並完整收尾，不增加無根據內容。")
        return await StructuredAgent(config, self.llm_service, self.output_model).respond_with_metadata(user_input)

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
            failure = AgentSchemaValidationError(
                f"{self.config.name} 回覆格式不正確，請重新產生。"
            )
            # 只傳宣告過的欄位名稱與錯誤型別，不複製 input/msg/任意 ctx。
            names = set()
            def declared_fields(schema):
                if isinstance(schema, dict):
                    names.update(schema.get('properties', {}))
                    for child in schema.values():
                        declared_fields(child)
                elif isinstance(schema, list):
                    for child in schema:
                        declared_fields(child)
            declared_fields(self.output_model.model_json_schema())
            details = []
            for item in error.errors(include_input=False, include_url=False)[:5]:
                path = '.'.join(str(part) if isinstance(part, int) or part in names else '?' for part in item['loc'])
                details.append(f"{path or '輸出結構'}：{item['type']}")
            failure.validation_feedback = '請依既定 Schema 修正驗證欄位：' + '；'.join(details)
            raise failure from error
        findings = incomplete_paths(output.model_dump(mode="json"))
        if findings:
            error = AgentSchemaValidationError("模型文字未完整收尾，請用完整短句重新產生。")
            error.validation_feedback = "未收尾欄位：" + "、".join(findings[:5])
            # 僅供本次有限重試使用，不放到共用 Agent 上或保存為成功輸出。
            error.invalid_response = StructuredAgentResponse(output, response.usage, self.config.max_output_tokens)
            error.incomplete_fields = findings
            raise error
        return StructuredAgentResponse(
            output=output,
            usage=response.usage,
            max_output_tokens=self.config.max_output_tokens,
        )

AgentSchemaValidationError.default_code = ErrorCode.LLM_INVALID_OUTPUT
