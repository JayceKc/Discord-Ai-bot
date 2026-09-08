"""保存每個 Discord Guild 目前正在進行的專案。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


class GuildProjectStoreError(RuntimeError):
    """讀取或保存 Guild 專案狀態時發生錯誤。"""


class GuildProjectStore(Protocol):
    """ProjectMeetingService 需要的 Guild 狀態儲存介面。"""

    def get_current_project_id(self, guild_id: int) -> str | None:
        """取得 Guild 目前的專案 ID。"""

    def set_current_project(self, guild_id: int, project_id: str) -> None:
        """保存 Guild 目前的專案 ID 與啟動時間。"""


class JsonGuildProjectStore:
    """使用 JSON 檔案持久保存每個 Guild 的目前專案。"""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)

    def get_current_project_id(self, guild_id: int) -> str | None:
        """Guild 沒有進行中專案時回傳 None。"""

        document = self._read_document()
        guild_data = document["guilds"].get(str(guild_id))
        if guild_data is None:
            return None
        if not isinstance(guild_data, dict):
            raise GuildProjectStoreError("Guild 專案資料必須是物件。")

        project_id = guild_data.get("project_id")
        if not isinstance(project_id, str) or not project_id.strip():
            raise GuildProjectStoreError("Guild 的 project_id 必須是非空字串。")
        return project_id.strip()

    def set_current_project(self, guild_id: int, project_id: str) -> None:
        """寫入目前專案；同一 Guild 永遠只保留一筆。"""

        document = self._read_document()
        document["guilds"][str(guild_id)] = {
            "project_id": project_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_document(document)

    def _read_document(self) -> dict[str, dict[str, object]]:
        """讀取 JSON；檔案尚不存在時建立空白結構。"""

        if not self.file_path.exists():
            return {"guilds": {}}

        try:
            document = json.loads(self.file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise GuildProjectStoreError(
                "guild_projects.json 不是有效的 JSON。"
            ) from error
        except OSError as error:
            raise GuildProjectStoreError("無法讀取 guild_projects.json。") from error

        if not isinstance(document, dict) or not isinstance(document.get("guilds"), dict):
            raise GuildProjectStoreError("guild_projects.json 必須包含 guilds 物件。")
        return document

    def _write_document(self, document: dict[str, dict[str, object]]) -> None:
        """將 Guild 狀態保存到 JSON。"""

        try:
            self.file_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as error:
            raise GuildProjectStoreError("無法保存 guild_projects.json。") from error
