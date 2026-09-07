"""集中讀取與驗證 Discord Bot 的環境設定。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """程式啟動後使用的設定；凍結後可避免執行途中被意外修改。"""

    discord_token: str
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen3.5:4b"
    ollama_timeout_seconds: float = 300.0
    max_ask_input_length: int = 500
    max_ask_output_length: int = 1900
    log_level: str = "INFO"


def load_settings() -> Settings:
    """從 .env 與環境變數建立設定，缺少必要資料時立即停止。"""

    load_dotenv()  # 將專案根目錄的 .env 載入環境變數。
    discord_token = os.getenv("DISCORD_TOKEN", "").strip()

    if not discord_token:
        raise RuntimeError("找不到 DISCORD_TOKEN，請檢查 .env 設定。")

    return Settings(
        discord_token=discord_token,
        ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen3.5:4b"),
        ollama_timeout_seconds=_read_positive_float("OLLAMA_TIMEOUT_SECONDS", 300.0),
        max_ask_input_length=_read_positive_int("MAX_ASK_INPUT_LENGTH", 500),
        max_ask_output_length=_read_positive_int("MAX_ASK_OUTPUT_LENGTH", 1900),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )


def configure_logging(log_level: str) -> None:
    """設定基本日誌格式；此函式不接收也不記錄 Discord Token。"""

    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def _read_positive_int(name: str, default: int) -> int:
    """讀取正整數環境變數，格式錯誤時顯示容易理解的原因。"""

    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f"{name} 必須是整數。") from error

    if value <= 0:
        raise RuntimeError(f"{name} 必須大於 0。")
    return value


def _read_positive_float(name: str, default: float) -> float:
    """讀取正浮點數環境變數，用於 Ollama 請求逾時秒數。"""

    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as error:
        raise RuntimeError(f"{name} 必須是數字。") from error

    if value <= 0:
        raise RuntimeError(f"{name} 必須大於 0。")
    return value
