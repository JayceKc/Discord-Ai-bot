"""測試 projects.json 與 JsonProjectRepository。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from models.project import ProjectStatus
from repositories.project_repository import (
    JsonProjectRepository,
    ProjectRepositoryError,
)


PROJECTS_FILE = Path(__file__).resolve().parents[1] / "projects.json"


class JsonProjectRepositoryTest(unittest.TestCase):
    """確認 JSON 資料會被驗證並轉成 Project。"""

    def test_loads_at_least_five_example_projects(self) -> None:
        repository = JsonProjectRepository(PROJECTS_FILE)

        projects = repository.list_projects()

        self.assertGreaterEqual(len(projects), 5)
        self.assertEqual(projects[0].id, "PRJ-001")
        self.assertTrue(projects[0].requirements)
        self.assertTrue(projects[0].acceptance_criteria)
        # 正式資料可能因 /start 或未來的 /end 改變，不假設一定是 pending。
        self.assertIsInstance(projects[0].status, ProjectStatus)
        self.assertEqual(projects[0].requirement_changes[0].project_id, "PRJ-001")

    def test_updates_project_status_in_json(self) -> None:
        """狀態更新後，重新建立 Repository 仍可讀到新狀態。"""

        with TemporaryDirectory() as directory:
            temporary_file = Path(directory) / "projects.json"
            temporary_file.write_text(
                PROJECTS_FILE.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            repository = JsonProjectRepository(temporary_file)

            updated = repository.update_status(
                "PRJ-001",
                ProjectStatus.IN_PROGRESS,
            )

            self.assertEqual(updated.status, ProjectStatus.IN_PROGRESS)
            reloaded = JsonProjectRepository(temporary_file).get_project("PRJ-001")
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.status, ProjectStatus.IN_PROGRESS)

    def test_missing_json_file_raises_repository_error(self) -> None:
        repository = JsonProjectRepository("not-found-projects.json")

        with self.assertRaisesRegex(ProjectRepositoryError, "找不到專案資料檔"):
            repository.list_projects()


if __name__ == "__main__":
    unittest.main()
