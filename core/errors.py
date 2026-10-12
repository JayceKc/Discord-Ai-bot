"""Stable error codes and public messages. Never render arbitrary exceptions."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import Enum
from uuid import uuid4


class ErrorCode(str, Enum):
    OLLAMA_UNAVAILABLE = 'OLLAMA_UNAVAILABLE'
    OLLAMA_TIMEOUT = 'OLLAMA_TIMEOUT'
    OLLAMA_MODEL_MISSING = 'OLLAMA_MODEL_MISSING'
    LLM_INVALID_OUTPUT = 'LLM_INVALID_OUTPUT'
    DB_UNAVAILABLE = 'DB_UNAVAILABLE'
    DB_TIMEOUT = 'DB_TIMEOUT'
    DB_TRANSACTION_FAILED = 'DB_TRANSACTION_FAILED'
    DB_COMMIT_UNKNOWN = 'DB_COMMIT_UNKNOWN'
    DB_BAD_CONFIG = 'DB_BAD_CONFIG'
    INVALID_ARGUMENT = 'INVALID_ARGUMENT'
    PROJECT_NOT_FOUND = 'PROJECT_NOT_FOUND'
    PROJECT_DATA_INVALID = 'PROJECT_DATA_INVALID'
    INVALID_STATE = 'INVALID_STATE'
    WORKFLOW_BUSY = 'WORKFLOW_BUSY'
    FORBIDDEN_ACTION = 'FORBIDDEN_ACTION'
    STALE_DECISION = 'STALE_DECISION'
    INTERACTION_EXPIRED = 'INTERACTION_EXPIRED'
    MESSAGE_MISSING = 'MESSAGE_MISSING'
    DISCORD_FORBIDDEN = 'DISCORD_FORBIDDEN'
    DISCORD_SEND_FAILED = 'DISCORD_SEND_FAILED'
    CONFIG_INVALID = 'CONFIG_INVALID'
    DISCORD_LOGIN_FAILED = 'DISCORD_LOGIN_FAILED'
    UNEXPECTED_ERROR = 'UNEXPECTED_ERROR'


class RetryPolicy(str, Enum):
    NONE = 'none'
    AFTER_FIX = 'after_fix'
    RESUME_CHECKPOINT = 'resume_checkpoint'
    VERIFY_RESULT_FIRST = 'verify_result_first'


CATALOGUE = {
    ErrorCode.OLLAMA_UNAVAILABLE: ('無法連線到 Ollama 模型服務。', '請管理員啟動或修正模型服務，再由已保存的步驟接續。', RetryPolicy.AFTER_FIX),
    ErrorCode.OLLAMA_TIMEOUT: ('Ollama 回應逾時。', '請先 /status 確認；討論用 /resume，草案與審查用原 /draft 或 /review 接續。', RetryPolicy.RESUME_CHECKPOINT),
    ErrorCode.OLLAMA_MODEL_MISSING: ('設定的模型尚未可用。', '請管理員確認模型名稱與安裝，再重試原指令。', RetryPolicy.AFTER_FIX),
    ErrorCode.LLM_INVALID_OUTPUT: ('模型回覆格式或內容未通過驗證。', '請先 /status；討論用 /resume，草案或審查用 /draft 或 /review 接續。', RetryPolicy.RESUME_CHECKPOINT),
    ErrorCode.DB_UNAVAILABLE: ('資料服務暫時無法使用。', '請管理員確認 MySQL；恢復後先 /status 或 /projects 確認保存結果。', RetryPolicy.VERIFY_RESULT_FIRST),
    ErrorCode.DB_TIMEOUT: ('資料服務處理逾時。', '請先 /status 或 /projects 確認保存結果，再依目前階段操作。', RetryPolicy.VERIFY_RESULT_FIRST),
    ErrorCode.DB_TRANSACTION_FAILED: ('這次資料交易未完成。', '請先 /status 確認檢查點，再重試原操作。', RetryPolicy.VERIFY_RESULT_FIRST),
    ErrorCode.DB_COMMIT_UNKNOWN: ('尚未確認這次操作是否已保存。', '請先 /status 或 /projects 查證；不要直接重送建立或批准。', RetryPolicy.VERIFY_RESULT_FIRST),
    ErrorCode.DB_BAD_CONFIG: ('資料服務設定需要修正。', '請管理員檢查帳號、權限與資料表，修正後重啟。', RetryPolicy.AFTER_FIX),
    ErrorCode.INVALID_ARGUMENT: ('指令或參數格式不正確。', '使用 /help 查看指令，或 /projects 查有效專案 ID。', RetryPolicy.NONE),
    ErrorCode.PROJECT_NOT_FOUND: ('找不到目前工作區可用的專案或變更。', '使用 /projects 查詢有效 ID。', RetryPolicy.NONE),
    ErrorCode.PROJECT_DATA_INVALID: ('已保存的專案資料不完整或格式不正確。', '請管理員補齊需求與驗收條件等專案資料；不要刪除紀錄或重複建立專案。', RetryPolicy.AFTER_FIX),
    ErrorCode.INVALID_STATE: ('目前階段無法執行這項操作。', '使用 /status 查看目前階段與下一步。', RetryPolicy.NONE),
    ErrorCode.WORKFLOW_BUSY: ('已有工作正在處理中。', '請等待並用 /status 查詢。', RetryPolicy.NONE),
    ErrorCode.FORBIDDEN_ACTION: ('你沒有執行這項操作的權限。', '請由原會議啟動者在原伺服器操作。', RetryPolicy.NONE),
    ErrorCode.STALE_DECISION: ('此決策或按鈕已失效。', '使用 /review 重新顯示最新決策，或 /decide 操作。', RetryPolicy.NONE),
    ErrorCode.INTERACTION_EXPIRED: ('原 Discord 互動已失效。', '重新發起指令；決策使用 /review 或 /decide。', RetryPolicy.NONE),
    ErrorCode.MESSAGE_MISSING: ('原 Discord 訊息已不存在。', '使用 /status 查詢，或 /review 重新顯示決策。', RetryPolicy.NONE),
    ErrorCode.DISCORD_FORBIDDEN: ('無法在此頻道顯示結果。', '請管理員確認頻道權限；已保存結果可用 /status 查詢。', RetryPolicy.AFTER_FIX),
    ErrorCode.DISCORD_SEND_FAILED: ('Discord 暫時無法顯示結果。', '使用 /status 或 /review 查詢已保存結果，不必重做已完成操作。', RetryPolicy.VERIFY_RESULT_FIRST),
    ErrorCode.CONFIG_INVALID: ('Bot 啟動設定不完整或格式不正確。', '請管理員依 .env.example 修正設定後重啟。', RetryPolicy.AFTER_FIX),
    ErrorCode.DISCORD_LOGIN_FAILED: ('Bot 無法登入 Discord。', '請管理員確認 Discord Token 與連線設定後重啟。', RetryPolicy.AFTER_FIX),
    ErrorCode.UNEXPECTED_ERROR: ('這次操作中斷。', '先 /status 確認保存結果；提供查詢代碼給管理員。', RetryPolicy.VERIFY_RESULT_FIRST),
}


class AppError(RuntimeError):
    """Legacy message arguments stay catch-compatible; never exposed by reports."""
    default_code = ErrorCode.UNEXPECTED_ERROR

    def __init__(self, message=None, *, code=None, operation=None, write_outcome=None, reference_id=None):
        self.code = code or (message if isinstance(message, ErrorCode) else self.default_code)
        self.reference_id = reference_id
        self.operation = operation
        self.write_outcome = write_outcome
        super().__init__(CATALOGUE[self.code][0] if message is None or isinstance(message, ErrorCode) else message)


@dataclass(frozen=True)
class ErrorContext:
    guild_id: int | None = None
    meeting_id: str | None = None
    command: str | None = None
    stage: str | None = None
    operation_id: str | None = None
    write_outcome: str = 'not_started'


@dataclass(frozen=True)
class ErrorReport:
    code: ErrorCode
    reference_id: str
    user_message: str
    next_action: str
    retry_policy: RetryPolicy
    write_outcome: str

    def text(self) -> str:
        saved = {'not_started': '尚未開始資料寫入。', 'rolled_back': '本次交易已回滾。',
                 'committed': '資料已保存；已完成步驟與目前狀態可重新查詢。', 'unknown': '保存結果尚未確認，請先查詢。'}
        return (f'❌ {self.user_message}\n目前保存狀態：{saved.get(self.write_outcome, saved["unknown"])}'
                f'\n下一步：{self.next_action}\n查詢代碼：{self.reference_id}（{self.code.value}）')


def error_chain(error):
    seen = set()
    for _ in range(8):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        yield error
        error = getattr(error, 'original', None) or error.__cause__


def classify_error(error: BaseException, context: ErrorContext | None = None) -> ErrorReport:
    import discord
    from discord import app_commands
    from discord.ext import commands
    import httpx
    from ollama import ResponseError
    from pymysql.err import MySQLError, InterfaceError
    context = context or ErrorContext(write_outcome='unknown')
    code = ErrorCode.UNEXPECTED_ERROR
    outcome = context.write_outcome
    chain = list(error_chain(error))
    # A database boundary knows the transaction phase; outer service wrappers do not.
    for item in chain:
        if getattr(item, 'write_outcome', None) is not None:
            outcome = item.write_outcome
        explicit = getattr(item, 'code', ErrorCode.UNEXPECTED_ERROR)
        if isinstance(explicit, ErrorCode) and explicit != ErrorCode.UNEXPECTED_ERROR:
            code = explicit
            if explicit.value.startswith('DB_'):
                break
    if code == ErrorCode.UNEXPECTED_ERROR:
        for item in chain:
            if isinstance(item, discord.LoginFailure):
                code = ErrorCode.DISCORD_LOGIN_FAILED
            elif isinstance(item, discord.NotFound):
                code = ErrorCode.INTERACTION_EXPIRED if item.code in (10062, 10015) else ErrorCode.MESSAGE_MISSING
            elif isinstance(item, discord.Forbidden):
                code = ErrorCode.DISCORD_FORBIDDEN
            elif isinstance(item, discord.InteractionResponded):
                code = ErrorCode.INTERACTION_EXPIRED
            elif isinstance(item, discord.HTTPException):
                code = ErrorCode.DISCORD_SEND_FAILED
            elif isinstance(item, (commands.MissingPermissions, commands.CheckFailure, app_commands.CheckFailure)):
                code = ErrorCode.FORBIDDEN_ACTION
            elif isinstance(item, (commands.CommandOnCooldown, app_commands.CommandOnCooldown)):
                code = ErrorCode.WORKFLOW_BUSY
            elif isinstance(item, (commands.CommandNotFound, commands.UserInputError, app_commands.TransformerError)):
                code = ErrorCode.INVALID_ARGUMENT
            elif isinstance(item, MySQLError):
                errno = item.args[0] if item.args and isinstance(item.args[0], int) else None
                code = (ErrorCode.DB_BAD_CONFIG if errno in (1044, 1045, 1049, 1054, 1064, 1146)
                        else ErrorCode.DB_UNAVAILABLE if isinstance(item, InterfaceError) or errno in (2002, 2003, 2006, 2013)
                        else ErrorCode.DB_TRANSACTION_FAILED)
            elif isinstance(item, ResponseError):
                code = ErrorCode.OLLAMA_MODEL_MISSING if item.status_code == 404 else ErrorCode.OLLAMA_UNAVAILABLE
            elif isinstance(item, httpx.TimeoutException):
                code = ErrorCode.OLLAMA_TIMEOUT
            elif isinstance(item, (ConnectionError, httpx.RequestError)):
                code = ErrorCode.OLLAMA_UNAVAILABLE
            elif isinstance(item, (asyncio.TimeoutError, OSError)):
                code = ErrorCode.DISCORD_SEND_FAILED if context.command == 'delivery' else ErrorCode.UNEXPECTED_ERROR
    if not any(getattr(item, 'write_outcome', None) for item in chain) and code in {
        ErrorCode.INVALID_ARGUMENT, ErrorCode.PROJECT_NOT_FOUND, ErrorCode.INVALID_STATE,
        ErrorCode.WORKFLOW_BUSY, ErrorCode.FORBIDDEN_ACTION, ErrorCode.STALE_DECISION,
    }:
        outcome = 'not_started'
    if outcome == 'unknown' and code.value.startswith('DB_') and code != ErrorCode.DB_BAD_CONFIG:
        code = ErrorCode.DB_COMMIT_UNKNOWN
    message, action, policy = CATALOGUE[code]
    if code.value.startswith(('OLLAMA_', 'LLM_')) and context.command == 'ask':
        action = '服務修復後重新輸入 !ask 問題。'
    reference_id = next((item.reference_id for item in chain if isinstance(item, AppError) and isinstance(item.reference_id, str) and re.fullmatch(r'[a-f0-9]{12}', item.reference_id)), None) or uuid4().hex[:12]
    return ErrorReport(code, reference_id, message, action, policy, outcome)


def safe_error_text(error, context=None):
    return classify_error(error, context).text()
