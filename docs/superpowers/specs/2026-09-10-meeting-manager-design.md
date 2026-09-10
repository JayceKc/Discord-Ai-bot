# DAY 15 Meeting Manager 設計

## 目標

建立一個純 Python 的 Meeting Manager，依固定順序呼叫 PM、Research、Creative、Finance 與 Review Agent。系統必須保存各 Agent 的輸入與輸出、防止同一 Discord Guild 重複啟動會議，並支援取消、失敗及從失敗位置恢復。

本階段不新增或修改 Discord 指令；Discord 整合留給後續工作。

## Agent 執行順序與資料流

固定順序如下：

1. PM Agent
2. Research Agent
3. Creative Agent
4. Finance Agent
5. Review Agent

PM 接收原始專案需求。Research 接收原始需求與 PM 結果；Creative 接收原始需求與 Research 結果；Finance 接收原始需求、Research 與 Creative 結果；Review 接收所有前置結果組成的草案。

每位 Agent 開始前，Meeting Manager 先保存該次輸入。Agent 成功回覆後立即保存結構化輸出，再進入下一位 Agent。因此即使中途失敗，已完成的輸出仍可供恢復流程使用。

Review Agent 的 `revision_count` 在首次會議固定為 `0`。DAY 15 只負責完成一次固定順序的審查，不自動執行修改循環。

## 元件

### `models/meeting.py`

定義：

- `MeetingStatus`：`pending`、`running`、`completed`、`failed`、`cancelled`。
- `MeetingStepStatus`：`pending`、`running`、`completed`、`failed`、`cancelled`。
- `MeetingStepRecord`：Agent 名稱、順序、狀態、輸入、輸出及錯誤。
- `MeetingRecord`：會議 ID、Guild ID、專案 ID、原始需求、目前狀態、目前步驟、步驟紀錄與錯誤。
- JSON 序列化及反序列化方法。

模型負責判斷合法狀態轉換：

- `pending -> running | cancelled`
- `running -> completed | failed | cancelled`
- `failed -> running | cancelled`
- `completed` 與 `cancelled` 為終止狀態。

非法轉換必須拋出明確的資料或狀態錯誤。

### `repositories/meeting_repository.py`

建立 `MeetingRepository` Protocol 與 `JsonMeetingRepository`。

`meetings.json` 保存：

- 每個 Guild 指向目前或最近一場會議的 ID。
- 以會議 ID 保存完整 `MeetingRecord`。
- 每位 Agent 已保存的輸入與輸出。

Repository 提供建立、取得、更新及查詢 Guild 目前會議的方法。寫入採用先產生暫存檔再取代正式檔案，降低寫入中斷造成 JSON 損壞的風險。

### `services/meeting_manager.py`

`MeetingManager` 接收 Repository 與五位 Agent，共用其既有 `respond()` 介面。

主要方法：

- `start(guild_id, project_id, requirement)`：建立並執行新會議。
- `cancel(guild_id)`：將可取消的會議標記為取消，並中止目前的執行 Task。
- `resume(guild_id)`：只允許恢復 `failed` 會議，從第一個未完成或失敗步驟繼續。

Manager 使用每個 Guild 各自的 `asyncio.Lock`。呼叫 `start()` 或 `resume()` 時，如果同 Guild 的 Lock 已鎖定，或 Repository 顯示該 Guild 有 `pending`／`running` 會議，立即回報會議已在執行，不等待後重複啟動。不同 Guild 可同時執行。

Agent 的 Pydantic 回覆使用 `model_dump(mode="json")` 轉為可保存資料。傳給下一位 Agent 的內容使用 JSON，並由固定的步驟輸入建構函式組合，避免 Agent 自行猜測上一步格式。

## 取消、失敗與恢復

取消時，Repository 先保存 `cancelled` 狀態，再取消目前 Task。執行流程捕捉取消事件時不得把狀態覆寫為 `failed`。已保存的完成步驟保留，取消狀態不可恢復。

Agent 呼叫、資料驗證或保存發生錯誤時：

1. 將目前步驟標記為 `failed`。
2. 保存不含敏感資訊的錯誤訊息。
3. 將會議標記為 `failed`。
4. 對呼叫端拋出統一的 `MeetingManagerError`。

恢復時保留所有 `completed` 步驟，從第一個 `failed` 或 `pending` 步驟重新執行。若找不到失敗會議、會議已完成或已取消，恢復操作會被拒絕。

## 測試

使用 Fake Agent 與暫存 JSON Repository，不連線 Ollama。至少測試：

1. 五位 Agent 嚴格依 PM、Research、Creative、Finance、Review 順序執行。
2. 每一步輸入及輸出在進入下一步前已保存。
3. 同一 Guild 重複啟動被拒絕，不同 Guild 不互相阻擋。
4. Agent 發生錯誤時保存 `failed` 狀態與失敗位置。
5. 恢復時跳過已完成步驟，從失敗 Agent 繼續。
6. 取消後狀態為 `cancelled`，且不再呼叫後續 Agent。
7. 合法及非法會議狀態轉換。
8. JSON 寫入後可由新的 Repository 實例完整讀回。

## 完成標準

單元測試能證明 Python 依固定順序呼叫五位 Fake Agent，並能保存每一步資料、阻擋同 Guild 重複執行，以及正確處理取消、失敗與恢復。完整既有測試也必須繼續通過。
