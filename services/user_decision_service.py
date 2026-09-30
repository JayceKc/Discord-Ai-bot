"""保存使用者決策、候選方案與批准流程；所有入口共用授權。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from models.meeting import MeetingRecord, ProposalMetrics
from models.user_decision import DecisionQuestion
from services.quality_evaluator import evaluate_review
from services.meeting_manager import MeetingManagerError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def proposal_digest(proposal: dict) -> str:
    return hashlib.sha256(json.dumps(proposal, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class UserDecisionService:
    """使用 MeetingManager 的 Guild lock，資料保存另有持久版本檢查。"""

    def __init__(self, manager):
        self.manager = manager

    async def get(self, guild_id: int) -> MeetingRecord:
        record = await self.manager._get_for_guild(guild_id)
        if record is None or record.decision_status == "legacy":
            raise MeetingManagerError("目前沒有新版使用者決策會議，請由 /meeting 或 /start 啟動新會議。")
        return record

    @staticmethod
    def authorize(record, guild_id, user_id, *, meeting_id=None, version=None, message_id=None):
        if guild_id is None or record.guild_id != guild_id:
            raise MeetingManagerError("此決策只能在原 Discord 伺服器操作。")
        if record.decision_owner_user_id != user_id:
            raise MeetingManagerError("只有啟動這場會議的使用者可以決策。")
        if meeting_id is not None and record.meeting_id != meeting_id:
            raise MeetingManagerError("這是舊會議的按鈕，請使用目前會議的訊息。")
        if version is not None and record.decision_version != version:
            raise MeetingManagerError("此按鈕已過期，請使用最新訊息或 /decide。")
        if message_id is not None and not any(
            m["message_id"] == message_id and m["version"] == record.decision_version
            and m["status"] == record.decision_status for m in record.decision_messages
        ):
            raise MeetingManagerError("此訊息不是目前有效的決策訊息，請使用 /decide。")

    async def prepare(self, record):
        """呼叫端已取得 Guild lock。生成後保存，重顯示不再呼叫模型。"""
        m = self.manager
        if record.review_result is None:
            record.review_result = await m._execute_workflow_agent(record, "Review Agent", m._build_review_input(record))
            m._attach_quality_evaluation(record, strict=True)
            await m._save(record)
        if record.user_decision is not None:
            return record
        discussion = [
            {"step_index": i, "agent": step.agent_name, "round": step.round_number,
                "output": self._discussion_excerpt(step.output_data)}
            for i, step in enumerate(record.steps)
            if step.round_number in {1, 2} and step.output_data is not None
        ]
        priority = m._priority_context(record)
        priority.pop("recent_experiences", None)
        context = {
            "project_id": record.project_id, "requirement": record.requirement,
            "requirement_change": record.applied_requirement_change.to_dict() if record.applied_requirement_change else None,
            "original_summary": record.proposal_draft["summary"],
            "review_issues": record.review_result.get("issues", [])[:2],
            "discussion": discussion, **priority,
        }
        prompt = json.dumps(context, ensure_ascii=False)
        for count in (2, 1):
            if len(prompt) <= m.max_prompt_characters:
                break
            for item in discussion:
                item["output"] = item["output"][:count]
            prompt = json.dumps(context, ensure_ascii=False)
        prompt = m._validate_prompt_budget(prompt)
        response = await m._retry_operation(lambda: m._pm_agent.create_decision(prompt),
            meeting_id=record.meeting_id, agent_name="PM Decision Agent", stage="user_decision")
        question = DecisionQuestion.model_validate(response.model_dump(mode="json"))
        for source in question.sources:
            if source.step_index >= len(record.steps):
                raise MeetingManagerError("決策分歧引用不存在的 Agent 步驟。")
            step = record.steps[source.step_index]
            if step.round_number not in {1, 2} or not step.output_data or not any(
                source.quote in value for value in self._strings(step.output_data)
            ):
                raise MeetingManagerError("決策分歧必須逐字引用已保存的討論內容，請重試 /review。")
        record.user_decision = {"question": question.model_dump(mode="json"), "history": [], "selection": None}
        record.decision_status = "awaiting_choice"
        record.decision_version += 1
        await m._save(record)
        return record

    @staticmethod
    def _discussion_excerpt(output):
        # Keep disagreements and constraints ahead of goals and general summaries.
        texts = []
        for key in ("disagreements", "constraints", "risks", "proposals", "alternatives", "items_to_verify"):
            value = next(UserDecisionService._strings(output.get(key)), None)
            if value:
                texts.append(value[:160])
        if not texts:
            texts = [value[:160] for value in UserDecisionService._strings(output)]
        return texts[:3]

    @staticmethod
    def _strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from UserDecisionService._strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from UserDecisionService._strings(item)

    async def act(self, guild_id, user_id, choice, *, meeting_id=None, version=None, message_id=None):
        m = self.manager
        lock = m._guild_locks.setdefault(guild_id, asyncio.Lock())
        if lock.locked():
            raise MeetingManagerError("目前正在處理這場會議，請稍後重試。")
        async with lock:
            record = await self.get(guild_id)
            self.authorize(record, guild_id, user_id, meeting_id=meeting_id, version=version, message_id=message_id)
            choice = choice.strip().upper()
            if record.decision_status == "awaiting_approval":
                return await self._approval(record, user_id, choice)
            if record.decision_status not in {"awaiting_choice", "applying_choice"}:
                raise MeetingManagerError("目前沒有等待選擇的決策，請使用 /review 查看狀態。")
            data = record.user_decision
            question = DecisionQuestion.model_validate(data["question"])
            option = next((o for o in question.options if o.option_id == choice), None)
            if option is None:
                raise MeetingManagerError("請選擇目前畫面中的 A、B 或 C 選項。")
            if record.decision_status == "applying_choice":
                if data["selection"]["option_id"] != choice:
                    raise MeetingManagerError("已有保存的選擇，請用相同選項重試完成處理。")
                if data.get("lease_until", 0) > time.time():
                    raise MeetingManagerError("此選擇正在處理；若 Bot 中斷，15 分鐘後可重試。")
            else:
                if data["history"] and data["history"][-1].get("outcome") == "rejected" and data["history"][-1]["selection"]["option_id"] == choice:
                    raise MeetingManagerError("此方向已駁回，請選擇另一個選項。")
                data["selection"] = {"option_id": choice, "user_id": user_id, "selected_at": now()}
                data["impact"] = []
                record.decision_status = "applying_choice"
                record.decision_version += 1
            data["lease_until"] = time.time() + 900
            data["operation_id"] = str(uuid4())
            record.error = None
            await m._save(record)
            try:
                await self._apply(record, option)
            except Exception as error:
                # CAS 保護：較舊操作不能清除新操作的 lease 或覆寫結果。
                record.error = "套用選擇或評分中斷；請使用 /decide 選擇相同選項重試。"
                data["lease_until"] = 0
                await m._save(record)
                if isinstance(error, MeetingManagerError):
                    raise
                raise MeetingManagerError(record.error) from error
            record.decision_status = "awaiting_approval"
            record.decision_version += 1
            record.candidate_review["decision_version"] = record.decision_version
            data["lease_until"] = 0
            await m._save(record)
            return record

    async def _apply(self, record, option):
        m = self.manager
        data = record.user_decision
        if record.candidate_proposal is None:
            await self._integrate(record, option)
        if record.candidate_review is None:
            await self._review(record, option)
        if record.candidate_review["status"] == "需要修改" and record.revision_count == 0:
            issue = m._select_revision_issue(record.candidate_review)
            agent = issue["assigned_agent"]
            prompt = m._build_revision_input(record, agent, record.candidate_review)
            output = await m._execute_workflow_agent(record, agent, prompt)
            record.revision_count = 1
            record.revision_agent_name = agent
            record.revision_output = output
            data["revision_pending"] = True
            await m._save(record)
        if data.get("revision_pending"):
            await self._integrate(record, option, revision=True)
            await self._review(record, option)

    async def _integrate(self, record, option, *, revision=False):
        m = self.manager
        context = {
            "project_id": record.project_id, "requirement": record.requirement,
            "requirement_change": record.applied_requirement_change.to_dict() if record.applied_requirement_change else None,
            "original_proposal": record.proposal_draft,
            "user_choice": option.model_dump(mode="json"),
            "question": record.user_decision["question"]["topic"],
            **m._priority_context(record),
        }
        if revision:
            context.update(original_proposal=record.candidate_proposal,
                review_issues=record.candidate_review["issues"], revision_output=record.revision_output)
        prompt = m._validate_prompt_budget(json.dumps(context, ensure_ascii=False))
        started = time.perf_counter()
        response = await m._retry_operation(lambda: m._pm_agent.apply_decision(prompt),
            meeting_id=record.meeting_id, agent_name="PM Choice Agent", stage="apply_choice")
        output = response.output
        proposal = output.proposal.model_dump(mode="json")
        if output.option_id != option.option_id:
            raise MeetingManagerError("PM 套用的選項與使用者選擇不一致。")
        if len(json.dumps(proposal, ensure_ascii=False)) > m.max_response_characters:
            raise MeetingManagerError("PM 候選方案超過字元預算。")
        impacts = []
        for impact in output.decision_impact:
            before = record.proposal_draft["sections"][impact.section]
            after = proposal["sections"][impact.section]
            if impact.section not in option.expected_changes or before.strip() == after.strip():
                raise MeetingManagerError("選擇必須實際改變選項指定的方案章節。")
            impacts.append({"section": impact.section, "before": before, "after": after, "reason": impact.reason})
        if not impacts:
            raise MeetingManagerError("候選方案沒有實際決策影響。")
        record.candidate_proposal = proposal
        record.candidate_proposal_metrics = ProposalMetrics(
            input_characters=len(prompt), output_characters=len(json.dumps(proposal, ensure_ascii=False)),
            prompt_tokens=response.usage.prompt_tokens, completion_tokens=response.usage.completion_tokens,
            max_output_tokens=response.max_output_tokens, execution_time_seconds=round(time.perf_counter()-started, 3))
        record.user_decision["impact"] = impacts
        if revision:
            record.user_decision["revision_pending"] = False
            record.candidate_review = None
        await m._save(record)

    async def _review(self, record, option):
        m = self.manager
        result = await m._execute_workflow_agent(record, "Review Agent", m._build_review_input(record, proposal=record.candidate_proposal))
        result["quality_evaluation"] = evaluate_review(result, record.meeting_context.priority_snapshot)
        result["proposal_digest"] = proposal_digest(record.candidate_proposal)
        result["decision_version"] = record.decision_version
        result["quality_evaluation"]["evaluated_artifact"] = "candidate_proposal"
        record.candidate_review = result
        record.user_decision.setdefault("evaluations", []).append(copy.deepcopy(result))
        await m._save(record)

    async def _approval(self, record, user_id, choice):
        if choice not in {"APPROVE", "REJECT"}:
            raise MeetingManagerError("方案等待確認，請選擇 approve（批准）或 reject（駁回）。")
        data = record.user_decision
        if not record.candidate_proposal or not record.candidate_review or record.candidate_review.get("decision_version") != record.decision_version or record.candidate_review.get("proposal_digest") != proposal_digest(record.candidate_proposal):
            raise MeetingManagerError("目前方案尚無對應評分，不能批准或駁回。")
        snapshot = {"selection": copy.deepcopy(data["selection"]), "impact": copy.deepcopy(data["impact"]),
            "proposal": copy.deepcopy(record.candidate_proposal), "review": copy.deepcopy(record.candidate_review),
            "evaluations": copy.deepcopy(data.get("evaluations", [])),
            "version": record.decision_version, "user_id": user_id, "acted_at": now(),
            "outcome": "approved" if choice == "APPROVE" else "rejected"}
        data["history"].append(snapshot)
        if choice == "APPROVE":
            record.final_proposal = copy.deepcopy(record.candidate_proposal)
            record.final_proposal_metrics = record.candidate_proposal_metrics
            record.decision_status = "approved"
        else:
            record.candidate_proposal = None
            record.candidate_review = None
            record.candidate_proposal_metrics = None
            data["selection"] = None
            data["impact"] = []
            data["evaluations"] = []
            record.decision_status = "awaiting_choice"
        record.decision_version += 1
        await self.manager._save(record)
        return record

    async def remember_message(self, guild_id, meeting_id, version, status, channel_id, message_id):
        lock = self.manager._guild_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            record = await self.get(guild_id)
            if record.meeting_id != meeting_id or record.decision_version != version or record.decision_status != status:
                return
            record.decision_messages.append({"channel_id": channel_id, "message_id": message_id, "version": version, "status": status})
            record.decision_messages = record.decision_messages[-20:]
            await self.manager._save(record)
