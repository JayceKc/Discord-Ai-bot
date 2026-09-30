"""可於 Bot 重啟後恢復的決策與批准按鈕。"""
from __future__ import annotations

import logging
import inspect
import discord

from services.meeting_manager import MeetingManagerError
from services.user_decision_service import UserDecisionService

logger = logging.getLogger(__name__)
SECTION_LABELS = {
    "background_and_goal": "背景與目標", "integrated_solution": "整合方案",
    "execution_plan": "執行計畫", "risks_and_responses": "風險與對策", "acceptance_criteria": "驗收標準",
}


def decision_embed(record):
    question = record.user_decision["question"]
    embed = discord.Embed(title="🗳️ 需要你決定：" + question["topic"][:180],
        description=question["why_user_decision_needed"], color=discord.Color.blue())
    source_lines = []
    for source in question["sources"]:
        step = record.steps[source["step_index"]]
        source_lines.append(f"{step.agent_name} 第 {step.round_number} 輪：{source['quote']}")
    embed.add_field(name="討論依據", value="\n".join(source_lines)[:1024], inline=False)
    last = record.user_decision["history"][-1] if record.user_decision["history"] else None
    for option in question["options"]:
        rejected = last and last["outcome"] == "rejected" and last["selection"]["option_id"] == option["option_id"]
        embed.add_field(name=f"{option['option_id']}｜{option['label']}" + ("（已駁回，請改選）" if rejected else ""),
            value=f"{option['description']}\n好處：{option['benefits']}\n代價：{option['tradeoffs']}\n影響：" + "、".join(SECTION_LABELS[key] for key in option["expected_changes"]), inline=False)
    embed.set_footer(text=f"只有會議啟動者可選擇。備用：/decide choice:A（或 B、C）｜版本 {record.decision_version}")
    return embed


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
        try:
            record = await self.presenter.service.get(self.guild_id)
            self.presenter.service.authorize(record, interaction.guild_id, interaction.user.id,
                meeting_id=self.meeting_id, version=self.version,
                message_id=interaction.message.id if interaction.message else -1)
            return True
        except MeetingManagerError as error:
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return False

    async def handle(self, interaction, choice):
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            record = await self.presenter.service.act(interaction.guild_id, interaction.user.id, choice,
                meeting_id=self.meeting_id, version=self.version, message_id=interaction.message.id)
        except MeetingManagerError as error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            logger.warning("無法停用舊決策按鈕 meeting_id=%s", self.meeting_id)
        await self.presenter.present(record, interaction.channel)
        try:
            await interaction.followup.send("✅ 操作已保存；若未看到結果，請使用 /review。", ephemeral=True)
        except discord.HTTPException:
            logger.warning("決策已保存但互動回覆失敗 meeting_id=%s", self.meeting_id)

    async def on_error(self, interaction, error, item):
        logger.error("決策按鈕處理失敗 error_type=%s", type(error).__name__)
        if interaction.response.is_done():
            await interaction.followup.send("❌ 操作中斷，請使用 /review 查看狀態或 /decide 重試。", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 操作中斷，請稍後重試。", ephemeral=True)


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
    def __init__(self, bot, service: UserDecisionService, proposal_formatter, review_formatter):
        self.bot = bot
        self.service = service
        self.proposal_formatter = proposal_formatter
        self.review_formatter = review_formatter

    async def present(self, record, channel):
        if channel is None:
            return False
        try:
            await self.disable_previous(record, channel)
            if record.decision_status == "applying_choice":
                await channel.send("選擇已保存。處理中斷時請用 `/decide` 選擇相同選項重試；Bot 中斷的處理保留最多 15 分鐘。")
                return True
            if record.decision_status == "awaiting_choice":
                view = DecisionView(self, record)
                message = await channel.send(embed=decision_embed(record), view=view)
            elif record.decision_status in {"awaiting_approval", "approved"}:
                approved = record.decision_status == "approved"
                proposal = record.final_proposal if approved else record.candidate_proposal
                embed = self.proposal_formatter(proposal,
                    metrics=record.final_proposal_metrics if approved else record.candidate_proposal_metrics)
                embed.title = ("已批准｜" if approved else "待批准｜") + embed.title[:245]
                for key, label in SECTION_LABELS.items():
                    embed.add_field(name=label, value=proposal["sections"][key][:1024], inline=False)
                await channel.send(embed=embed)
                await channel.send(embed=self.review_formatter(record.candidate_review, stage="已批准方案" if approved else "待批准方案"))
                before = record.review_result["quality_evaluation"]["outputs"]["quality_index"]
                after = record.candidate_review["quality_evaluation"]["outputs"]["quality_index"]
                embed = discord.Embed(title="✅ 方案已批准" if approved else "請確認決策後的方案",
                    description=f"選擇：{record.user_decision['selection']['option_id']}\n品質指標：{before} → {after}（{after-before:+.1f}）",
                    color=discord.Color.green() if approved else discord.Color.orange())
                # Each impact fits one field; preserve every actual change.
                for i in record.user_decision["impact"]:
                    embed.add_field(name=SECTION_LABELS[i["section"]],
                        value=f"修改前：{i['before']}\n修改後：{i['after']}\n理由：{i['reason']}"[:1024], inline=False)
                if approved:
                    await channel.send(embed=embed)
                    return True
                embed.set_footer(text=f"備用：/decide choice:approve 或 reject｜版本 {record.decision_version}")
                view = ApprovalView(self, record)
                message = await channel.send(embed=embed, view=view)
            else:
                return False
            await self.service.remember_message(record.guild_id, record.meeting_id, record.decision_version,
                record.decision_status, channel.id, message.id)
            return True
        except (discord.HTTPException, MeetingManagerError) as error:
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
                await target.get_partial_message(message["message_id"]).edit(view=None)
            except discord.HTTPException:
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
