"""使用 Ollama 官方 Python 套件產生模型回覆。"""

from __future__ import annotations

import logging  # 記錄模型名稱、推論時間與 Token 數量。
import time  # 使用高精度計時器測量 Python 實際等待時間。
from dataclasses import dataclass  # 建立只保存資料的回覆與統計類別。
from typing import Any, Mapping, Protocol  # 描述回應型別與可替換的 Client 介面。

import httpx  # Ollama 套件底層連線及逾時錯誤的型別來源。
from ollama import AsyncClient, ResponseError  # Ollama 官方非同步 Client 與 API 錯誤。


# Ollama 本機服務的預設網址，以及這個專案固定使用的模型。
DEFAULT_OLLAMA_HOST = "http://localhost:11434"  # 沒有傳入 host 時使用本機 Ollama。
DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"  # 沒有傳入 model 時使用專案預設模型。

# 使用 Python logging 記錄統計，不使用 print，方便未來統一管理日誌。
logger = logging.getLogger(__name__)


class LLMServiceError(RuntimeError):
    """呼叫或解析 Ollama API 時發生的可預期錯誤。"""


@dataclass(frozen=True)
class LLMUsage:
    """單次 Ollama 推論的時間與 Token 統計。"""

    # Python 實際等待整個請求完成的秒數。
    request_duration_seconds: float
    # Ollama 回傳的時間單位是奈秒（nanosecond）。
    total_duration_ns: int | None
    load_duration_ns: int | None
    # prompt_eval_count 是輸入 Token，eval_count 是模型輸出 Token。
    prompt_tokens: int | None
    completion_tokens: int | None

    @property
    def total_tokens(self) -> int | None:
        # 只要其中一個統計缺失，就不猜測總 Token。
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class LLMResponse:
    """已解析的模型文字與推論統計。"""

    content: str
    usage: LLMUsage


class OllamaClientProtocol(Protocol):
    """正式 Client 與測試 Fake Client 共用的最小介面。"""

    async def chat(self, **kwargs: Any) -> Any:
        """產生一次非串流聊天回應。"""


class LLMService:
    """使用 `ollama.AsyncClient` 呼叫 `/api/chat`。"""

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        timeout: float = 300.0,
        client: OllamaClientProtocol | None = None,
    ) -> None:
        # 環境變數統一由 config.py 讀取；服務只接受傳入值或使用預設值。
        self.host = (host or DEFAULT_OLLAMA_HOST).rstrip("/")  # 避免網址結尾重複出現 /。
        self.model = model or DEFAULT_OLLAMA_MODEL  # 保存每次 chat() 要傳給 Ollama 的模型名。
        # 測試時可以注入 Fake Client；正式執行則建立官方 AsyncClient。
        self.client = client or AsyncClient(host=self.host, timeout=timeout)

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
        """傳送單輪聊天訊息並回傳文字與使用量統計。"""

        started_at = time.perf_counter()  # 高精度計時器，適合測量經過時間。

        try:
            # system prompt 有提供時才放在最前面，讓模型先收到角色與任務背景。
            messages: list[dict[str, str]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": message})

            request: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "stream": False,
            }
            # 將服務層的生成參數轉成 Ollama API 使用的欄位名稱；未提供的選項不送出。
            options: dict[str, object] = {}
            if temperature is not None:
                options["temperature"] = temperature
            if seed is not None:
                options["seed"] = seed
            if max_output_tokens is not None:
                options["num_predict"] = max_output_tokens
            if options:
                request["options"] = options
            # JSON Schema 是選用的結構化輸出限制，只有呼叫端提供時才設定 format。
            if json_schema is not None:
                request["format"] = dict(json_schema)

            # AsyncClient.chat 會由 Ollama 套件替我們呼叫 POST /api/chat。
            response = await self.client.chat(**request)
        except httpx.TimeoutException as error:
            # Ollama 套件底層使用 HTTPX，因此逾時會拋出 HTTPX 例外。
            raise LLMServiceError("Ollama 回應逾時，請稍後再試。") from error
        except ResponseError as error:
            # ResponseError 代表 Ollama API 回傳 4xx 或 5xx 等錯誤。
            status_code = getattr(error, "status_code", None)
            status_text = str(status_code) if status_code is not None else "未知"
            raise LLMServiceError(
                f"Ollama API 回傳錯誤狀態：{status_text}。"
            ) from error
        except httpx.RequestError as error:
            # 包含服務未啟動、網址錯誤或網路連線失敗。
            raise LLMServiceError("無法連線到 Ollama，請確認服務是否已啟動。") from error

        request_duration = time.perf_counter() - started_at  # 結束時間減開始時間即經過秒數。
        # 舊版套件回傳 dict，新版則回傳物件；_read_field 同時支援兩種。
        message_data = _read_field(response, "message")
        content = _read_field(message_data, "content")

        if not isinstance(content, str) or not content.strip():
            raise LLMServiceError("Ollama 回傳內容缺少 message.content。")

        # 解析 Ollama 回傳的效能與 Token 統計。
        usage = LLMUsage(
            request_duration_seconds=request_duration,
            total_duration_ns=_optional_int(_read_field(response, "total_duration")),
            load_duration_ns=_optional_int(_read_field(response, "load_duration")),
            prompt_tokens=_optional_int(_read_field(response, "prompt_eval_count")),
            completion_tokens=_optional_int(_read_field(response, "eval_count")),
        )

        # 日誌只記錄模型和統計，不記錄使用者問題或模型回答。
        logger.info(
            "Ollama 推論完成 model=%s request_seconds=%.3f total_duration_ns=%s "
            "load_duration_ns=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s",
            self.model,
            usage.request_duration_seconds,
            usage.total_duration_ns,
            usage.load_duration_ns,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
        )

        return LLMResponse(content=content.strip(), usage=usage)  # 將文字與統計一起交給 Bot。


def _read_field(value: object, field: str) -> object:
    """讀取舊版字典回應或新版 Ollama 回應物件的欄位。"""

    if isinstance(value, Mapping):  # 支援 ollama 0.3.x 的字典回應。
        return value.get(field)
    # 支援較新版 Ollama 套件的 ChatResponse / Message 物件。
    return getattr(value, field, None)


def _optional_int(value: object) -> int | None:
    """只接受 Ollama 正常回傳的整數統計值。"""

    return value if isinstance(value, int) and not isinstance(value, bool) else None
