"""將開發期 JSON 資料匯入 MySQL；預設只顯示 dry-run 統計。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# 允許從專案根目錄直接執行 `python scripts/migrate_json_to_mysql.py`。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database.db import MySQLDatabase
from models.meeting import MeetingRecord
from repositories.mysql_guild_project_store import MySQLGuildProjectStore
from repositories.mysql_meeting_repository import MySQLMeetingRepository
from repositories.mysql_project_repository import MySQLProjectRepository
from repositories.project_repository import JsonProjectRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="實際寫入 MySQL")
    mode.add_argument("--dry-run", action="store_true", help="只顯示匯入統計（預設）")
    parser.add_argument("--projects-file", default="projects.json")
    parser.add_argument("--guild-projects-file", default="guild_projects.json")
    parser.add_argument("--meetings-file", default="meetings.json")
    return parser.parse_args()


def load_records(path: Path) -> list[MeetingRecord]:
    if not path.exists():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    meetings = document.get("meetings", {})
    if not isinstance(meetings, dict):
        raise ValueError("meetings.json 缺少 meetings 物件。")
    return [MeetingRecord.from_dict(item) for item in meetings.values() if isinstance(item, dict)]


def load_guilds(path: Path) -> list[tuple[int, str]]:
    if not path.exists():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    raw_guilds = document.get("guilds", {})
    if not isinstance(raw_guilds, dict):
        raise ValueError("guild_projects.json 缺少 guilds 物件。")
    return [
        (int(guild_id), value["project_id"].strip().upper())
        for guild_id, value in raw_guilds.items()
        if isinstance(value, dict) and isinstance(value.get("project_id"), str)
    ]


async def run(args: argparse.Namespace) -> None:
    projects = JsonProjectRepository(args.projects_file).list_projects()
    guilds = load_guilds(Path(args.guild_projects_file))
    meetings = load_records(Path(args.meetings_file))
    print(f"dry-run：projects={len(projects)} guilds={len(guilds)} meetings={len(meetings)}")
    if not args.apply:
        return

    load_dotenv()
    password = os.getenv("DB_PASSWORD", "")
    if not password:
        raise RuntimeError("--apply 需要 DB_PASSWORD。")
    database = MySQLDatabase(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3307")),
        database=os.getenv("DB_NAME", "ai_company"),
        user=os.getenv("DB_USER", "ai_company_app"),
        password=password,
        min_size=1,
        max_size=1,
    )
    await database.open()
    try:
        project_repository = MySQLProjectRepository(database)
        guild_store = MySQLGuildProjectStore(database)
        meeting_repository = MySQLMeetingRepository(database)
        for project in projects:
            await project_repository.upsert_project(project)
        for guild_id, project_id in guilds:
            await guild_store.set_current_project(guild_id, project_id)
        for record in meetings:
            await meeting_repository.save(record)
    finally:
        await database.close()
    print("已完成 MySQL 匯入；可重複執行，不會產生重複 ID。")


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
