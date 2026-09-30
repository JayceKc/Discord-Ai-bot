# Discord AI Agent Company

這是一個使用 Discord、Ollama 與多個 AI Agent 模擬專案會議的學習專案。

目前預設模型為 `qwen3.5:4b`。使用者可以在 Discord 啟動專案，讓 PM、Research、Creative、Finance 與 Review Agent 依序討論，最後產生經過審查的提案。

> [!IMPORTANT]
> 每次啟動前，請先完成下面的「啟動前必讀清單」。遇到問題時，再閱讀 [TECHNICAL_GUIDE.md](TECHNICAL_GUIDE.md)。

## 啟動前必讀清單

每次執行 `python bot.py` 前，依序確認：

- [ ] 終端機位於專案根目錄 `Ai-Company`
- [ ] 虛擬環境顯示 `(.venv)`
- [ ] `.env` 已設定 `DISCORD_TOKEN`
- [ ] Discord Bot 邀請時包含 `bot` 與 `applications.commands` Scope
- [ ] Discord Developer Portal 已啟用 Message Content Intent
- [ ] Ollama 已在 `127.0.0.1:11434` 執行
- [ ] 已下載 `qwen3.5:4b`
- [ ] MySQL 已啟動，`.env` 已設定 `DB_PASSWORD` 與 `MYSQL_ROOT_PASSWORD`
- [ ] 測試全部通過
- [ ] 若修改過 Slash Command，已重新啟動 Bot 等待同步
- [ ] 確認 `meetings.json` 是否要延續舊會議，避免誤用快取結果

建議每次直接執行：

```bash
source .venv/bin/activate
ollama list
curl http://127.0.0.1:11434/api/tags
python -m unittest discover -s tests
python bot.py
```

如果 `curl` 能取得模型清單，就不需要再次執行 `ollama serve`。

## 功能

### 文字指令

| 指令 | 功能 |
|---|---|
| `!hello` | 測試 Bot 是否正常回覆 |
| `!ask <問題>` | 將問題交給本機 Qwen 模型 |

### 指定頻道的自然語言入口

在 `.env` 設定 `PROJECT_INTAKE_CHANNEL_ID=你的頻道ID`，重啟 Bot 後，只有該頻道的普通訊息會觸發專案收集；`0`（預設）代表關閉。其他頻道不會觸發，原有 Slash Command 不受此設定限制。Bot 需要該頻道的「檢視頻道」「傳送訊息」權限，且 Discord Developer Portal 的 Message Content Intent 必須啟用。

在指定頻道輸入「我要做一個網站」可直接觸發；「我要開一間咖啡廳」等未包含固定觸發詞的說法，會由本機模型判斷是否為建立專案的意圖。模型判定為新專案且信心值至少為 `0.8` 時，Bot 會先顯示理解到的專案名稱，使用者回覆「確認」後才開始收集資料。判定不明確或模型暫時無法使用時，Bot 會提供「我要做一個網站」等輸入範例，不會直接建立資料。

確認意圖後，Bot 會依序詢問類型、需求、預算、期限及驗收條件。核對完整資料並再次回覆「確認」後，才會建立專案、執行第一輪並展示 PM 草案；此時回覆「直接定稿」會跳過第二輪，進入 Review 與最終方案，回覆「修改：加入搜尋功能」才會保存需求變更並進行第二輪。建立前可回覆「取消」。未提交草稿及等待回覆的狀態只保存在 Bot 記憶體，閒置 30 分鐘或 Bot 重啟後會消失；已建立的專案與會議仍在 MySQL。訊息會公開在該頻道，請不要輸入密碼等敏感資料。

若 Guild 已有進行中專案，Bot 會擋下新的建立與開會，避免產生無法啟動的專案。完整會議保存最終方案後，會將專案標為 `completed` 並釋放目前專案。已建立專案的流程若中斷，訊息會提示「重試」；Bot 重啟後會失去對話階段，已保存的第一輪草案可用 `/review` 定稿。

### Slash Command

| 指令 | 功能 |
|---|---|
| `/hello` | 測試 Interaction、defer 與 Followup |
| `/help` | 顯示指令說明 |
| `/projects` | 顯示可使用的專案 |
| `/workspace` | 查看工作區、預設優先目標與成果數 |
| `/priority <priority>` | 管理員設定下一場會議的優先目標 |
| `/meeting <project_id> <change_id>` | 一次執行兩輪討論到最終方案 |
| `/start <project_id>` | 啟動專案並執行第一輪 Agent 討論 |
| `/change <change_id>` | 套用一次需求變更並執行第二輪討論 |
| `/draft` | 由 PM 整合已完成的一輪或兩輪內容為草案 |
| `/review` | 審查草案、最多修改一次並產生最終方案 |

完整操作順序：

```text
/projects
   ↓
/start PRJ-003
   ↓
/change CHG-002
   ↓
/draft
   ↓
/review
```

## 安裝

### 1. 建立虛擬環境

本專案開發環境使用 Python 3.13。建議使用 Python 3.11 以上版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. 安裝套件

```bash
python -m pip install -r requirements.txt
```

主要套件：

- `discord.py`：Discord Bot 與 Slash Command
- `ollama`：非同步呼叫本機 Qwen
- `pydantic`：結構化輸出驗證
- `python-dotenv`：讀取 `.env`
- `httpx`：Ollama 底層連線與逾時錯誤

### 3. 準備 Ollama 模型

```bash
ollama pull qwen3.5:4b
ollama list
```

只有 Ollama 尚未啟動時，才執行：

```bash
ollama serve
```

### 4. 設定環境變數

```bash
cp .env.example .env
```

至少設定：

```dotenv
DISCORD_TOKEN=你的_Discord_Bot_Token
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen3.5:4b
```

不要把 `.env`、Discord Token 或私人會議紀錄提交到 GitHub。

### 5. 啟動 MySQL 與更新 schema

Day 22 起，Bot 從 MySQL 讀取專案與會議資料。第一次使用先設定 `.env` 的
`DB_PASSWORD`、`MYSQL_ROOT_PASSWORD`，再執行：

```bash
docker compose up -d mysql
docker compose exec mysql mysqladmin ping -h 127.0.0.1 -uai_company_app -p
```

既有 Day 22 資料庫要先套用 Day 23 migration（空資料庫在 Docker 首次初始化時會自動執行）：

```bash
docker compose exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot ai_company' < database/migrations/002_day23_workspace_custom_project.sql
```

若不需要舊 JSON 資料，無須執行匯入腳本；Bot 正式執行只讀 MySQL。這個 migration 不清除現有資料或 Docker volume。執行時若遇到 metadata lock timeout，先在 DBeaver 結束未提交交易，再重跑 migration。

Day 23 測試流程：管理員先用 `/priority` 選目標，接著在指定頻道輸入「我要做一個網站」並依提示建立專案、檢視第一輪草案，再選擇「直接定稿」或「修改：具體需求」。完成後用 `/workspace` 查看成果數。完整會議保存最終方案後，專案會自動標為 `completed`，同一伺服器可開始下一個專案；尚未產生最終方案的失敗或中斷會議仍保持進行中，以便接續。`/custom_project` 已移除，底層 MySQL 專案建立功能仍保留。

Day 24 起，Review 會對完整度、創意、可信度、可行性分別給出 1～5 分與理由。Python 依會議開始時保存的優先目標套用固定權重，計算 0～100 品質指標，並整理風險與改善建議。若草案需要修改，PM 產生最終方案後會再次 Review；Discord 分別顯示草案與最終方案的分數。草案評分保存在 `meetings.review_result.quality_evaluation`，修改後評分保存在 `meetings.review_result.post_revision_review.quality_evaluation`，不需要額外 migration。最多仍只修改一次；第二次 Review 若仍指出問題，會顯示該結果，不會再次修改。

## Day 25：使用者決策與按鈕

既有資料庫先套用新增欄位 migration（本次開發環境已套用）：

```bash
docker compose exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot ai_company' < database/migrations/003_day25_user_decision.sql
```

重啟 Bot 後，新會議會保存啟動者為決策者。`/meeting`、`/start` 後接 `/draft`、`/review`，以及自然語言入口都會進入同一決策流程：

1. PM 整理一項重要分歧或需求取捨，提供 2～3 個選項，說明做法、好處與代價。
2. 原啟動者按 A／B／C，PM 依選擇修改方案，Review 重新評分。
3. 顯示完整候選方案、修改前後內容、品質指標差值與剩餘風險。
4. 按「批准方案」才正式完成專案並釋放 Guild；按「駁回、重新選擇」可改選另一個方向。

按鈕失效或訊息遺失時，可使用 `/decide choice:A`（或 B／C）、`/decide choice:approve`、`/decide choice:reject`。`/review` 可重新顯示目前狀態，沿用已保存的選項。只有原啟動者能選擇、批准與駁回，其他成員仍可查看公開內容。

候選方案保存在 `candidate_proposal`，對應評分保存在 `candidate_review`，選擇、影響、各輪評分與批准／駁回歷史保存在 `user_decision`。等待確認期間 `final_proposal` 為空，不計入已完成成果。原有一次自動修改額度仍適用；使用者改選不重設額度。Review 若仍有問題，會完整顯示風險，由使用者決定是否接受。

Bot 重啟會恢復持久按鈕。套用選擇時發生模型錯誤，可用相同 `/decide choice` 續跑；Bot 在處理中意外中斷時，處理保留最多 15 分鐘，再允許續跑。舊會議保留原流程與原評分，不補造決策者。


## 執行

```bash
source .venv/bin/activate
python bot.py
```

啟動後應看到類似日誌：

```text
準備啟動 Bot（Ollama host=http://localhost:11434 model=qwen3.5:4b）
已同步 N 個斜線指令
機器人已上線
```

## 測試

執行全部測試：

```bash
python -m unittest discover -s tests
```

只測單一檔案：

```bash
python -m unittest tests.test_meeting_manager
python -m unittest tests.test_bot
python -m unittest tests.test_review_agent
```

Fake 測試通過代表程式流程正確，不代表 Ollama 一定已經啟動。正式連線仍需另外執行：

```bash
python manual_test.py
```

## 專案結構

```text
Ai-Company/
├── agents/                 # Agent 身份、Prompt 與 Pydantic Schema
├── models/                 # Project、Meeting 與狀態資料模型
├── repositories/           # JSON 資料存取
├── services/               # Ollama、會議流程與專案服務
├── tests/                  # Fake LLM 與流程測試
├── bot.py                  # Discord 指令與依賴組裝
├── config.py               # 環境設定
├── projects.json           # 範例專案及需求變更
├── guild_projects.json     # Guild 目前專案，執行後產生
└── meetings.json           # 會議、草案與審查狀態，執行後產生
```

## 快速排錯

### Ollama 顯示 address already in use

```text
Error: listen tcp 127.0.0.1:11434: bind: address already in use
```

這通常表示 Ollama 已經啟動，不是服務壞掉。先執行：

```bash
curl http://127.0.0.1:11434/api/tags
```

有回應就直接啟動 Bot，不要再開第二個 `ollama serve`。

### Discord 看不到 Slash Command

確認：

1. 邀請連結包含 `applications.commands`。
2. Bot 有足夠的頻道權限。
3. 程式已重新啟動並成功執行 `CommandTree.sync()`。
4. Discord 客戶端已重新整理。

### Agent 執行失敗

先看終端機日誌，再依序確認 Ollama、模型名稱、Prompt 長度、輸出 Token 與 Pydantic Schema。不要只刪除錯誤訊息，詳細處理方式請看技術文件的「踩雷紀錄」。

## 詳細文件

完整架構、資料流程、JSON 欄位、限制設計與踩雷紀錄：

- [TECHNICAL_GUIDE.md](TECHNICAL_GUIDE.md)

官方參考：

- [discord.py 文件](https://discordpy.readthedocs.io/)
- [Ollama Python](https://github.com/ollama/ollama-python)
- [Pydantic 文件](https://docs.pydantic.dev/)
