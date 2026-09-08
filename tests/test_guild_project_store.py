"""測試每個 Discord Guild 的目前專案保存功能。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from repositories.guild_project_store import JsonGuildProjectStore


class JsonGuildProjectStoreTest(unittest.TestCase):
    """確認不同 Guild 可保存各自唯一的目前專案。"""

    def test_saves_and_reloads_current_project_for_each_guild(self) -> None:
        with TemporaryDirectory() as directory:
            file_path = Path(directory) / "guild_projects.json"
            store = JsonGuildProjectStore(file_path)

            store.set_current_project(111, "PRJ-001")
            store.set_current_project(222, "PRJ-002")

            reloaded_store = JsonGuildProjectStore(file_path)
            self.assertEqual(reloaded_store.get_current_project_id(111), "PRJ-001")
            self.assertEqual(reloaded_store.get_current_project_id(222), "PRJ-002")
            self.assertIsNone(reloaded_store.get_current_project_id(333))

    def test_same_guild_keeps_only_latest_project(self) -> None:
        with TemporaryDirectory() as directory:
            file_path = Path(directory) / "guild_projects.json"
            store = JsonGuildProjectStore(file_path)

            store.set_current_project(111, "PRJ-001")
            store.set_current_project(111, "PRJ-002")

            self.assertEqual(store.get_current_project_id(111), "PRJ-002")


if __name__ == "__main__":
    unittest.main()
