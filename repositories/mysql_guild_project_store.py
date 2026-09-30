"""以 MySQL 保存 Discord Guild 的目前專案。"""

from __future__ import annotations

from datetime import datetime, timezone

from database.db import DatabaseError, MySQLDatabase
from repositories.guild_project_store import GuildProjectStoreError


class MySQLGuildProjectStore:
    """非同步 Guild 狀態儲存；同一 Guild 僅保留一筆目前專案。"""

    def __init__(self, database: MySQLDatabase) -> None:
        self.database = database

    async def get_current_project_id(self, guild_id: int) -> str | None:
        try:
            async with self.database.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT project_id FROM guild_current_projects WHERE guild_id = %s",
                        (guild_id,),
                    )
                    row = await cursor.fetchone()
                    return None if row is None else str(row["project_id"])
        except (DatabaseError, Exception) as error:
            raise GuildProjectStoreError("無法讀取 MySQL Guild 專案狀態。") from error

    async def set_current_project(self, guild_id: int, project_id: str) -> None:
        try:
            async with self.database.transaction() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO guild_current_projects (guild_id, project_id, started_at)
                        VALUES (%s, %s, %s) AS new
                        ON DUPLICATE KEY UPDATE
                            project_id = new.project_id,
                            started_at = new.started_at
                        """,
                        (guild_id, project_id, datetime.now(timezone.utc)),
                    )
        except (DatabaseError, Exception) as error:
            raise GuildProjectStoreError("無法保存 MySQL Guild 專案狀態。") from error
