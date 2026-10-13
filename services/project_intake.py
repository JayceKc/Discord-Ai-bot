"""單一 Discord 頻道的自然語言專案收集狀態。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date
import json

from models.project import RequirementChange


FIELDS = (
    "title", "category", "requirements", "budget", "deadline",
    "acceptance_criteria",
)
QUESTIONS = {
    "title": "請給這個專案一個名稱（例如：品牌官網）。",
    "category": "專案類型是什麼？（例如：網站）",
    "requirements": "請列出需求，每行一項。",
    "budget": "預算是多少新臺幣？請輸入整數；沒有預算可輸入 0。",
    "deadline": "期限是哪天？請輸入 YYYY-MM-DD。",
    "acceptance_criteria": "請列出驗收條件，每行一項。",
}
START_PATTERN = re.compile(r"^(?:我想做|我要做|我想建立|我要建立|幫我做|幫我建立)(?:一個|一份|個)?\s*(.+)$")
MAX_BUDGET = 18_446_744_073_709_551_615


@dataclass
class IntakeSession:
    user_id: int
    guild_id: int
    channel_id: int
    fields: dict[str, object] = field(default_factory=dict)
    awaiting_confirmation: bool = False
    running: bool = False
    stage: str = "collecting"
    project_id: str | None = None
    submission_project_id: str | None = None
    submission_change_id: str | None = None
    change_id: str | None = None
    change: RequirementChange | None = None
    updated_at: float = field(default_factory=time.monotonic)

    def missing_field(self) -> str | None:
        return next((name for name in FIELDS if name not in self.fields), None)


class ProjectIntake:
    """只保存尚未提交的草稿；重啟會清除，正式專案仍由 Repository 保存。"""

    def __init__(self, *, ttl_seconds: float = 1800, max_prompt_characters: int = 6000) -> None:
        self.sessions: dict[tuple[int, int, int], IntakeSession] = {}
        self.ttl_seconds = ttl_seconds
        # 保留角色上下文／近期經驗空間，不能把整個模型預算都用於原始需求。
        self.requirement_budget = max_prompt_characters - min(2400, max_prompt_characters // 2)

    def get(self, guild_id: int, channel_id: int, user_id: int) -> IntakeSession | None:
        key = (guild_id, channel_id, user_id)
        session = self.sessions.get(key)
        if session and time.monotonic() - session.updated_at > self.ttl_seconds and not session.running and session.stage not in {"write_outcome_unknown", "change_outcome_unknown"}:
            self.sessions.pop(key, None)
            return None
        return session

    def start(self, guild_id: int, channel_id: int, user_id: int, content: str) -> IntakeSession | None:
        match = START_PATTERN.fullmatch(content.strip())
        if match is None:
            return None
        # 保留原句作為名稱草稿；不把簡短願望偽裝成完整需求。
        title = match.group(1).strip().rstrip("。！! ")
        return self.start_from_title(guild_id, channel_id, user_id, title)

    def start_from_title(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        title: str,
        *,
        stage: str = "collecting",
    ) -> IntakeSession:
        """以已驗證的標題建立草稿，供規則與模型判斷共用。"""

        title = title.strip().rstrip("。！! ")
        if not title or len(title) > 255:
            raise ValueError("專案名稱必須介於 1–255 字。")
        session = IntakeSession(user_id, guild_id, channel_id, stage=stage)
        session.fields["title"] = title
        self.sessions[(guild_id, channel_id, user_id)] = session
        return session

    def cancel(self, session: IntakeSession) -> None:
        self.sessions.pop((session.guild_id, session.channel_id, session.user_id), None)

    def accept(self, session: IntakeSession, content: str) -> str | None:
        """接收目前欄位；傳回錯誤文字，成功則傳回 None。"""

        field_name = session.missing_field()
        if field_name is None:
            return "所有欄位已填好，請回覆「確認」或「取消」。"
        value = content.strip()
        if not value:
            return "內容不能留空。"
        if field_name in ("title", "category"):
            maximum = {"title": 255, "category": 100}[field_name]
            if len(value) > maximum:
                return f"文字過長，最多 {maximum} 字。"
            session.fields[field_name] = value
        elif field_name in ("requirements", "acceptance_criteria"):
            items = tuple(line.strip() for line in value.splitlines() if line.strip())
            if not items or len(items) > 20 or any(len(item) > 1000 for item in items):
                return "請提供 1–20 行，每行最多 1000 字。"
            session.fields[field_name] = items
        elif field_name == "budget":
            if not re.fullmatch(r"\d+", value) or int(value) > MAX_BUDGET:
                return "預算請輸入 0 或正整數（新臺幣）。"
            session.fields[field_name] = int(value)
        elif field_name == "deadline":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return "日期請使用 YYYY-MM-DD。"
            try:
                session.fields[field_name] = date.fromisoformat(value)
            except ValueError:
                return "日期不存在，請使用有效的 YYYY-MM-DD。"
        session.updated_at = time.monotonic()
        if session.missing_field() is None:
            error = self.validate_prompt(session)
            if error:
                # 留在目前欄位，讓使用者可直接回覆較短的內容。
                session.fields.pop(field_name)
                session.awaiting_confirmation = False
                return error
        session.awaiting_confirmation = session.missing_field() is None
        return None

    def validate_prompt(self, session: IntakeSession) -> str | None:
        encoded = json.dumps({"requirement": format_requirement(session.fields)}, ensure_ascii=False)
        if len(encoded) > self.requirement_budget:
            return f"需求與驗收合併過長，含預算／期限需在 {self.requirement_budget} 字元內；請縮短本欄位，或用「修改需求：內容」「修改驗收：內容」調整。"
        return None

    def update_text_field(self, session, name, text):
        items = tuple(line.strip() for line in text.splitlines() if line.strip())
        if not items or len(items) > 20 or any(len(item) > 1000 for item in items):
            return "請提供 1–20 行，每行最多 1000 字。"
        previous = session.fields.get(name)
        session.fields[name] = items
        session.awaiting_confirmation = session.missing_field() is None
        if session.awaiting_confirmation:
            error = self.validate_prompt(session)
            if error:
                # 拒絕的修改不污染已驗證草稿，避免欄位齊全卻不能確認。
                if previous is None:
                    session.fields.pop(name)
                else:
                    session.fields[name] = previous
                session.awaiting_confirmation = session.missing_field() is None and self.validate_prompt(session) is None
                return error
        return None

    @staticmethod
    def summary(session: IntakeSession) -> str:
        fields = session.fields
        return (
            f"名稱：{fields['title']}\n類型：{fields['category']}\n"
            f"需求：{'；'.join(fields['requirements'])}\n"
            f"預算：NT$ {fields['budget']:,}\n期限：{fields['deadline'].isoformat()}\n"
            f"驗收：{'；'.join(fields['acceptance_criteria'])}"
        )


def format_requirement(fields) -> str:
    """入口檢查與實際模型輸入使用同一格式。"""
    return "\n".join((*fields['requirements'],
        f"硬性預算上限：{fields['budget']}", f"硬性交付期限：{fields['deadline'].isoformat()}",
        "驗收條件：" + "；".join(fields['acceptance_criteria'])))
