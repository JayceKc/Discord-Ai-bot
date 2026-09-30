"""MySQL persistence and read-only experience memory for a Discord guild."""

from __future__ import annotations

import json
from typing import Any

from database.db import MySQLDatabase
from models.workspace import ProjectPriority, Workspace


class WorkspaceRepositoryError(RuntimeError):
    """A workspace query or update failed."""


class MySQLWorkspaceRepository:
    def __init__(self, database: MySQLDatabase) -> None:
        self.database = database

    async def get_or_create(self, guild_id: int, guild_name: str) -> Workspace:
        try:
            async with self.database.transaction() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "INSERT IGNORE INTO workspaces (guild_id, name, default_priority) "
                        "VALUES (%s, %s, %s)",
                        (guild_id, guild_name[:100] or "Discord 工作區", ProjectPriority.GROWTH.value),
                    )
                    if not guild_name.startswith("Guild "):
                        await cursor.execute(
                            "UPDATE workspaces SET name = %s WHERE guild_id = %s "
                            "AND name = %s",
                            (guild_name[:100] or "Discord 工作區", guild_id, f"Guild {guild_id}"),
                        )
            return await self.get(guild_id)
        except Exception as error:
            if isinstance(error, WorkspaceRepositoryError):
                raise
            raise WorkspaceRepositoryError("無法建立工作區。") from error

    async def get(self, guild_id: int) -> Workspace:
        try:
            async with self.database.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT name, default_priority FROM workspaces WHERE guild_id = %s",
                        (guild_id,),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        raise WorkspaceRepositoryError("找不到工作區。")
                    await cursor.execute(
                        "SELECT project_id FROM guild_current_projects WHERE guild_id = %s",
                        (guild_id,),
                    )
                    current = await cursor.fetchone()
                    await cursor.execute(
                        "SELECT COUNT(*) AS n FROM projects WHERE owner_guild_id = %s "
                        "AND status = 'completed'",
                        (guild_id,),
                    )
                    completed = await cursor.fetchone()
                    await cursor.execute(
                        "SELECT COUNT(DISTINCT project_id) AS n FROM meetings "
                        "WHERE guild_id = %s AND final_proposal IS NOT NULL",
                        (guild_id,),
                    )
                    finalized = await cursor.fetchone()
                    return Workspace(
                        guild_id=guild_id,
                        name=str(row["name"]),
                        default_priority=ProjectPriority(row["default_priority"]),
                        completed_custom_projects=int(completed["n"]),
                        projects_with_final_proposal=int(finalized["n"]),
                        current_project_id=(str(current["project_id"]) if current else None),
                    )
        except WorkspaceRepositoryError:
            raise
        except Exception as error:
            raise WorkspaceRepositoryError("無法讀取工作區。") from error

    async def set_default_priority(self, guild_id: int, priority: ProjectPriority) -> Workspace:
        try:
            async with self.database.transaction() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE workspaces SET default_priority = %s WHERE guild_id = %s",
                        (priority.value, guild_id),
                    )
                    if cursor.rowcount == 0:
                        raise WorkspaceRepositoryError("找不到工作區。")
            return await self.get(guild_id)
        except WorkspaceRepositoryError:
            raise
        except Exception as error:
            raise WorkspaceRepositoryError("無法更新優先目標。") from error

    async def recent_experiences(self, guild_id: int, project_id: str, *, exclude_meeting_id: str | None = None) -> list[dict[str, str]]:
        try:
            async with self.database.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT m.id, m.project_id, m.final_proposal FROM meetings m "
                        "JOIN projects p ON p.id = m.project_id "
                        "WHERE m.guild_id = %s "
                        "AND p.category = (SELECT category FROM projects WHERE id = %s) "
                        "AND m.final_proposal IS NOT NULL AND (%s IS NULL OR m.id <> %s) "
                        "ORDER BY m.updated_at DESC, m.id DESC LIMIT 3",
                        (guild_id, project_id, exclude_meeting_id, exclude_meeting_id),
                    )
                    result: list[dict[str, str]] = []
                    for row in await cursor.fetchall():
                        proposal: Any = row["final_proposal"]
                        if isinstance(proposal, str):
                            proposal = json.loads(proposal)
                        if not isinstance(proposal, dict):
                            continue
                        decisions = proposal.get("decisions", [])
                        tradeoffs = "；".join(
                            f"{item.get('topic', '')}：{item.get('decision', '')}（{item.get('reason', '')}）"
                            for item in decisions[:3] if isinstance(item, dict)
                        ) if isinstance(decisions, list) else ""
                        result.append({
                            "meeting_id": str(row["id"]),
                            "project_id": str(row["project_id"]),
                            "title": str(proposal.get("title", ""))[:80],
                            "summary": str(proposal.get("summary", ""))[:180],
                            "tradeoffs": tradeoffs[:150],
                        })
                    return result
        except Exception as error:
            raise WorkspaceRepositoryError("無法讀取近期專案經驗。") from error
