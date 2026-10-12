"""Redaction is a final guard; error logging uses metadata instead of payloads."""
from __future__ import annotations

import logging
import os
import re
import traceback

_secrets: tuple[str, ...] = ()


def register_secrets(secrets=()):
    global _secrets
    values = [value for key, value in os.environ.items()
              if any(part in key.upper() for part in ('TOKEN', 'PASSWORD', 'SECRET', 'API_KEY'))]
    _secrets = tuple(sorted({*(_secrets), *(str(s) for s in secrets if s), *values} - {''}, key=len, reverse=True))


def redact_sensitive(value: object, *, secrets=()) -> str:
    if isinstance(value, bytes):
        text = value.decode('utf-8', errors='replace')
    elif isinstance(value, BaseException):
        return f'<{type(value).__name__}>'
    else:
        try:
            text = str(value)
        except Exception:
            return '<unprintable>'
    for secret in sorted({*_secrets, *(str(s) for s in secrets if s)}, key=len, reverse=True):
        if secret:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(authorization[\s\'":=]+)(?:bearer\s+)?[^\s,}\'";]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)\bbearer\s+[^\s,}\'";]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'(?i)((?:password|passwd|token|secret|api[_-]?key)[\s\'":=]+)(?:"[^"]*"|\'[^\']*\'|[^\s&,;}]+)', r'\1[REDACTED]', text)
    text = re.sub(r'(https?://)[^/@\s]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'(https?://[^\s?]+)\?[^\s]+', r'\1?[REDACTED]', text)
    text = re.sub(r'(/api/webhooks/\d+/)[^\s/?]+', r'\1[REDACTED]', text)
    text = re.sub(r'\b(?:mfa\.[\w-]+|[\w-]{20,}\.[\w-]{6,}\.[\w-]{20,})\b', '[REDACTED]', text)
    return text


def _safe_argument(value, depth=0):
    if isinstance(value, BaseException):
        return f'<{type(value).__name__}>'
    if depth > 6:
        return '<depth limit>'
    if isinstance(value, dict):
        return {_safe_argument(key, depth+1): _safe_argument(item, depth+1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_safe_argument(item, depth+1) for item in value)
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return f'<{type(value).__name__}>'


class SafeLogFormatter(logging.Formatter):
    def format(self, record):
        # Clone so other handlers cannot recover cached unsafe exc_text.
        safe = logging.makeLogRecord(record.__dict__.copy())
        safe.msg = _safe_argument(safe.msg)
        safe.args = _safe_argument(safe.args)
        try:
            message = safe.getMessage()
        except Exception:
            message = "log_format_failed"
        if safe.exc_info:
            exception = safe.exc_info[1]
            frames = traceback.extract_tb(safe.exc_info[2])
            safe.msg = f'{redact_sensitive(message)} exception_type={type(exception).__name__} frames=' + ','.join(
                f'{frame.name}:{frame.lineno}' for frame in frames)
            safe.args = ()
        else:
            safe.msg = redact_sensitive(message)
            safe.args = ()
        safe.exc_info = safe.exc_text = safe.stack_info = None
        return redact_sensitive(super().format(safe))


class SafeLogFilter(logging.Filter):
    def filter(self, record):
        # Mutate before handler.emit so logging.handleError cannot print original
        # arguments or cached traceback if the output stream itself fails.
        record.msg = SafeLogFormatter('%(message)s').format(record)
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


def configure_safe_logging(level='INFO', *, secrets=()):
    register_secrets(secrets)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    formatter = SafeLogFormatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    loggers = [root] + [x for x in logging.Logger.manager.loggerDict.values() if isinstance(x, logging.Logger)]
    for log in loggers:
        for handler in log.handlers:
            handler.setFormatter(formatter)
            if not any(isinstance(item, SafeLogFilter) for item in handler.filters):
                handler.addFilter(SafeLogFilter())
    for name in ('discord', 'httpx', 'httpcore', 'ollama', 'aiomysql'):
        logging.getLogger(name).setLevel(logging.WARNING)


def log_error(report, context, error):
    def identifier(value):
        return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) else None
    commands = {'ask', 'hello', 'help', 'projects', 'workspace', 'priority', 'meeting', 'start', 'change', 'draft', 'review', 'resume', 'status', 'decide', 'delivery'}
    stages = {'discussion', 'draft', 'baseline_review', 'candidate_review', 'revision', 'applying_choice', 'awaiting_choice', 'awaiting_approval', 'approved', 'intake'}
    guild_id = context.guild_id if isinstance(context.guild_id, int) else None
    frames = ','.join(f'{frame.name}:{frame.lineno}' for frame in traceback.extract_tb(error.__traceback__))
    logging.getLogger('core.errors').warning(
        'operation_failed code=%s reference_id=%s exception_type=%s write_outcome=%s guild_id=%s command=%s stage=%s meeting_id=%s operation_id=%s frames=%s',
        report.code.value, report.reference_id, type(error).__name__, report.write_outcome, guild_id,
        context.command if context.command in commands else None,
        context.stage if context.stage in stages else None, identifier(context.meeting_id), identifier(context.operation_id), frames)
