"""Task-scoped progress callbacks; service code has no Discord dependency."""
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
import inspect
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowEvent:
    guild_id: int
    project_id: str
    meeting_id: str
    stage: str
    state: str
    agent_name: str | None = None
    round_number: int | None = None
    current: int | None = None
    total: int | None = None
    attempt: int | None = None
    decision_version: int | None = None


WorkflowProgressCallback = Callable[[WorkflowEvent], Awaitable[None]]
_callback = ContextVar("workflow_callback", default=None)
_last_event = ContextVar("workflow_last_event", default=None)


async def emit_event(event):
    _last_event.set(event)
    callback = _callback.get()
    if callback is not None:
        try:
            await callback(event)
        except Exception as error:
            logger.warning("進度通知失敗 meeting_id=%s error_type=%s", event.meeting_id, type(error).__name__)


async def emit_progress(record, stage, state="started", **kwargs):
    await emit_event(WorkflowEvent(record.guild_id, record.project_id, record.meeting_id, stage, state,
        decision_version=record.decision_version, **kwargs))


async def emit_retry(attempt):
    event = _last_event.get()
    if event is not None:
        await emit_event(replace(event, state="retrying", attempt=attempt))


def reports_progress(method):
    """Optional callback propagates through nested calls, isolated per async task."""
    @wraps(method)
    async def wrapper(*args, on_progress=None, **kwargs):
        if on_progress is None:
            return await method(*args, **kwargs)
        token = _callback.set(on_progress)
        last_token = _last_event.set(None)
        try:
            return await method(*args, **kwargs)
        except BaseException:
            event = _last_event.get()
            if event is not None:
                await emit_event(replace(event, state="failed"))
            raise
        finally:
            _last_event.reset(last_token)
            _callback.reset(token)
    signature = inspect.signature(method)
    wrapper.__signature__ = signature.replace(parameters=[*signature.parameters.values(),
        inspect.Parameter("on_progress", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=WorkflowProgressCallback | None)])
    return wrapper
