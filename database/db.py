"""集中管理 aiomysql 連線池與資料庫交易。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiomysql


class DatabaseError(RuntimeError):
    """資料庫連線或交易發生可預期錯誤。"""


class MySQLDatabase:
    """共用連線池；Repository 不直接建立或關閉資料庫連線。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        min_size: int = 1,
        max_size: int = 5,
        connect_timeout_seconds: float = 10.0,
    ) -> None:
        if min_size <= 0 or max_size < min_size:
            raise ValueError("MySQL 連線池大小必須滿足 1 <= min_size <= max_size。")
        self._options = {
            "host": host,
            "port": port,
            "db": database,
            "user": user,
            "password": password,
            "minsize": min_size,
            "maxsize": max_size,
            "connect_timeout": connect_timeout_seconds,
            "autocommit": False,
            "charset": "utf8mb4",
            "cursorclass": aiomysql.DictCursor,
            "init_command": "SET time_zone = '+00:00'",
        }
        self._pool: aiomysql.Pool | None = None

    async def open(self) -> None:
        """建立 pool 並立即驗證連線；密碼不會包含在錯誤訊息。"""

        if self._pool is not None:
            return
        try:
            self._pool = await aiomysql.create_pool(**self._options)
            async with self.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute("SELECT 1")
                    await cursor.fetchone()
        except Exception as error:
            await self.close()
            raise DatabaseError("無法連線到 MySQL，請檢查 DB 設定與服務狀態。") from error

    async def close(self) -> None:
        """關閉 pool，等待已借出的連線安全釋放。"""

        if self._pool is None:
            return
        pool, self._pool = self._pool, None
        pool.close()
        await pool.wait_closed()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiomysql.Connection]:
        """取得一條連線；離開區塊時一定歸還 pool。"""

        if self._pool is None:
            raise DatabaseError("MySQL 連線池尚未啟動。")
        async with self._pool.acquire() as connection:
            yield connection

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiomysql.Connection]:
        """執行原子交易；任何例外都 rollback。"""

        async with self.connection() as connection:
            try:
                await connection.begin()
                yield connection
            except Exception:
                await connection.rollback()
                raise
            else:
                await connection.commit()
