"""定義專案 Repository 介面與 JSON 檔案實作。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from models.project import Project, ProjectDataError, ProjectStatus


class ProjectRepositoryError(RuntimeError):
    """讀取或解析專案資料時發生的可預期錯誤。"""


class ProjectRepository(Protocol):
    """Bot 查詢專案時依賴的最小介面。"""

    def list_projects(self) -> list[Project]:
        """取得全部專案。"""

    def get_project(self, project_id: str) -> Project | None:
        """依 ID 尋找專案；找不到時回傳 None。"""

    def update_status(self, project_id: str, status: ProjectStatus) -> Project:
        """更新並保存專案狀態。"""


class JsonProjectRepository:
    """從 projects.json 讀取專案資料。"""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)

    def list_projects(self) -> list[Project]:
        """讀取 JSON、驗證欄位，最後回傳 Project 清單。"""

        document = self._read_document()
        projects_data = self._read_projects_data(document)

        projects: list[Project] = []
        for index, project_data in enumerate(projects_data, start=1):
            if not isinstance(project_data, dict):
                raise ProjectRepositoryError(f"第 {index} 筆專案必須是物件。")
            try:
                projects.append(Project.from_dict(project_data))
            except ProjectDataError as error:
                raise ProjectRepositoryError(
                    f"第 {index} 筆專案格式錯誤：{error}"
                ) from error

        return projects

    def get_project(self, project_id: str) -> Project | None:
        """使用不分大小寫的專案 ID 尋找專案。"""

        normalized_id = project_id.strip().upper()
        return next(
            (project for project in self.list_projects() if project.id.upper() == normalized_id),
            None,
        )

    def update_status(self, project_id: str, status: ProjectStatus) -> Project:
        """更新 JSON 中的狀態，並回傳更新後的 Project。"""

        normalized_id = project_id.strip().upper()
        document = self._read_document()
        projects_data = self._read_projects_data(document)

        for project_data in projects_data:
            if not isinstance(project_data, dict):
                continue
            stored_id = project_data.get("id")
            if isinstance(stored_id, str) and stored_id.upper() == normalized_id:
                project_data["status"] = status.value
                self._write_document(document)
                try:
                    return Project.from_dict(project_data)
                except ProjectDataError as error:
                    raise ProjectRepositoryError(
                        f"更新後的專案格式錯誤：{error}"
                    ) from error

        raise ProjectRepositoryError(f"找不到專案 ID：{normalized_id}")

    def _read_document(self) -> dict[str, object]:
        """讀取並驗證 projects.json 最外層結構。"""

        try:
            raw_text = self.file_path.read_text(encoding="utf-8")
            document = json.loads(raw_text)
        except FileNotFoundError as error:
            raise ProjectRepositoryError(
                f"找不到專案資料檔：{self.file_path}"
            ) from error
        except json.JSONDecodeError as error:
            raise ProjectRepositoryError("projects.json 不是有效的 JSON。") from error
        except OSError as error:
            raise ProjectRepositoryError("無法讀取 projects.json。") from error

        if not isinstance(document, dict):
            raise ProjectRepositoryError("projects.json 最外層必須是物件。")
        return document

    @staticmethod
    def _read_projects_data(document: dict[str, object]) -> list[object]:
        """取得最外層 projects 陣列。"""

        projects_data = document.get("projects")
        if not isinstance(projects_data, list):
            raise ProjectRepositoryError("projects 必須是陣列。")
        return projects_data

    def _write_document(self, document: dict[str, object]) -> None:
        """以 UTF-8 寫回 JSON，保留容易閱讀的縮排。"""

        try:
            self.file_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as error:
            raise ProjectRepositoryError("無法保存 projects.json。") from error
