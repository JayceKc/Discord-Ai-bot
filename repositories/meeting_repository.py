"""使用 Repository 介面與 JSON 檔案保存 Agent 會議。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from models.meeting import MeetingDataError, MeetingRecord


class MeetingRepositoryError(RuntimeError):
    """讀取或保存會議紀錄時發生錯誤。"""


class MeetingRepository(Protocol):
    """MeetingManager 使用的會議資料存取介面。"""

    def save(self, record: MeetingRecord) -> None:
        """建立或覆寫一筆完整會議紀錄。"""

    def get(self, meeting_id: str) -> MeetingRecord | None:
        """依會議 ID 取得紀錄。"""

    def get_for_guild(self, guild_id: int) -> MeetingRecord | None:
        """取得 Guild 目前或最近一場會議。"""


class JsonMeetingRepository:
    """將 Guild 索引與完整會議紀錄持久保存為 JSON。"""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)

    def save(self, record: MeetingRecord) -> None:
        """保存會議，並讓 Guild 指向這一場最新會議。"""

        document = self._read_document()
        document["meetings"][record.meeting_id] = record.to_dict()
        document["guilds"][str(record.guild_id)] = record.meeting_id
        self._write_document(document)

    def get(self, meeting_id: str) -> MeetingRecord | None:
        """找不到指定 ID 時回傳 None。"""

        raw_record = self._read_document()["meetings"].get(meeting_id)
        return self._convert_record(raw_record)

    def get_for_guild(self, guild_id: int) -> MeetingRecord | None:
        """Guild 尚未開過會議時回傳 None。"""

        document = self._read_document()
        meeting_id = document["guilds"].get(str(guild_id))
        if meeting_id is None:
            return None
        if not isinstance(meeting_id, str) or not meeting_id:
            raise MeetingRepositoryError("Guild 的 meeting_id 必須是非空字串。")

        raw_record = document["meetings"].get(meeting_id)
        if raw_record is None:
            raise MeetingRepositoryError("Guild 指向的會議紀錄不存在。")
        return self._convert_record(raw_record)

    def _read_document(self) -> dict[str, dict[str, object]]:
        """讀取文件；檔案不存在時回傳空白資料結構。"""

        if not self.file_path.exists():
            return {"guilds": {}, "meetings": {}}

        try:
            document = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MeetingRepositoryError("無法讀取 meetings.json。") from error

        if not isinstance(document, dict):
            raise MeetingRepositoryError("meetings.json 必須是 JSON 物件。")
        guilds = document.get("guilds")
        meetings = document.get("meetings")
        if not isinstance(guilds, dict) or not isinstance(meetings, dict):
            raise MeetingRepositoryError(
                "meetings.json 缺少 guilds 或 meetings 物件。"
            )
        return {"guilds": guilds, "meetings": meetings}

    def _write_document(self, document: dict[str, dict[str, object]]) -> None:
        """先寫入暫存檔再取代正式檔，降低半寫入檔案的風險。"""

        temporary_path = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self.file_path)
        except OSError as error:
            raise MeetingRepositoryError("無法保存 meetings.json。") from error

    @staticmethod
    def _convert_record(raw_record: object) -> MeetingRecord | None:
        """驗證 JSON 物件並轉換為 MeetingRecord。"""

        if raw_record is None:
            return None
        if not isinstance(raw_record, dict):
            raise MeetingRepositoryError("會議紀錄必須是 JSON 物件。")
        try:
            return MeetingRecord.from_dict(raw_record)
        except (MeetingDataError, ValueError) as error:
            raise MeetingRepositoryError("會議紀錄格式不正確。") from error
