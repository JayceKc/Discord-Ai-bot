from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Mapping, Protocol

from services.llm_service import LLMResponse, LLMServiceError, LLMUsage


class AgentError(RuntimeError):
    """Agent 無法產生或解析回覆時的統一錯誤。"""


@dataclass(frozen=True)
class AgentConfig:
    """保存 Agent 身份、模型生成選項與結構化輸出設定。"""

    name: str
    role: str
    system_prompt: str
    timeout_seconds: float = 60.0
    temperature: float = 0.2
    seed: int = 42
    max_output_tokens: int = 500
    json_schema: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        # 先驗證必要文字，避免建立出無法辨識身份的 Agent。
        if not self.name.strip() or not self.role.strip() or not self.system_prompt.strip():
            raise ValueError("Agent 名稱、角色與 System Prompt 不可為空。")
        if self.timeout_seconds <= 0:
            raise ValueError("Agent 逾時秒數必須大於 0。")
        if self.max_output_tokens <= 0:
            raise ValueError("Agent 最大輸出 Token 必須大於 0。")


@dataclass(frozen=True)
class AgentResponse:
    """回傳模型文字、選擇性解析資料與使用量統計。"""

    content: str
    data: dict[str, object] | list[object] | None
    usage: LLMUsage


class AgentLLMServiceProtocol(Protocol):
    """正式 LLM 服務與測試 Fake 共用的最小介面。"""

    async def chat(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        seed: int | None = None,
        max_output_tokens: int | None = None,
        json_schema: Mapping[str, object] | None = None,
    ) -> LLMResponse:
        """取得一次完整模型回覆。"""


class BaseAgent:
    """提供所有 Agent 共用的身份提示、逾時與回覆解析流程。"""

    def __init__(self, config: AgentConfig, llm_service: AgentLLMServiceProtocol) -> None:
        self.config = config
        self.llm_service = llm_service

    async def respond(self, user_input: str) -> AgentResponse:
        """送出使用者輸入，並依設定回傳文字或 JSON 資料。"""

        user_input = user_input.strip()
        if not user_input:
            raise AgentError("使用者輸入不可為空。")

        # 把 Agent 身份與任務提示合併，確保每次呼叫都有一致上下文。
        full_system_prompt = (
            f"Agent 名稱：{self.config.name}\n"
            f"角色：{self.config.role}\n\n"
            f"{self.config.system_prompt}"
        )
        try:
            response = await asyncio.wait_for(
                self.llm_service.chat(
                    user_input,
                    system_prompt=full_system_prompt,
                    temperature=self.config.temperature,
                    seed=self.config.seed,
                    max_output_tokens=self.config.max_output_tokens,
                    json_schema=self.config.json_schema,
                ),
                timeout=self.config.timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            raise AgentError("Agent 等待模型回覆逾時。") from error
        except LLMServiceError as error:
            raise AgentError(f"Agent 無法取得模型回覆：{error}") from error

        data = None
        if self.config.json_schema is not None:
            # Schema 啟用時只接受 JSON 物件或陣列，避免回傳不符合預期的純量。
            try:
                data = json.loads(response.content)
            except json.JSONDecodeError as error:
                raise AgentError("模型回覆不是合法 JSON。") from error
            if not isinstance(data, (dict, list)):
                raise AgentError("模型 JSON 回覆必須是物件或陣列。")

        return AgentResponse(response.content, data, response.usage)
