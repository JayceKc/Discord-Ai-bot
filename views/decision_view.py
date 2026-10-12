"""可於 Bot 重啟後恢復的決策與批准按鈕。"""
from __future__ import annotations

import logging
import inspect
import discord
from core.error_handler import ErrorHandler
from core.errors import ErrorContext
from presentation.discord_delivery import (safe_defer, safe_followup_send, safe_message_edit, safe_channel_send, safe_original_response_edit, DELIVERY_ERRORS)

from services.meeting_manager import MeetingManagerError
from services.user_decision_service import UserDecisionService
from presentation.embeds import build_decision_embeds, build_approval_embeds
from presentation.progress import build_status_embeds

logger = logging.getLogger(__name__)
SECTION_LABELS = {
    "background_and_goal": "背景與目標", "integrated_solution": "整合方案",
    "execution_plan": "執行計畫", "risks_and_responses": "風險與對策", "acceptance_criteria": "驗收標準",
}


def decision_embed(record):
    """Compatibility helper; full output is provided by build_decision_embeds."""
    return build_decision_embeds(record)[0]


class DecisionView(discord.ui.View):
    def __init__(self, presenter, record):
        super().__init__(timeout=None)
        self.presenter = presenter
        self.guild_id = record.guild_id
        self.meeting_id = record.meeting_id
        self.version = record.decision_version
        self.owner_id = record.decision_owner_user_id
        last = record.user_decision["history"][-1] if record.user_decision["history"] else None
        for option in record.user_decision["question"]["options"]:
            disabled = bool(last and last["outcome"] == "rejected" and last["selection"]["option_id"] == option["option_id"])
            self._add(option["option_id"], f"{option['option_id']}｜{option['label']}", discord.ButtonStyle.primary, disabled)

    def _add(self, choice, label, style, disabled=False):
        button = discord.ui.Button(label=label[:80], style=style, disabled=disabled,
            custom_id=f"day25:{self.meeting_id}:{self.version}:{choice}")
        async def callback(interaction):
            await self.handle(interaction, choice)
        button.callback = callback
        self.add_item(button)

    async def interaction_check(self, interaction):
        if not await safe_defer(interaction, ephemeral=True):
            return False
        try:
            record = await self.presenter.service.get(self.guild_id)
            self.presenter.service.authorize(record, interaction.guild_id, interaction.user.id,
                meeting_id=self.meeting_id, version=self.version,
                message_id=interaction.message.id if interaction.message else -1)
            return True
        except Exception as error:
            await ErrorHandler().handle(error, ErrorContext(guild_id=self.guild_id, write_outcome='not_started'), interaction=interaction)
            return False

    async def handle(self, interaction, choice):
        if not await safe_defer(interaction, ephemeral=True):
            return
        await safe_original_response_edit(interaction, "⏳ 正在處理；可用 /status 查看公開進度。")
        completed = False
        try:
            record = await self.presenter.act(interaction, choice,
                meeting_id=self.meeting_id, version=self.version, message_id=interaction.message.id)
            completed = True
        except Exception as error:
            await ErrorHandler().handle(error, ErrorContext(guild_id=self.guild_id, write_outcome='unknown'), interaction=interaction)
            return
        finally:
            await safe_original_response_edit(interaction, "✅ 操作已保存；請查看會議頻道的結果，或使用 /status 查詢目前階段與下一步。" if completed else "❌ 操作未完成；請查看錯誤卡。若已保存選擇，請用 /decide 重試相同選項。")
        for child in self.children:
            child.disabled = True
        try:
            await safe_message_edit(interaction.message, view=self)
        except DELIVERY_ERRORS:
            logger.warning("無法停用舊決策按鈕 meeting_id=%s", self.meeting_id)
        await self.presenter.present(record, interaction.channel)

    async def on_error(self, interaction, error, item):
        await ErrorHandler().handle(error, ErrorContext(guild_id=self.guild_id, write_outcome='unknown'), interaction=interaction)


class ApprovalView(DecisionView):
    def __init__(self, presenter, record):
        discord.ui.View.__init__(self, timeout=None)
        self.presenter = presenter
        self.guild_id = record.guild_id
        self.meeting_id = record.meeting_id
        self.version = record.decision_version
        self.owner_id = record.decision_owner_user_id
        self._add("approve", "批准方案", discord.ButtonStyle.success)
        self._add("reject", "駁回、重新選擇", discord.ButtonStyle.danger)


class DecisionPresenter:
    def __init__(self, bot, service: UserDecisionService, proposal_formatter=None, review_formatter=None, *, router=None, run_workflow=None):
        self.bot = bot
        self.router = router
        self.run_workflow = run_workflow
        self.service = service
        self.proposal_formatter = proposal_formatter
        self.review_formatter = review_formatter

    async def act(self, interaction, choice, **kwargs):
        if self.run_workflow is not None:
            return await self.run_workflow(self.service.act, interaction.guild_id,
                interaction.user.id, choice, channel=interaction.channel, **kwargs)
        return await self.service.act(interaction.guild_id, interaction.user.id, choice, **kwargs)

    async def _send(self, record, channel, embed, view=None):
        if self.router is not None:
            return await self.router.send(record.guild_id, "meeting", channel, embed=embed, view=view)
        return await safe_channel_send(channel, embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())

    async def present(self, record, channel):
        if channel is None:
            return False
        message = None
        view = None
        try:
            await self.disable_previous(record, channel)
            if record.decision_status == "awaiting_choice":
                pages = build_decision_embeds(record)
                view = DecisionView(self, record)
            elif record.decision_status in {"awaiting_approval", "approved"}:
                pages = build_approval_embeds(record)
                view = ApprovalView(self, record) if record.decision_status == "awaiting_approval" else None
            else:
                pages = build_status_embeds(record)
            for index, embed in enumerate(pages):
                message = await self._send(record, channel, embed, view if index == len(pages)-1 else None)
                if message is None:
                    return False
            if view is not None:
                target_id = getattr(getattr(message, "channel", None), "id", channel.id)
                try:
                    await self.service.remember_message(record.guild_id, record.meeting_id, record.decision_version,
                        record.decision_status, target_id, message.id)
                except Exception:
                    try:
                        await safe_message_edit(message, view=None)
                    except DELIVERY_ERRORS:
                        pass
                    raise
            if self.router is not None:
                # Reload after registering the operation message, so the status
                # link points to the exact channel and message authorized above.
                latest = await self.service.get(record.guild_id)
                target = self.router.resolve(record.guild_id, "status", channel)
                meeting_target = getattr(message, "channel", channel)
                if target is not None and getattr(target, "id", None) != getattr(meeting_target, "id", None):
                    for embed in build_status_embeds(latest):
                        await self.router.send(record.guild_id, "status", channel, embed=embed)
            return True
        except Exception as error:
            logger.warning("決策訊息未完成傳送 meeting_id=%s error_type=%s", record.meeting_id, type(error).__name__)
            return False

    async def disable_previous(self, record, channel):
        for message in record.decision_messages:
            if message["version"] == record.decision_version and message["status"] == record.decision_status:
                continue
            target = channel if channel.id == message["channel_id"] else (
                self.bot.get_channel(message["channel_id"]) if hasattr(self.bot, "get_channel") else None)
            if target is None or not hasattr(target, "get_partial_message"):
                continue
            try:
                await safe_message_edit(target.get_partial_message(message["message_id"]), view=None)
            except DELIVERY_ERRORS:
                logger.warning("舊按鈕訊息無法更新 message_id=%s", message["message_id"])

    async def restore(self):
        result = self.service.manager.repository.pending_decisions()
        records = await result if inspect.isawaitable(result) else result
        for record in records:
            for message in record.decision_messages:
                if message["version"] != record.decision_version or message["status"] != record.decision_status:
                    continue
                if record.decision_status == "awaiting_choice":
                    view = DecisionView(self, record)
                elif record.decision_status == "awaiting_approval":
                    view = ApprovalView(self, record)
                else:
                    continue
                self.bot.add_view(view, message_id=message["message_id"])
