# Shared BaseAgent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可共用 `LLMService`、統一 `respond()` 介面並支援固定生成參數與選用 JSON Schema 的 BaseAgent。

**Architecture:** 擴充現有 `LLMService.chat()` 的選用參數，維持目前 `!ask` 呼叫相容；新增獨立 `agents/base_agent.py`，由 `AgentConfig` 保存角色與生成設定，`BaseAgent` 負責逾時、錯誤轉換及 JSON 解析。測試以 Fake Ollama Client 和 Fake LLM Service 驗證邊界，不連線至真實 Ollama。

**Tech Stack:** Python 3.10、`asyncio`、`dataclasses`、`typing.Protocol`、`ollama==0.3.3`、`unittest.IsolatedAsyncioTestCase`

**Spec:** `docs/superpowers/specs/2026-09-09-base-agent-design.md`

## Global Constraints

- `AgentConfig` 預設逾時為 `60.0` 秒、Temperature 為 `0.2`、Seed 為 `42`、最大輸出為 `500` tokens。
- JSON Schema 必須是選用設定；普通文字 Agent 使用 `None`。
- 保持現有 `LLMService.chat("問題")` 和 Discord `!ask` 相容。
- 保持 Ollama `stream=False`，且不修改 `services/llm_serviceapi.py`。
- 不新增 `jsonschema` 或 Pydantic 依賴。
- 測試不可啟動或連線至真實 Ollama。
- 所有新增程式包含適合學習閱讀的繁體中文註解。

---

## File Structure

- `services/llm_service.py`：唯一的 Ollama Python Client 呼叫與回覆統計解析入口；新增 system、options 與 format 參數轉換。
- `tests/test_llm_service.py`：使用 Fake Ollama Client 驗證 BaseAgent 所需參數轉成正確 Ollama 請求。
- `agents/__init__.py`：標記 Agent package，匯出共用 Agent 型別。
- `agents/base_agent.py`：定義 `AgentConfig`、`AgentResponse`、`AgentError`、LLM Protocol 與 `BaseAgent.respond()`。
- `tests/test_base_agent.py`：使用 Fake LLM Service 驗證普通文字及 JSON Schema 兩條資料流。

### Task 1: 擴充 LLMService 的 Agent 生成參數

**Files:**
- Modify: `services/llm_service.py:55-96`
- Test: `tests/test_llm_service.py`

**Interfaces:**
- Consumes: 既有 `LLMResponse`、`LLMUsage` 與 `OllamaClientProtocol`。
- Produces: `LLMService.chat(message, *, system_prompt=None, temperature=None, seed=None, max_output_tokens=None, json_schema=None) -> LLMResponse`。

- [ ] **Step 1: 寫入會失敗的 Ollama 請求參數測試**

在 `LLMServiceTest` 新增：

```python
async def test_chat_passes_agent_options_and_json_schema(self):
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }
    fake_client = FakeOllamaClient(
        response={"message": {"content": '{"summary":"完成"}'}}
    )
    service = LLMService(client=fake_client)

    await service.chat(
        "整理需求",
        system_prompt="你是專案經理。",
        temperature=0.2,
        seed=42,
        max_output_tokens=500,
        json_schema=schema,
    )

    self.assertEqual(
        fake_client.calls,
        [{
            "model": "qwen3.5:4b",
            "messages": [
                {"role": "system", "content": "你是專案經理。"},
                {"role": "user", "content": "整理需求"},
            ],
            "stream": False,
            "options": {
                "temperature": 0.2,
                "seed": 42,
                "num_predict": 500,
            },
            "format": schema,
        }],
    )
```

- [ ] **Step 2: 執行新測試並確認因介面尚未支援而失敗**

Run: `.venv/bin/python -m unittest tests.test_llm_service.LLMServiceTest.test_chat_passes_agent_options_and_json_schema -v`

Expected: ERROR，訊息包含 `unexpected keyword argument 'system_prompt'`。

- [ ] **Step 3: 實作選用參數與 Ollama 請求轉換**

將 Client Protocol 改成接受 Ollama 額外關鍵字，並擴充 `chat()`：

```python
class OllamaClientProtocol(Protocol):
    async def chat(self, **kwargs: Any) -> Any:
        """產生一次非串流聊天回應。"""


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
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": message})

    request: dict[str, Any] = {
        "model": self.model,
        "messages": messages,
        "stream": False,
    }
    options: dict[str, object] = {}
    if temperature is not None:
        options["temperature"] = temperature
    if seed is not None:
        options["seed"] = seed
    if max_output_tokens is not None:
        options["num_predict"] = max_output_tokens
    if options:
        request["options"] = options
    if json_schema is not None:
        request["format"] = dict(json_schema)

    response = await self.client.chat(**request)
```

保留既有計時、錯誤轉換、內容解析、Token 統計與安全日誌程式碼。

- [ ] **Step 4: 執行 LLMService 測試並確認新舊行為都通過**

Run: `.venv/bin/python -m unittest tests.test_llm_service -v`

Expected: 5 tests，全部 `ok`。

- [ ] **Step 5: 提交 LLMService 介面變更**

```bash
git add services/llm_service.py tests/test_llm_service.py
git commit -m "feat: support agent generation options"
```

### Task 2: 建立共用 BaseAgent 與 Fake LLM Service 測試

**Files:**
- Create: `agents/__init__.py`
- Create: `agents/base_agent.py`
- Test: `tests/test_base_agent.py`

**Interfaces:**
- Consumes: Task 1 的 `LLMService.chat()` 與既有 `LLMResponse`、`LLMUsage`。
- Produces: `AgentConfig`、`AgentResponse`、`AgentError`、`AgentLLMServiceProtocol`、`BaseAgent.respond(user_input) -> AgentResponse`。

- [ ] **Step 1: 建立兩個會失敗的 Fake LLM Service 測試**

新增 `tests/test_base_agent.py`：

```python
import unittest

from agents.base_agent import AgentConfig, BaseAgent
from services.llm_service import LLMResponse, LLMUsage


def make_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        usage=LLMUsage(0.01, None, None, 10, 5),
    )


class FakeLLMService:
    def __init__(self, response: LLMResponse):
        self.response = response
        self.calls = []

    async def chat(self, message: str, **kwargs):
        self.calls.append({"message": message, **kwargs})
        return self.response


class BaseAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_respond_passes_identity_prompt_and_default_options(self):
        fake_llm = FakeLLMService(make_response("需求已整理。"))
        agent = BaseAgent(
            AgentConfig(
                name="小企",
                role="專案經理",
                system_prompt="請整理使用者的專案需求。",
            ),
            fake_llm,
        )

        result = await agent.respond("建立 Discord Bot")

        self.assertEqual(result.content, "需求已整理。")
        self.assertIsNone(result.data)
        self.assertEqual(fake_llm.calls[0]["message"], "建立 Discord Bot")
        self.assertEqual(
            fake_llm.calls[0]["system_prompt"],
            "Agent 名稱：小企\n角色：專案經理\n\n請整理使用者的專案需求。",
        )
        self.assertEqual(fake_llm.calls[0]["temperature"], 0.2)
        self.assertEqual(fake_llm.calls[0]["seed"], 42)
        self.assertEqual(fake_llm.calls[0]["max_output_tokens"], 500)
        self.assertIsNone(fake_llm.calls[0]["json_schema"])

    async def test_respond_parses_json_when_schema_is_configured(self):
        schema = {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        }
        fake_llm = FakeLLMService(make_response('{"summary":"建立 Bot"}'))
        agent = BaseAgent(
            AgentConfig("小企", "專案經理", "整理需求。", json_schema=schema),
            fake_llm,
        )

        result = await agent.respond("建立 Discord Bot")

        self.assertEqual(result.data, {"summary": "建立 Bot"})
        self.assertEqual(fake_llm.calls[0]["json_schema"], schema)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 執行 BaseAgent 測試並確認因模組尚未建立而失敗**

Run: `.venv/bin/python -m unittest tests.test_base_agent -v`

Expected: ERROR，訊息包含 `No module named 'agents'`。

- [ ] **Step 3: 建立 AgentConfig、AgentResponse 與 BaseAgent**

新增 `agents/base_agent.py`，核心實作如下；所有公開類別與重要流程補上繁體中文註解：

```python
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
    name: str
    role: str
    system_prompt: str
    timeout_seconds: float = 60.0
    temperature: float = 0.2
    seed: int = 42
    max_output_tokens: int = 500
    json_schema: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.role.strip() or not self.system_prompt.strip():
            raise ValueError("Agent 名稱、角色與 System Prompt 不可為空。")
        if self.timeout_seconds <= 0:
            raise ValueError("Agent 逾時秒數必須大於 0。")
        if self.max_output_tokens <= 0:
            raise ValueError("Agent 最大輸出 Token 必須大於 0。")


@dataclass(frozen=True)
class AgentResponse:
    content: str
    data: dict[str, object] | list[object] | None
    usage: LLMUsage


class AgentLLMServiceProtocol(Protocol):
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
    def __init__(self, config: AgentConfig, llm_service: AgentLLMServiceProtocol) -> None:
        self.config = config
        self.llm_service = llm_service

    async def respond(self, user_input: str) -> AgentResponse:
        user_input = user_input.strip()
        if not user_input:
            raise AgentError("使用者輸入不可為空。")

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
            try:
                data = json.loads(response.content)
            except json.JSONDecodeError as error:
                raise AgentError("模型回覆不是合法 JSON。") from error
            if not isinstance(data, (dict, list)):
                raise AgentError("模型 JSON 回覆必須是物件或陣列。")

        return AgentResponse(response.content, data, response.usage)
```

在 `agents/__init__.py` 匯出五個公開型別：

```python
"""AI Agent 共用元件。"""

from agents.base_agent import (
    AgentConfig,
    AgentError,
    AgentLLMServiceProtocol,
    AgentResponse,
    BaseAgent,
)

__all__ = [
    "AgentConfig",
    "AgentError",
    "AgentLLMServiceProtocol",
    "AgentResponse",
    "BaseAgent",
]
```

- [ ] **Step 4: 執行 BaseAgent 測試並確認通過**

Run: `.venv/bin/python -m unittest tests.test_base_agent -v`

Expected: 2 tests，全部 `ok`。

- [ ] **Step 5: 提交 BaseAgent 與 Fake 測試**

```bash
git add agents/__init__.py agents/base_agent.py tests/test_base_agent.py
git commit -m "feat: add shared base agent"
```

### Task 3: 完整回歸驗證

**Files:**
- Verify: `bot.py`
- Verify: `services/llm_service.py`
- Verify: `agents/base_agent.py`
- Verify: `tests/`

**Interfaces:**
- Consumes: Task 1 與 Task 2 的完整實作。
- Produces: 通過既有與新增測試、可被後續具體 Agent 使用的 DAY 11 基礎層。

- [ ] **Step 1: 執行完整單元測試**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: 既有 28 tests 加新增 3 tests，共 31 tests，全部 `ok`。

- [ ] **Step 2: 檢查 Python 語法**

Run: `.venv/bin/python -m compileall -q agents services tests bot.py config.py`

Expected: exit code `0`，沒有語法錯誤輸出。

- [ ] **Step 3: 檢查 Git 差異格式與範圍**

Run: `git diff --check`

Expected: exit code `0`，沒有尾端空白或衝突標記。確認功能 commit 不包含 `.gitignore`、`ai company icon.png` 或 `projects.json` 的既有本機變更。
