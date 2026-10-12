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
