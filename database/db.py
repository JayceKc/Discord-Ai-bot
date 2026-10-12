"""Bounded pool I/O and transactions; writes are never replayed."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, TypeVar

import aiomysql
from core.errors import AppError, ErrorCode, ErrorContext, classify_error
from core.logging_security import log_error

T = TypeVar('T')


class DatabaseError(AppError):
    default_code = ErrorCode.DB_UNAVAILABLE

    def __init__(self, *args, write_outcome='not_started', **kwargs):
        super().__init__(*args, write_outcome=write_outcome, **kwargs)


class MySQLDatabase:
    def __init__(self, *, host, port, database, user, password, min_size=1,
                 max_size=5, connect_timeout_seconds=10.0,
                 acquire_timeout_seconds=10.0, operation_timeout_seconds=30.0,
                 pool_recycle_seconds=300.0):
        if min_size <= 0 or max_size < min_size:
            raise ValueError('MySQL 連線池大小必須滿足 1 <= min_size <= max_size。')
        if any(value <= 0 for value in (connect_timeout_seconds, acquire_timeout_seconds,
                                       operation_timeout_seconds, pool_recycle_seconds)):
            raise ValueError('資料庫期限必須大於 0。')
        self._options = dict(host=host, port=port, db=database, user=user, password=password,
            minsize=min_size, maxsize=max_size, connect_timeout=connect_timeout_seconds,
            pool_recycle=pool_recycle_seconds, autocommit=False, charset='utf8mb4',
            cursorclass=aiomysql.DictCursor, init_command="SET time_zone = '+00:00'")
        self.acquire_timeout = acquire_timeout_seconds
        self.operation_timeout = operation_timeout_seconds
        self._pool = None
        self._init_lock = asyncio.Lock()
        self._retry_after = 0.0
        self._permanent_error = None
        self._closing = False

    async def run(self, operation: Awaitable[T]) -> T:
        return await asyncio.wait_for(operation, timeout=self.operation_timeout)

    def _failure(self, error, *, write_outcome='not_started', commit=False):
        if commit:
            return DatabaseError(code=ErrorCode.DB_COMMIT_UNKNOWN, write_outcome='unknown')
        code = classify_error(error, ErrorContext(write_outcome=write_outcome)).code
        if isinstance(error, asyncio.TimeoutError):
            code = ErrorCode.DB_TIMEOUT
        elif not code.value.startswith('DB_'):
            code = ErrorCode.DB_TRANSACTION_FAILED if write_outcome == 'rolled_back' else ErrorCode.DB_UNAVAILABLE
        return DatabaseError(code=code, write_outcome=write_outcome)

    async def ensure_ready(self):
        if self._closing:
            raise DatabaseError()
        if self._pool is not None and not self._pool.closed:
            return
        async with self._init_lock:
            if self._closing:
                raise DatabaseError()
            if self._pool is not None and not self._pool.closed:
                return
            if self._permanent_error is not None:
                raise DatabaseError(code=self._permanent_error)
            if time.monotonic() < self._retry_after:
                raise DatabaseError()
            pool = None
            try:
                pool = await self.run(aiomysql.create_pool(**self._options))
                connection = await asyncio.wait_for(pool.acquire(), self.acquire_timeout)
                try:
                    await self.run(connection.ping(reconnect=False))
                    async with connection.cursor() as cursor:
                        await self.run(cursor.execute('SELECT 1'))
                        await self.run(cursor.fetchone())
                    await self.run(connection.rollback())
                except BaseException:
                    connection.close()
                    raise
                finally:
                    await pool.release(connection)
                self._pool = pool
            except BaseException as error:
                if pool is not None:
                    pool.terminate()
                    try:
                        await self.run(pool.wait_closed())
                    except Exception as cleanup_error:
                        context = ErrorContext(write_outcome='not_started')
                        log_error(classify_error(cleanup_error, context), context, cleanup_error)
                if not isinstance(error, Exception):
                    raise
                failure = self._failure(error)
                self._retry_after = time.monotonic() + 5
                if failure.code == ErrorCode.DB_BAD_CONFIG:
                    self._permanent_error = failure.code
                raise failure from error

    async def open(self):
        await self.ensure_ready()

    async def close(self):
        self._closing = True
        async with self._init_lock:
            pool, self._pool = self._pool, None
            if pool is None:
                return
            pool.close()
            try:
                await self.run(pool.wait_closed())
            except asyncio.TimeoutError:
                pool.terminate()
                await self.run(pool.wait_closed())

    @asynccontextmanager
    async def _borrow(self):
        await self.ensure_ready()
        pool = self._pool
        connection = None
        try:
            connection = await asyncio.wait_for(pool.acquire(), self.acquire_timeout)
            await self.run(connection.ping(reconnect=False))
        except Exception as error:
            if connection is not None:
                connection.close()
                await pool.release(connection)
            raise self._failure(error) from error
        except BaseException:
            if connection is not None:
                connection.close()
                await pool.release(connection)
            raise
        try:
            yield connection
        finally:
            await pool.release(connection)

    async def _rollback(self, connection, *, write_outcome="unknown"):
        try:
            await self.run(connection.rollback())
            return True
        except Exception as error:
            connection.close()
            report = self._failure(error, write_outcome=write_outcome)
            context = ErrorContext(write_outcome=write_outcome)
            log_error(classify_error(report, context), context, error)
            return False
        except BaseException:
            connection.close()
            raise

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiomysql.Connection]:
        """Read scopes end their transaction before returning the connection."""
        async with self._borrow() as connection:
            try:
                yield connection
            except BaseException as error:
                await self._rollback(connection, write_outcome="not_started")
                if isinstance(error, asyncio.TimeoutError):
                    connection.close()
                if isinstance(error, Exception) and not isinstance(error, AppError):
                    raise self._failure(error) from error
                raise
            else:
                if not await self._rollback(connection, write_outcome="not_started"):
                    raise DatabaseError(code=ErrorCode.DB_UNAVAILABLE)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiomysql.Connection]:
        async with self._borrow() as connection:
            try:
                await self.run(connection.begin())
            except BaseException as error:
                connection.close()
                if isinstance(error, Exception):
                    raise self._failure(error) from error
                raise
            try:
                yield connection
            except BaseException as error:
                rolled_back = await self._rollback(connection)
                if isinstance(error, asyncio.TimeoutError):
                    connection.close()
                if isinstance(error, asyncio.CancelledError):
                    raise
                if isinstance(error, AppError) and rolled_back:
                    error.write_outcome = 'rolled_back'
                    raise
                if not isinstance(error, Exception):
                    raise
                raise self._failure(error, write_outcome='rolled_back' if rolled_back else 'unknown') from error
            try:
                await self.run(connection.commit())
            except BaseException as error:
                # Once COMMIT was attempted, even a later rollback proves nothing.
                connection.close()
                if not isinstance(error, Exception):
                    raise
                raise self._failure(error, commit=True) from error


def readonly_retry(method):
    """Retry one whole read scope once; never apply to write methods."""
    from functools import wraps
    @wraps(method)
    async def wrapper(*args, **kwargs):
        for attempt in range(2):
            try:
                return await method(*args, **kwargs)
            except Exception as error:
                report = classify_error(error, ErrorContext())
                if attempt or report.code not in {ErrorCode.DB_UNAVAILABLE, ErrorCode.DB_TIMEOUT} or report.write_outcome == 'unknown':
                    raise
    return wrapper
