"""直接使用 HTTPX 呼叫 Ollama `/api/chat` 的教學版本。"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx


# Ollama 預設在本機 11434 Port 提供 HTTP API。
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"

logger = logging.getLogger(__name__)


class LLMServiceAPIError(RuntimeError):
    """直接呼叫 Ollama HTTP API 時發生的可預期錯誤。"""


@dataclass(frozen=True)
class LLMUsageAPI:
    """單次 HTTP API 呼叫的時間與 Token 統計。"""

    request_duration_seconds: float
    total_duration_ns: int | None
    load_duration_ns: int | None
    prompt_tokens: int | None
    completion_tokens: int | None

    @property
    def total_tokens(self) -> int | None:
        # 只有輸入與輸出 Token 都存在時，才計算總 Token。
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class LLMResponseAPI:
    """從 Ollama JSON 整理出的回答與統計資料。"""

    content: str
    usage: LLMUsageAPI


class LLMServiceAPI:
    """使用 `httpx.AsyncClient` 直接呼叫 Ollama HTTP API。"""

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # 參數優先，其次讀取環境變數，最後使用本機預設值。
        self.host = (host or os.getenv("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)).rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
        self.timeout = timeout
        # 正式執行時為 None；測試時注入 MockTransport 攔截 HTTP 請求。
        self.transport = transport

    async def chat(self, message: str) -> LLMResponseAPI:
        """呼叫 POST `/api/chat` 並解析非串流 JSON 回覆。"""

        # 這份 dict 會由 HTTPX 自動轉換成 JSON Request Body。
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": message}],
            # Ollama 預設可能串流；False 會一次取得完整 JSON。
            "stream": False,
        }
        started_at = time.perf_counter()

        try:
            # async with 會在請求結束後自動關閉 HTTP 連線資源。
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"{self.host}/api/chat",
                    json=payload,
                )
                # 4xx 或 5xx 狀態會在這裡轉成 HTTPStatusError。
                response.raise_for_status()
        except httpx.TimeoutException as error:
            raise LLMServiceAPIError("Ollama 回應逾時，請稍後再試。") from error
        except httpx.HTTPStatusError as error:
            raise LLMServiceAPIError(
                f"Ollama API 回傳錯誤狀態：{error.response.status_code}。"
            ) from error
        except httpx.RequestError as error:
            raise LLMServiceAPIError(
                "無法連線到 Ollama，請確認服務是否已啟動。"
            ) from error

        request_duration = time.perf_counter() - started_at

        try:
            # 將 Response Body 從 JSON 轉換成 Python dict。
            data = response.json()
        except ValueError as error:
            raise LLMServiceAPIError("Ollama 回傳的資料不是有效 JSON。") from error

        if not isinstance(data, dict):
            raise LLMServiceAPIError("Ollama 回傳格式不正確。")

        # Ollama 的回答文字位於 message.content。
        message_data = data.get("message")
        content = message_data.get("content") if isinstance(message_data, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise LLMServiceAPIError("Ollama 回傳內容缺少 message.content。")

        # Ollama 的 duration 單位是奈秒，count 欄位則是 Token 數量。
        usage = LLMUsageAPI(
            request_duration_seconds=request_duration,
            total_duration_ns=_optional_int(data.get("total_duration")),
            load_duration_ns=_optional_int(data.get("load_duration")),
            prompt_tokens=_optional_int(data.get("prompt_eval_count")),
            completion_tokens=_optional_int(data.get("eval_count")),
        )

        # 不記錄使用者問題與回答，避免私人內容進入日誌。
        logger.info(
            "Ollama API 推論完成 model=%s request_seconds=%.3f "
            "total_duration_ns=%s load_duration_ns=%s prompt_tokens=%s "
            "completion_tokens=%s total_tokens=%s",
            self.model,
            usage.request_duration_seconds,
            usage.total_duration_ns,
            usage.load_duration_ns,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
        )

        return LLMResponseAPI(content=content.strip(), usage=usage)


def _optional_int(value: object) -> int | None:
    """統計欄位若不是整數，就以 None 表示缺少資料。"""

    return value if isinstance(value, int) and not isinstance(value, bool) else None
