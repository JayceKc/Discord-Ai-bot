"""Reconcile a project submission by the actual generated primary key."""
import inspect
from core.errors import AppError, ErrorCode, ErrorContext, classify_error, error_chain


async def resolve(value):
    return await value if inspect.isawaitable(value) else value


class ProjectSubmissionService:
    def __init__(self, repository):
        self.repository = repository

    async def submit(self, session):
        if session.stage == 'write_outcome_unknown':
            raise AppError(code=ErrorCode.DB_COMMIT_UNKNOWN, write_outcome='unknown')
        try:
            project = await resolve(self.repository.create_custom_project(session.guild_id, **session.fields))
        except Exception as error:
            report = classify_error(error, ErrorContext())
            if report.write_outcome in {'unknown', 'committed'}:
                session.stage = 'write_outcome_unknown'
                session.submission_project_id = next((e.operation for e in error_chain(error) if getattr(e, 'operation', None)), None)
            raise
        session.project_id = project.id
        return project

    async def reconcile_submission(self, session):
        if session.stage != 'write_outcome_unknown':
            return
        # Matching free-form fields is not proof of identity. Missing identifier
        # remains blocked for administrator review, including after a restart.
        if not session.submission_project_id:
            raise AppError(code=ErrorCode.DB_COMMIT_UNKNOWN, write_outcome='unknown')
        project = await resolve(self.repository.get_for_guild(session.guild_id, session.submission_project_id))
        if project is None:
            raise AppError(code=ErrorCode.DB_COMMIT_UNKNOWN, write_outcome='unknown')
        session.project_id = project.id
        session.stage = 'submission_verified'
        session.awaiting_confirmation = False

    async def submit_change(self, session, description):
        """保留不確定寫入的變更 ID，查證前不得再次新增。"""
        if session.stage == 'change_outcome_unknown':
            raise AppError(code=ErrorCode.DB_COMMIT_UNKNOWN, write_outcome='unknown')
        try:
            return await resolve(self.repository.add_requirement_change(
                session.guild_id, session.project_id, description))
        except Exception as error:
            report = classify_error(error)
            if report.write_outcome in {'unknown', 'committed'}:
                session.stage = 'change_outcome_unknown'
                session.submission_change_id = next((e.operation for e in error_chain(error)
                    if getattr(e, 'operation', None)), None)
            raise

    async def reconcile_change(self, session):
        if not session.submission_change_id or not session.project_id:
            raise AppError(code=ErrorCode.DB_COMMIT_UNKNOWN, write_outcome='unknown')
        project = await resolve(self.repository.get_for_guild(session.guild_id, session.project_id))
        change = next((item for item in project.requirement_changes
            if item.id == session.submission_change_id), None) if project else None
        if change is None:
            raise AppError(code=ErrorCode.DB_COMMIT_UNKNOWN, write_outcome='unknown')
        session.change_id, session.change = change.id, change
        session.stage = 'second_round_failed'
