"""以 MySQL 保存 Project 與 RequirementChange。"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any
from uuid import uuid4

from database.db import DatabaseError, MySQLDatabase
from models.project import Project, ProjectDataError, ProjectStatus, RequirementChange
from repositories.project_repository import ProjectRepositoryError


class MySQLProjectRepository:
    """非同步專案 Repository；所有回傳資料仍經領域模型驗證。"""

    def __init__(self, database: MySQLDatabase) -> None:
        self.database = database

    async def list_for_guild(self, guild_id: int) -> list[Project]:
        try:
            async with self.database.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT id, category, title, budget, deadline, status, owner_guild_id "
                        "FROM projects WHERE owner_guild_id IS NULL OR owner_guild_id = %s "
                        "ORDER BY id", (guild_id,),
                    )
                    return await self._build_projects(cursor, list(await cursor.fetchall()))
        except Exception as error:
            raise ProjectRepositoryError("無法讀取 Guild 專案資料。") from error

    async def get_for_guild(self, guild_id: int, project_id: str) -> Project | None:
        project = await self.get_project(project_id)
        return project if project is not None and project.owner_guild_id in (None, guild_id) else None

    async def create_custom_project(
        self, guild_id: int, *, title: str, category: str,
        requirements: tuple[str, ...], budget: int, deadline: date,
        acceptance_criteria: tuple[str, ...], change_description: str | None = None,
    ) -> Project:
        if (not isinstance(guild_id, int) or guild_id <= 0
                or not title.strip() or len(title.strip()) > 255
                or not category.strip() or len(category.strip()) > 100
                or not requirements or not acceptance_criteria
                or len(requirements) > 20 or len(acceptance_criteria) > 20
                or any(not item.strip() or len(item.strip()) > 1000
                       for item in (*requirements, *acceptance_criteria))
                or not isinstance(budget, int) or isinstance(budget, bool)
                or budget < 0 or budget > 18_446_744_073_709_551_615
                or not isinstance(deadline, date)
                or (change_description is not None
                    and (not change_description.strip() or len(change_description.strip()) > 2000))):
            raise ProjectRepositoryError("自訂專案欄位格式不正確。")
        project_id = "CUS-" + uuid4().hex[:20].upper()
        change_id = "CHG-" + uuid4().hex[:20].upper()
        try:
            async with self.database.transaction() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "INSERT INTO projects (id, category, title, budget, deadline, status, owner_guild_id) "
                        "VALUES (%s, %s, %s, %s, %s, 'pending', %s)",
                        (project_id, category, title, budget, deadline, guild_id),
                    )
                    await cursor.executemany(
                        "INSERT INTO project_requirements (project_id, position, content) "
                        "VALUES (%s, %s, %s)",
                        [(project_id, i, text) for i, text in enumerate(requirements)],
                    )
                    await cursor.executemany(
                        "INSERT INTO project_acceptance_criteria (project_id, position, content) "
                        "VALUES (%s, %s, %s)",
                        [(project_id, i, text) for i, text in enumerate(acceptance_criteria)],
                    )
                    if change_description is not None:
                        await cursor.execute(
                            "INSERT INTO requirement_changes "
                            "(id, project_id, description, reason, requested_at, status) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            (change_id, project_id, change_description,
                             "使用者於建立專案時提供的第二輪情境", date.today(), "待評估"),
                        )
            project = await self.get_for_guild(guild_id, project_id)
            if project is None:
                raise ProjectRepositoryError("自訂專案建立後無法讀取。")
            return project
        except ProjectRepositoryError:
            raise
        except Exception as error:
            raise ProjectRepositoryError("無法建立自訂專案。") from error

    async def add_requirement_change(
        self, guild_id: int, project_id: str, description: str,
    ) -> RequirementChange:
        """使用者看過第一輪草案後，才建立真正的第二輪變更。"""

        normalized_id = project_id.strip().upper()
        normalized_description = description.strip()
        if (guild_id <= 0 or not normalized_description
                or len(normalized_description) > 2000):
            raise ProjectRepositoryError("需求變更內容格式不正確。")
        change_id = "CHG-" + uuid4().hex[:20].upper()
        change = RequirementChange(
            change_id, normalized_id, normalized_description,
            "使用者檢視第一輪草案後提出", date.today(), "待評估",
        )
        try:
            async with self.database.transaction() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT project_id FROM guild_current_projects "
                        "WHERE guild_id = %s FOR UPDATE",
                        (guild_id,),
                    )
                    current = await cursor.fetchone()
                    if current is None or current["project_id"] != normalized_id:
                        raise ProjectRepositoryError("這個專案不是本伺服器目前進行中的專案。")
                    await cursor.execute(
                        "SELECT owner_guild_id, status FROM projects WHERE id = %s FOR UPDATE",
                        (normalized_id,),
                    )
                    row = await cursor.fetchone()
                    if (row is None or row["owner_guild_id"] != guild_id
                            or row["status"] != ProjectStatus.IN_PROGRESS.value):
                        raise ProjectRepositoryError("只能修改本伺服器進行中的自訂專案。")
                    await cursor.execute(
                        "SELECT COUNT(*) AS n FROM requirement_changes WHERE project_id = %s",
                        (normalized_id,),
                    )
                    if (await cursor.fetchone())["n"]:
                        raise ProjectRepositoryError("這個專案已有第二輪需求變更。")
                    await cursor.execute(
                        "INSERT INTO requirement_changes "
                        "(id, project_id, description, reason, requested_at, status) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (change.id, change.project_id, change.description,
                         change.reason, change.requested_at, change.status),
                    )
            return change
        except ProjectRepositoryError:
            raise
        except Exception as error:
            raise ProjectRepositoryError("無法保存需求變更。") from error

    async def list_projects(self) -> list[Project]:
        try:
            async with self.database.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT id, category, title, budget, deadline, status, owner_guild_id "
                        "FROM projects ORDER BY id"
                    )
                    projects = list(await cursor.fetchall())
                    return await self._build_projects(cursor, projects)
        except DatabaseError as error:
            raise ProjectRepositoryError("無法讀取 MySQL 專案資料。") from error
        except Exception as error:
            raise ProjectRepositoryError("無法讀取 MySQL 專案資料。") from error

    async def get_project(self, project_id: str) -> Project | None:
        normalized_id = project_id.strip().upper()
        try:
            async with self.database.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT id, category, title, budget, deadline, status, owner_guild_id "
                        "FROM projects WHERE id = %s",
                        (normalized_id,),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        return None
                    return (await self._build_projects(cursor, [row]))[0]
        except DatabaseError as error:
            raise ProjectRepositoryError("無法讀取 MySQL 專案資料。") from error
        except Exception as error:
            raise ProjectRepositoryError("無法讀取 MySQL 專案資料。") from error

    async def update_status(
        self,
        project_id: str,
        status: ProjectStatus,
    ) -> Project:
        normalized_id = project_id.strip().upper()
        try:
            async with self.database.transaction() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE projects SET status = %s WHERE id = %s",
                        (status.value, normalized_id),
                    )
                    if cursor.rowcount == 0:
                        raise ProjectRepositoryError(f"找不到專案 ID：{normalized_id}")
            project = await self.get_project(normalized_id)
            if project is None:
                raise ProjectRepositoryError(f"找不到專案 ID：{normalized_id}")
            return project
        except ProjectRepositoryError:
            raise
        except (DatabaseError, Exception) as error:
            raise ProjectRepositoryError("無法保存 MySQL 專案狀態。") from error

    async def upsert_project(self, project: Project) -> None:
        """供 JSON 遷移使用；專案主檔與子資料必須一同保存。"""

        try:
            async with self.database.transaction() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO projects (id, category, title, budget, deadline, status)
                        VALUES (%s, %s, %s, %s, %s, %s) AS new
                        ON DUPLICATE KEY UPDATE
                            category = new.category, title = new.title,
                            budget = new.budget, deadline = new.deadline,
                            status = new.status
                        """,
                        (
                            project.id,
                            project.category,
                            project.title,
                            project.budget,
                            project.deadline,
                            project.status.value,
                        ),
                    )
                    for table in (
                        "project_requirements",
                        "project_acceptance_criteria",
                    ):
                        await cursor.execute(
                            f"DELETE FROM {table} WHERE project_id = %s", (project.id,)
                        )
                    await cursor.executemany(
                        "INSERT INTO project_requirements (project_id, position, content) "
                        "VALUES (%s, %s, %s)",
                        [
                            (project.id, position, content)
                            for position, content in enumerate(project.requirements)
                        ],
                    )
                    await cursor.executemany(
                        "INSERT INTO project_acceptance_criteria (project_id, position, content) "
                        "VALUES (%s, %s, %s)",
                        [
                            (project.id, position, content)
                            for position, content in enumerate(project.acceptance_criteria)
                        ],
                    )
                    await cursor.execute(
                        "DELETE FROM requirement_changes WHERE project_id = %s",
                        (project.id,),
                    )
                    await cursor.executemany(
                        """
                        INSERT INTO requirement_changes
                            (id, project_id, description, reason, requested_at, status)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        [
                            (
                                change.id,
                                change.project_id,
                                change.description,
                                change.reason,
                                change.requested_at,
                                change.status,
                            )
                            for change in project.requirement_changes
                        ],
                    )
        except (DatabaseError, Exception) as error:
            raise ProjectRepositoryError("無法匯入 MySQL 專案資料。") from error

    async def claim_for_guild(
        self,
        guild_id: int,
        project_id: str,
        *,
        allow_existing: bool,
    ) -> Project:
        """原子取得 Guild 專案，避免狀態更新與 Guild 寫入只有一半成功。"""

        normalized_id = project_id.strip().upper()
        try:
            async with self.database.transaction() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT project_id FROM guild_current_projects "
                        "WHERE guild_id = %s FOR UPDATE",
                        (guild_id,),
                    )
                    guild_row = await cursor.fetchone()
                    current_id = (
                        str(guild_row["project_id"]) if guild_row is not None else None
                    )
                    if current_id == normalized_id:
                        if allow_existing:
                            pass
                        else:
                            raise ProjectRepositoryError(
                                f"專案 {normalized_id} 已經在這個伺服器進行中。"
                            )
                    elif current_id is not None:
                        raise ProjectRepositoryError(
                            f"這個伺服器已有進行中會議：{current_id}。"
                        )

                    await cursor.execute(
                        "SELECT status, owner_guild_id FROM projects WHERE id = %s FOR UPDATE",
                        (normalized_id,),
                    )
                    project_row = await cursor.fetchone()
                    if project_row is None:
                        raise ProjectRepositoryError(f"找不到專案 ID：{normalized_id}，請先使用 /projects 查看。")
                    if project_row["owner_guild_id"] not in (None, guild_id):
                        raise ProjectRepositoryError("這個專案不屬於目前伺服器。")
                    status = ProjectStatus.from_value(project_row["status"])
                    if current_id != normalized_id:
                        if not status.can_transition_to(ProjectStatus.IN_PROGRESS):
                            raise ProjectRepositoryError(
                                f"專案 {normalized_id} 目前無法啟動。"
                            )
                        await cursor.execute(
                            "UPDATE projects SET status = %s WHERE id = %s",
                            (ProjectStatus.IN_PROGRESS.value, normalized_id),
                        )
                        await cursor.execute(
                            """
                            INSERT INTO guild_current_projects (guild_id, project_id, started_at)
                            VALUES (%s, %s, UTC_TIMESTAMP(6)) AS new
                            ON DUPLICATE KEY UPDATE
                                project_id = new.project_id,
                                started_at = new.started_at
                            """,
                            (guild_id, normalized_id),
                        )
            project = await self.get_project(normalized_id)
            if project is None:
                raise ProjectRepositoryError(f"找不到專案 ID：{normalized_id}。")
            return project
        except ProjectRepositoryError:
            raise
        except (DatabaseError, Exception) as error:
            raise ProjectRepositoryError("無法保存 MySQL 專案狀態。") from error

    async def _build_projects(
        self,
        cursor: Any,
        rows: list[dict[str, object]],
    ) -> list[Project]:
        if not rows:
            return []
        identifiers = [str(row["id"]) for row in rows]
        placeholders = ", ".join(["%s"] * len(identifiers))
        await cursor.execute(
            "SELECT project_id, position, content FROM project_requirements "
            f"WHERE project_id IN ({placeholders}) ORDER BY project_id, position",
            identifiers,
        )
        requirements = _group_contents(await cursor.fetchall())
        await cursor.execute(
            "SELECT project_id, position, content FROM project_acceptance_criteria "
            f"WHERE project_id IN ({placeholders}) ORDER BY project_id, position",
            identifiers,
        )
        acceptance = _group_contents(await cursor.fetchall())
        await cursor.execute(
            """
            SELECT id, project_id, description, reason, requested_at, status
            FROM requirement_changes
            WHERE project_id IN (""" + placeholders + ") ORDER BY project_id, requested_at, id",
            identifiers,
        )
        changes: dict[str, list[dict[str, object]]] = defaultdict(list)
        for change in await cursor.fetchall():
            copied = dict(change)
            copied["requested_at"] = _date_text(copied["requested_at"])
            changes[str(copied["project_id"])].append(copied)

        projects: list[Project] = []
        for row in rows:
            data = dict(row)
            project_id = str(data["id"])
            data["deadline"] = _date_text(data["deadline"])
            data["requirements"] = requirements[project_id]
            data["acceptance_criteria"] = acceptance[project_id]
            data["requirement_changes"] = changes[project_id]
            try:
                projects.append(Project.from_dict(data))
            except ProjectDataError as error:
                raise ProjectRepositoryError("MySQL 專案資料格式不正確。") from error
        return projects


def _group_contents(rows: list[dict[str, object]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        grouped[str(row["project_id"])].append(str(row["content"]))
    return grouped


def _date_text(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
