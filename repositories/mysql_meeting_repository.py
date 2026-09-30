"""以 MySQL 原子保存完整 Agent 會議紀錄。"""

from __future__ import annotations

import json
from typing import Any

from database.db import DatabaseError, MySQLDatabase
from models.meeting import MeetingDataError, MeetingRecord, MeetingStatus
from repositories.meeting_repository import MeetingRepositoryError


class MySQLMeetingRepository:
    """會議主檔、步驟與 Guild 最新會議索引使用同一筆交易保存。"""

    def __init__(self, database: MySQLDatabase) -> None:
        self.database = database

    async def save(self, record: MeetingRecord) -> None:
        data = record.to_dict()
        try:
            async with self.database.transaction() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute("SELECT storage_version FROM meetings WHERE id = %s FOR UPDATE", (record.meeting_id,))
                    existing = await cursor.fetchone()
                    if existing is not None and existing["storage_version"] != record.storage_version:
                        raise MeetingRepositoryError("會議資料已更新，請重新讀取最新狀態。")
                    if existing is None and record.storage_version != 0:
                        raise MeetingRepositoryError("會議紀錄不存在，不能覆寫。")
                    await cursor.execute(
                        """
                        INSERT INTO meetings (
                            id, guild_id, project_id, requirement, status,
                            current_step_index, meeting_context,
                            applied_requirement_change, proposal_draft, proposal_metrics,
                            review_result, revision_count, revision_agent_name,
                            revision_output, final_proposal, final_proposal_metrics, error,
                            decision_owner_user_id, decision_status, decision_version, storage_version, user_decision, candidate_proposal, candidate_proposal_metrics, candidate_review, decision_messages
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        ) AS new
                        ON DUPLICATE KEY UPDATE
                            guild_id = new.guild_id, project_id = new.project_id,
                            requirement = new.requirement, status = new.status,
                            current_step_index = new.current_step_index,
                            meeting_context = new.meeting_context,
                            applied_requirement_change = new.applied_requirement_change,
                            proposal_draft = new.proposal_draft,
                            proposal_metrics = new.proposal_metrics,
                            review_result = new.review_result,
                            revision_count = new.revision_count,
                            revision_agent_name = new.revision_agent_name,
                            revision_output = new.revision_output,
                            final_proposal = new.final_proposal,
                            final_proposal_metrics = new.final_proposal_metrics,
                            error = new.error,
                            decision_owner_user_id = new.decision_owner_user_id,
                            decision_status = new.decision_status,
                            decision_version = new.decision_version,
                            storage_version = new.storage_version,
                            user_decision = new.user_decision,
                            candidate_proposal = new.candidate_proposal,
                            candidate_proposal_metrics = new.candidate_proposal_metrics,
                            candidate_review = new.candidate_review,
                            decision_messages = new.decision_messages
                        """,
                        (
                            data["meeting_id"], data["guild_id"], data["project_id"],
                            data["requirement"], data["status"], data["current_step_index"],
                            _dump_json(data["meeting_context"]),
                            _dump_json(data["applied_requirement_change"]),
                            _dump_json(data["proposal_draft"]),
                            _dump_json(data["proposal_metrics"]),
                            _dump_json(data["review_result"]), data["revision_count"],
                            data["revision_agent_name"], _dump_json(data["revision_output"]),
                            _dump_json(data["final_proposal"]),
                            _dump_json(data["final_proposal_metrics"]), data["error"],
                            data["decision_owner_user_id"], data["decision_status"], data["decision_version"], record.storage_version + 1, _dump_json(data["user_decision"]), _dump_json(data["candidate_proposal"]), _dump_json(data["candidate_proposal_metrics"]), _dump_json(data["candidate_review"]), _dump_json(data["decision_messages"]),
                        ),
                    )
                    await cursor.execute(
                        "DELETE FROM meeting_steps WHERE meeting_id = %s AND sequence_no >= %s",
                        (record.meeting_id, len(record.steps)),
                    )
                    if record.steps:
                        await cursor.executemany(
                            """
                            INSERT INTO meeting_steps (
                                meeting_id, sequence_no, agent_name, step_order, round_number,
                                status, input_text, output_data, input_characters,
                                output_characters, prompt_tokens, completion_tokens,
                                max_output_tokens, execution_time_seconds, error
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s
                            ) AS new
                            ON DUPLICATE KEY UPDATE
                                agent_name = new.agent_name, step_order = new.step_order,
                                round_number = new.round_number, status = new.status,
                                input_text = new.input_text, output_data = new.output_data,
                                input_characters = new.input_characters,
                                output_characters = new.output_characters,
                                prompt_tokens = new.prompt_tokens,
                                completion_tokens = new.completion_tokens,
                                max_output_tokens = new.max_output_tokens,
                                execution_time_seconds = new.execution_time_seconds,
                                error = new.error
                            """,
                            [
                                (
                                    record.meeting_id, sequence_no, step.agent_name,
                                    step.order, step.round_number, step.status.value,
                                    step.input_text, _dump_json(step.output_data),
                                    step.input_characters, step.output_characters,
                                    step.prompt_tokens, step.completion_tokens,
                                    step.max_output_tokens, step.execution_time_seconds,
                                    step.error,
                                )
                                for sequence_no, step in enumerate(record.steps)
                            ],
                        )
                    await cursor.execute(
                        """
                        INSERT INTO guild_latest_meetings (guild_id, meeting_id)
                        VALUES (%s, %s) AS new
                        ON DUPLICATE KEY UPDATE meeting_id = new.meeting_id
                        """,
                        (record.guild_id, record.meeting_id),
                    )
                    if record.final_proposal is not None and record.decision_status not in {"legacy", "approved"}:
                        raise MeetingRepositoryError("方案尚未批准，不能保存為最終方案。")
                    if record.status == MeetingStatus.COMPLETED and record.final_proposal is not None:
                        # 最終方案、專案完成及 Guild 釋放必須同筆交易提交。
                        # 最新會議索引仍指向這場會議，供歷史／使用量查詢；
                        # 下一場新會議保存時會將它改指向新會議。
                        await cursor.execute(
                            "SELECT project_id FROM guild_current_projects "
                            "WHERE guild_id = %s FOR UPDATE",
                            (record.guild_id,),
                        )
                        current = await cursor.fetchone()
                        if current is not None and current["project_id"] == record.project_id:
                            await cursor.execute(
                                "SELECT status FROM projects WHERE id = %s FOR UPDATE",
                                (record.project_id,),
                            )
                            project = await cursor.fetchone()
                            if project is None or project["status"] not in ("in_progress", "completed"):
                                raise MeetingRepositoryError("最終方案已產生，但專案狀態無法完成。")
                            if project["status"] == "in_progress":
                                await cursor.execute(
                                    "UPDATE projects SET status = 'completed' WHERE id = %s",
                                    (record.project_id,),
                                )
                            await cursor.execute(
                                "DELETE FROM guild_current_projects "
                                "WHERE guild_id = %s AND project_id = %s",
                                (record.guild_id, record.project_id),
                            )
            record.storage_version += 1
        except MeetingRepositoryError:
            raise
        except Exception as error:
            raise MeetingRepositoryError("無法保存 MySQL 會議紀錄。") from error

    async def get(self, meeting_id: str) -> MeetingRecord | None:
        try:
            async with self.database.connection() as connection:
                async with connection.cursor() as cursor:
                    return await self._load(cursor, meeting_id)
        except MeetingRepositoryError:
            raise
        except (DatabaseError, Exception) as error:
            raise MeetingRepositoryError("無法讀取 MySQL 會議紀錄。") from error

    async def get_for_guild(self, guild_id: int) -> MeetingRecord | None:
        try:
            async with self.database.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT meeting_id FROM guild_latest_meetings WHERE guild_id = %s",
                        (guild_id,),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        return None
                    return await self._load(cursor, str(row["meeting_id"]))
        except MeetingRepositoryError:
            raise
        except (DatabaseError, Exception) as error:
            raise MeetingRepositoryError("無法讀取 MySQL Guild 會議紀錄。") from error

    async def pending_decisions(self) -> list[MeetingRecord]:
        try:
            async with self.database.connection() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT m.id FROM meetings m JOIN guild_latest_meetings g ON g.meeting_id = m.id "
                        "WHERE m.decision_status IN ('awaiting_choice', 'awaiting_approval', 'applying_choice')")
                    ids = [row["id"] for row in await cursor.fetchall()]
                    return [record for meeting_id in ids if (record := await self._load(cursor, meeting_id)) is not None]
        except Exception as error:
            raise MeetingRepositoryError("無法恢復使用者決策。") from error

    async def _load(self, cursor: Any, meeting_id: str) -> MeetingRecord | None:
        await cursor.execute(
            """
            SELECT id, guild_id, project_id, requirement, status, current_step_index,
                   meeting_context, applied_requirement_change, proposal_draft,
                   proposal_metrics, review_result, revision_count, revision_agent_name,
                   revision_output, final_proposal, final_proposal_metrics, error,
                   decision_owner_user_id, decision_status, decision_version, storage_version, user_decision, candidate_proposal, candidate_proposal_metrics, candidate_review, decision_messages
            FROM meetings WHERE id = %s
            """,
            (meeting_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        await cursor.execute(
            """
            SELECT agent_name, step_order, round_number, status, input_text,
                   output_data, input_characters, output_characters, prompt_tokens,
                   completion_tokens, max_output_tokens, execution_time_seconds, error
            FROM meeting_steps WHERE meeting_id = %s ORDER BY sequence_no
            """,
            (meeting_id,),
        )
        steps = [
            {
                "agent_name": item["agent_name"], "order": item["step_order"],
                "round_number": item["round_number"], "status": item["status"],
                "input_text": item["input_text"],
                "output_data": _load_json(item["output_data"]),
                "input_characters": item["input_characters"],
                "output_characters": item["output_characters"],
                "prompt_tokens": item["prompt_tokens"],
                "completion_tokens": item["completion_tokens"],
                "max_output_tokens": item["max_output_tokens"],
                "execution_time_seconds": (
                    float(item["execution_time_seconds"])
                    if item["execution_time_seconds"] is not None else None
                ),
                "error": item["error"],
            }
            for item in await cursor.fetchall()
        ]
        data = {
            "meeting_id": row["id"], "guild_id": row["guild_id"],
            "project_id": row["project_id"], "requirement": row["requirement"],
            "status": row["status"], "current_step_index": row["current_step_index"],
            "steps": steps, "meeting_context": _load_json(row["meeting_context"]),
            "applied_requirement_change": _load_json(row["applied_requirement_change"]),
            "proposal_draft": _load_json(row["proposal_draft"]),
            "proposal_metrics": _load_json(row["proposal_metrics"]),
            "review_result": _load_json(row["review_result"]),
            "revision_count": row["revision_count"],
            "revision_agent_name": row["revision_agent_name"],
            "revision_output": _load_json(row["revision_output"]),
            "final_proposal": _load_json(row["final_proposal"]),
            "final_proposal_metrics": _load_json(row["final_proposal_metrics"]),
            "error": row["error"],
            "decision_owner_user_id": row["decision_owner_user_id"],
            "decision_status": row["decision_status"],
            "decision_version": row["decision_version"],
            "storage_version": row["storage_version"],
            "user_decision": _load_json(row["user_decision"]),
            "candidate_proposal": _load_json(row["candidate_proposal"]),
            "candidate_proposal_metrics": _load_json(row["candidate_proposal_metrics"]),
            "candidate_review": _load_json(row["candidate_review"]),
            "decision_messages": _load_json(row["decision_messages"]) or [],
        }
        try:
            return MeetingRecord.from_dict(data)
        except (MeetingDataError, ValueError, TypeError) as error:
            raise MeetingRepositoryError("MySQL 會議紀錄格式不正確。") from error


def _dump_json(value: object) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _load_json(value: object) -> object:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    raise MeetingRepositoryError("MySQL JSON 欄位格式不正確。")
