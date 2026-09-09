# Task 2 實作與驗證報告

## 實作內容

- 新增 `agents/base_agent.py`：提供 `AgentConfig`、`AgentResponse`、`AgentError`、`AgentLLMServiceProtocol` 與 `BaseAgent.respond()`。
- `BaseAgent` 會組合 Agent 名稱、角色與 system prompt，傳入預設 temperature、seed、max output tokens 及選用的 JSON Schema。
- `respond()` 具備空輸入拒絕、逾時與 LLMServiceError 統一轉換，以及 JSON 物件／陣列解析。
- 新增 `agents/__init__.py`，匯出五個公開型別。
- 新增 `tests/test_base_agent.py`，依 brief 僅包含兩個 Fake LLM Service 測試。
- 所有新增 production/test 程式均加入學習友善的繁體中文註解或說明文字。

## TDD 紀錄

1. 先新增兩個測試，執行 `/Users/jaycekuan/Documents/Ai-Company/.venv/bin/python -m unittest tests.test_base_agent -v`。
2. RED：測試載入錯誤，訊息為 `ModuleNotFoundError: No module named 'agents'`，確認缺少 production module 導致失敗。
3. GREEN：新增共用 Agent 實作後，同一指令通過 2 tests。
4. 以完整測試套件驗證：`/Users/jaycekuan/Documents/Ai-Company/.venv/bin/python -m unittest discover -v`，共 31 tests，全部通過。

## Self-review

- `git diff --check` 通過，未發現空白錯誤。
- 僅修改本任務指定的三個檔案；未觸碰 bot、repositories、services/llm_serviceapi.py 或其他既有檔案。
- 測試確實驗證身份 prompt、預設選項、Fake 呼叫紀錄及 JSON 解析結果。
