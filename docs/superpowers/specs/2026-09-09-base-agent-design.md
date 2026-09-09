# DAY 11：共用 BaseAgent 設計

## 目標

建立所有 AI 角色共用的 `BaseAgent`，集中管理角色資料、System Prompt 與模型生成參數。Agent 共用既有的 `LLMService`，不直接依賴 Ollama Client，讓後續角色容易新增並可使用 Fake LLM Service 測試。

## 範圍

本次包含：

- 定義 Agent 名稱、角色與 System Prompt。
- 建立統一的非同步 `respond()` 介面。
- 讓多個 Agent 共用同一個 `LLMService` 實例。
- 支援 Agent 層級的逾時、Temperature、Seed、最大輸出 Token 與選用 JSON Schema。
- 建立兩個最小 Fake LLM Service 測試。

本次不包含：

- 建立特定業務角色，例如 PM Agent 或工程師 Agent。
- 將 BaseAgent 接入 Discord 指令。
- 對 JSON Schema 進行第二次本機欄位驗證。
- 修改 HTTP API 版本的 `llm_serviceapi.py`。

## 架構

新增 `agents/base_agent.py`，包含以下元件：

### AgentConfig

不可變的 dataclass，保存：

- `name: str`：Agent 顯示名稱。
- `role: str`：Agent 的職責描述。
- `system_prompt: str`：具體行為規則。
- `timeout_seconds: float = 60.0`：單次 `respond()` 的最長等待時間。
- `temperature: float = 0.2`：控制輸出的隨機程度。
- `seed: int = 42`：讓相同輸入較容易重現結果。
- `max_output_tokens: int = 500`：傳給 Ollama 的 `num_predict`。
- `json_schema: Mapping[str, object] | None = None`：選用的結構化輸出格式。

`AgentConfig` 建立時驗證必要文字不可為空、逾時與最大輸出必須大於零。這些預設值日後可以由個別 Agent 覆寫。

### AgentResponse

不可變的 dataclass，統一保存：

- `content: str`：模型回傳的文字。
- `data: dict[str, object] | list[object] | None`：使用 JSON Schema 時解析後的 JSON，普通文字回覆為 `None`。
- `usage: LLMUsage`：沿用現有推論時間與 Token 統計。

即使 Agent 是否使用 JSON Schema 不同，呼叫端仍會收到相同的回傳型別。

### BaseAgent

建構時接收 `AgentConfig` 與符合 LLM Service Protocol 的物件。公開介面為：

```python
async def respond(self, user_input: str) -> AgentResponse:
    ...
```

`respond()` 會：

1. 驗證並整理使用者輸入。
2. 將名稱、角色與 System Prompt 組成完整的 system 訊息。
3. 將生成參數交給共用的 `LLMService.chat()`。
4. 使用 Agent 自己的逾時設定限制整次等待時間。
5. 普通文字回覆直接建立 `AgentResponse`。
6. 有 JSON Schema 時使用 `json.loads()` 解析內容，再放入 `AgentResponse.data`。

## LLMService 介面調整

擴充現有 `LLMService.chat()`，增加選用的 keyword-only 參數：

```python
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
    ...
```

參數轉換方式：

- `system_prompt` 轉成 `{"role": "system", "content": ...}`，放在 user 訊息之前。
- `temperature` 放入 Ollama `options["temperature"]`。
- `seed` 放入 Ollama `options["seed"]`。
- `max_output_tokens` 放入 Ollama `options["num_predict"]`。
- `json_schema` 傳給 Ollama `format`。
- 保持 `stream=False`。

只有呼叫端提供選用參數時才傳給 Ollama，因此現有的 `LLMService.chat("問題")`、`!ask` 與既有測試仍維持原本行為。

## 資料流

```text
使用者輸入
  → BaseAgent.respond()
  → AgentConfig（角色與生成參數）
  → 共用 LLMService.chat()
  → ollama.AsyncClient.chat()
  → LLMResponse
  → AgentResponse
```

## 錯誤處理

新增 `AgentError`，作為 Agent 對外的統一可預期錯誤：

- `respond()` 超過 Agent 設定的等待時間時，轉成清楚的逾時訊息。
- `LLMServiceError` 由 `AgentError` 包裝，並使用例外鏈保留原始原因。
- JSON Schema 模式收到無法解析的 JSON 時，拋出 `AgentError`。
- 空白的使用者輸入在呼叫 LLM 前拒絕。

Ollama 會根據 `format` 中的 JSON Schema 約束生成內容；BaseAgent 負責解析 JSON。本次不新增 `jsonschema` 或 Pydantic 依賴做第二次本機驗證。

## 測試

新增 `tests/test_base_agent.py`，使用最小的 Fake LLM Service，不啟動 Ollama：

1. 普通文字測試：確認 `respond()` 正確傳遞完整 System Prompt、Temperature、Seed、最大輸出及 `json_schema=None`，並回傳 `data=None`。
2. JSON 測試：確認 JSON Schema 正確傳入，且回覆內容能解析至 `AgentResponse.data`。

既有 `tests/test_llm_service.py` 會補充一個測試，確認 Agent 生成參數會正確轉成 Ollama 的 `messages`、`options` 與 `format`。既有測試與完整測試套件都必須通過。

## 檔案變更

- 新增 `agents/__init__.py`
- 新增 `agents/base_agent.py`
- 新增 `tests/test_base_agent.py`
- 修改 `services/llm_service.py`
- 修改 `tests/test_llm_service.py`

不修改 `bot.py`、專案 Repository 或目前的 Discord 指令。
