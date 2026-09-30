"""Agent 錯誤型別，獨立模組可避免服務層與 Agent 基底類別循環匯入。"""


class AgentError(RuntimeError):
    """Agent 無法產生或解析回覆時的統一錯誤。"""


class RetryableAgentError(AgentError):
    """可在同一個會議步驟內安全重試的 Agent 錯誤。"""


class AgentTimeoutError(RetryableAgentError):
    """等待模型回覆超過 Agent 設定的時間。"""


class AgentServiceError(RetryableAgentError):
    """模型服務暫時無法完成請求。"""


class AgentInvalidJSONError(RetryableAgentError):
    """模型輸出不是可解析的 JSON。"""
