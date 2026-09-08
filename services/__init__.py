"""專案共用服務。"""
"""Discord Bot 使用的服務層。"""

from .project_meeting_service import ProjectMeetingError, ProjectMeetingService

__all__ = ["ProjectMeetingError", "ProjectMeetingService"]
