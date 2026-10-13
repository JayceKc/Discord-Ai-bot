"""Live progress and status derived from durable meeting data."""
import asyncio
import logging
import time
import discord
from presentation.discord_delivery import DELIVERY_ERRORS, safe_message_edit
from presentation.agent_profiles import get_agent_profile
from presentation.embeds import metadata
from presentation.message_chunks import EmbedSection, paginate_embed

logger = logging.getLogger(__name__)

STAGES = {
    "discussion": "Agent 討論", "draft": "PM 整合草案", "baseline_review": "草案審查",
    "decision_preparation": "整理決策選項", "applying_choice": "套用使用者選擇",
    "candidate_review": "候選方案重新評分", "revision": "自動修改",
    "awaiting_choice": "等待選擇方向", "awaiting_approval": "等待批准", "approved": "專案已批准完成",
}


def next_action(record, *, is_running=False):
    if is_running:
        return "正在處理，暫時無需操作。可用 /status 查詢。"
    status = record.decision_status
    if status == "approved" or (status == "legacy" and record.final_proposal is not None):
        return "專案已完成，可回專案入口建立下一個專案。"
    if status == "applying_choice":
        selection = (record.user_decision or {}).get("selection") or {}
        option = selection.get("option_id", "A")
        lease = (record.user_decision or {}).get("lease_until", 0)
        if lease > time.time():
            return f"選擇 {option} 已保存；處理保留尚有 {max(1, int(lease-time.time()))} 秒。等待到期後，以 /decide choice:{option} 接續；不可改選。"
        return f"處理已中斷，可用 /decide choice:{option} 接續已保存選擇。"
    if record.error or record.status.value in {"failed", "cancelled"}:
        if record.proposal_draft:
            return "使用 /review 接續已保存的草案與審查。"
        return "自然語言流程在原 requests 頻道回覆「重試」；完整會議重跑相同 /meeting；分段流程用 /resume 接續失敗討論。"
    if status == "awaiting_choice":
        return "由原啟動者閱讀目前選項，按 A／B／C；備用：/decide choice:A（或畫面中的 B／C）。"
    if status == "awaiting_approval":
        return "由原啟動者檢視方案、評估與風險後批准或駁回；備用：/decide choice:approve 或 reject。"
    if record.proposal_draft:
        if record.applied_requirement_change is not None:
            return "兩輪草案已保存，使用 /review 審查；自然語言入口回覆「重試」接續。需求變更已使用一次，不能再次 /change。"
        return "使用 /review 審查；自然語言流程在原 requests 頻道回覆「直接定稿」或「修改：具體需求」。"
    if record.status.value == "completed":
        if record.applied_requirement_change is not None:
            return "兩輪討論已完成。使用 /draft 整合草案，或在原專案入口回覆「重試」；不必 /resume，也不能再次 /change。"
        return "討論已完成，專案尚未批准。使用 /draft 整合草案；需要第二輪時先用 /change。"
    return "需確認上次處理狀態；用 /review 查看可接續產物，或重跑原會議指令。"


def build_status_embeds(record, *, is_running=False, event=None):
    if record is None:
        return paginate_embed(title="尚未開始專案會議", sections=[EmbedSection("下一步", "在專案入口輸入「我要做一個網站」，或 /projects 選案後用 /start。")])
    stage = STAGES.get(record.decision_status)
    if record.decision_status == "legacy" and record.final_proposal:
        stage = "專案已完成（舊流程）"
    if stage is None:
        stage = "草案已保存" if record.proposal_draft else ("討論已完成" if record.status.value == "completed" else "需確認／可恢復")
    if is_running:
        stage = "處理中｜" + (STAGES.get(event.stage, event.stage) if event else stage)
    elif record.error:
        stage = "處理中斷｜" + stage
    sections = [EmbedSection("目前階段", stage), EmbedSection("下一步", next_action(record, is_running=is_running))]
    if is_running and event is not None and event.agent_name:
        sections.insert(1, EmbedSection("目前負責", get_agent_profile(event.agent_name).display_name))
    if record.decision_owner_user_id:
        sections.insert(1, EmbedSection("決策操作者", f"<@{record.decision_owner_user_id}>（會議啟動者）"))
    if record.error:
        sections.append(EmbedSection("中斷原因", "上次處理中斷；請依下一步接續。"))
    valid = [m for m in record.decision_messages if m["version"] == record.decision_version and m["status"] == record.decision_status]
    if valid:
        message = valid[-1]
        sections.append(EmbedSection("操作位置", f"[前往決策訊息](https://discord.com/channels/{record.guild_id}/{message['channel_id']}/{message['message_id']})；訊息遺失時用 /review 重新顯示。"))
    pages = paginate_embed(title="專案狀態", sections=sections, footer=metadata(record))
    color = 0xE74C3C if record.error else 0x2ECC71 if record.decision_status == "approved" else 0xE67E22 if record.decision_status in {"awaiting_choice", "awaiting_approval"} else 0x95A5A6
    for page in pages:
        page.color = color
    return pages


class ProgressPresenter:
    def __init__(self, router, guild_id, origin_channel):
        self.router = router
        self.guild_id = guild_id
        self.origin_channel = origin_channel
        self.message = None
        self.last_update = 0.0
        self.last_stage = None
        self.pending = None
        self.latest = None
        self.lock = asyncio.Lock()

    async def update(self, event):
        self.latest = event
        immediate = event.stage != self.last_stage or event.state in {"failed", "waiting", "completed"}
        if not immediate and time.monotonic() - self.last_update < 2:
            if self.pending is None:
                self.pending = asyncio.create_task(self._delayed())
            return
        await self._display(event)

    async def _delayed(self):
        try:
            await asyncio.sleep(max(0, 2 - (time.monotonic() - self.last_update)))
            await self._display(self.latest)
        except Exception as error:
            logger.warning("延後進度更新失敗 error_type=%s", type(error).__name__)
        finally:
            self.pending = None

    async def _display(self, event):
        async with self.lock:
            state = {"started": "⏳ 處理中", "retrying": "⏳ 重試中", "completed": "✅ 階段完成", "waiting": "🗳️ 等待操作", "failed": "❌ 處理中斷"}[event.state]
            agent = get_agent_profile(event.agent_name or "System")
            lines = [f"目前：{STAGES.get(event.stage, event.stage)}"]
            if event.agent_name:
                lines.append(f"負責：{agent.emoji} {agent.display_name}")
            if event.round_number:
                lines.append(f"第 {event.round_number} 輪")
            if event.current is not None:
                lines.append(f"本輪第 {event.current}/{event.total} 位 Agent")
            if event.attempt:
                lines.append(f"嘗試次數：{event.attempt}")
            lines.append("下一步：請等待；可用 /status 查詢。" if event.state not in {"failed", "waiting"} else "下一步：使用 /status 查看操作或恢復方式。")
            embed = paginate_embed(title=f"{state}｜{STAGES.get(event.stage, event.stage)}", description="\n".join(lines),
                profile=agent, footer=f"專案 {event.project_id}｜會議 {event.meeting_id}")[0]
            if event.state in {"failed", "waiting"} or event.stage == "approved":
                embed.color = 0xE74C3C if event.state == "failed" else 0x2ECC71 if event.stage == "approved" else 0xE67E22
            await self._send_or_edit(embed)
            self.last_stage = event.stage
            self.last_update = time.monotonic()

    async def _send_or_edit(self, embed):
        if self.message is not None:
            try:
                await asyncio.wait_for(self.message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none()), 30)
                return
            except discord.NotFound:
                self.message = None
            except DELIVERY_ERRORS as error:
                logger.warning('progress_edit_failed exception_type=%s', type(error).__name__)
                return
        self.message = await self.router.send(self.guild_id, "status", self.origin_channel, embed=embed)

    async def finish(self, record=None):
        if self.pending is not None:
            self.pending.cancel()
            try:
                await self.pending
            except asyncio.CancelledError:
                pass
            self.pending = None
        if record is not None:
            # Normal metadata fits on a single status card; send any overflow.
            pages = build_status_embeds(record)
            if self.latest is not None and self.latest.state == "failed":
                pages[0].title = "❌ 處理中斷｜專案狀態"
                pages[0].color = 0xE74C3C
            await self._send_or_edit(pages[0])
            for page in pages[1:]:
                await self.router.send(self.guild_id, "status", self.origin_channel, embed=page)


def build_intake_status_embeds(session):
    from services.project_intake import QUESTIONS
    if session.stage == "write_outcome_unknown":
        action = "建立結果尚未確認；回覆「查證」或用 /projects 查詢，勿重複建立。"
    elif session.stage == "submission_verified":
        action = f"專案已保存；使用 /start project_id:{session.project_id} 接續。"
    elif session.stage == "intent_confirmation":
        action = "回覆「確認」繼續建立需求草稿，或「取消」。"
    elif session.awaiting_confirmation:
        action = "核對需求摘要後回覆「確認」建立並開會，或「取消」。"
    elif session.stage == "preview":
        action = "回覆「直接定稿」進入選項與批准，或「修改：具體需求」開始第二輪。"
    else:
        action = QUESTIONS.get(session.missing_field(), "請依需求提示繼續。")
    return paginate_embed(title="需求收集中" if session.project_id is None else "第一輪草案等待回覆",
        sections=[EmbedSection("操作者", f"<@{session.user_id}>"),
            EmbedSection("操作位置", f"回到 <#{session.channel_id}> 回覆。"),
            EmbedSection("下一步", action)], footer="尚未提交的需求草稿會在閒置 30 分鐘或 Bot 重啟後消失。")
