"""集中讀取與驗證 Discord Bot 的環境設定。"""

from __future__ import annotations  # 延後解析型別註記，提升不同 Python 版本的相容性。

import logging  # Python 內建日誌模組，用來設定輸出格式與等級。
import os  # 用來讀取作業系統環境變數。
from dataclasses import dataclass  # 讓設定類別可以自動產生初始化方法。

from dotenv import load_dotenv  # 將 .env 檔案內容載入環境變數。


@dataclass(frozen=True)  # frozen=True 代表 Settings 建立後不能再修改欄位。
class Settings:
    """程式啟動後使用的設定；凍結後可避免執行途中被意外修改。"""

    discord_token: str  # Discord Bot 登入憑證，沒有預設值，啟動時一定要提供。
    ollama_host: str = "http://localhost:11434"  # 本機 Ollama 預設服務網址。
    ollama_model: str = "qwen3.5:4b"  # !ask 預設使用的模型名稱。
    ollama_timeout_seconds: float = 300.0  # 等待模型回答的最長秒數。
    max_ask_input_length: int = 500  # 使用者問題的最大字元數。
    max_ask_output_length: int = 1900  # 傳回 Discord 的最大字元數。
    projects_file: str = "projects.json"  # JsonProjectRepository 讀取的資料檔路徑。
    guild_projects_file: str = "guild_projects.json"  # 每個 Guild 的目前專案。
    log_level: str = "INFO"  # 預設只顯示 INFO 以上等級的日誌。


def load_settings() -> Settings:
    """從 .env 與環境變數建立設定，缺少必要資料時立即停止。"""

    load_dotenv()  # 將專案根目錄的 .env 載入環境變數。

    # 找不到變數時先取得空字串；strip() 可移除不小心輸入的前後空白。
    discord_token = os.getenv("DISCORD_TOKEN", "").strip()

    if not discord_token:  # 空字串代表使用者尚未設定 Discord Token。
        raise RuntimeError("找不到 DISCORD_TOKEN，請檢查 .env 設定。")  # 阻止 Bot 用空 Token 啟動。

    # 建立一個 Settings 物件；環境變數不存在時使用每個欄位的預設值。
    return Settings(
        discord_token=discord_token,  # Token 只保存在設定物件中，不寫進日誌。
        ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/"),  # 移除網址結尾的 /。
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen3.5:4b"),  # 允許透過 .env 更換模型。
        ollama_timeout_seconds=_read_positive_float("OLLAMA_TIMEOUT_SECONDS", 300.0),
        max_ask_input_length=_read_positive_int("MAX_ASK_INPUT_LENGTH", 500),
        max_ask_output_length=_read_positive_int("MAX_ASK_OUTPUT_LENGTH", 1900),
        projects_file=os.getenv("PROJECTS_FILE", "projects.json"),
        guild_projects_file=os.getenv("GUILD_PROJECTS_FILE", "guild_projects.json"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),  # 統一轉成大寫，例如 info 變成 INFO。
    )


def configure_logging(log_level: str) -> None:
    """設定基本日誌格式；此函式不接收也不記錄 Discord Token。"""

    # 從 logging 找到同名等級；名稱錯誤時安全地退回 INFO。
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,  # 決定最低要顯示哪個等級的訊息。
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",  # 時間｜等級｜模組｜內容。
    )


def _read_positive_int(name: str, default: int) -> int:
    """讀取正整數環境變數，格式錯誤時顯示容易理解的原因。"""

    raw_value = os.getenv(name, str(default))  # 環境變數都是字串，沒有設定就用預設值。
    try:
        value = int(raw_value)  # 嘗試把字串轉成整數。
    except ValueError as error:
        raise RuntimeError(f"{name} 必須是整數。") from error  # 保留原始錯誤供除錯使用。

    if value <= 0:  # 字數上限不接受 0 或負數。
        raise RuntimeError(f"{name} 必須大於 0。")
    return value  # 驗證成功後回傳轉換完成的整數。


def _read_positive_float(name: str, default: float) -> float:
    """讀取正浮點數環境變數，用於 Ollama 請求逾時秒數。"""

    raw_value = os.getenv(name, str(default))  # 先取得文字形式的秒數。
    try:
        value = float(raw_value)  # 支援 30 或 30.5 這類秒數。
    except ValueError as error:
        raise RuntimeError(f"{name} 必須是數字。") from error

    if value <= 0:  # 逾時秒數必須是正數。
        raise RuntimeError(f"{name} 必須大於 0。")
    return value  # 驗證成功後回傳浮點數。
