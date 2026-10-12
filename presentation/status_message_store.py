"""只保存狀態卡 ID；原子替換檔案，不保存訊息內容或憑證。"""
import json
import os
from pathlib import Path
import tempfile


class StatusMessageStore:
    def __init__(self, path):
        self.path = Path(path)
        try:
            loaded = json.loads(self.path.read_text())
            self.ids = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            self.ids = {}

    @staticmethod
    def encode(key):
        return ":".join(map(str, key))

    def get(self, key):
        value = self.ids.get(self.encode(key))
        return value if type(value) is int and value > 0 else None

    def put(self, key, message_id):
        self.ids[self.encode(key)] = message_id
        self._save()

    def metric_signature(self, guild_id, meeting_id):
        value = self.ids.get(f"metrics:{guild_id}:{meeting_id}")
        return value if isinstance(value, str) else None

    def remember_metrics(self, guild_id, meeting_id, signature):
        self.ids[f"metrics:{guild_id}:{meeting_id}"] = signature
        self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 同一個 Bot 行程的同步寫入不跨 await；只允許單一正式 Bot 行程。
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix="status-")
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(self.ids, handle)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
