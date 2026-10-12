"""Bounded Discord delivery. Private replies never fall back to public."""
import asyncio
import logging

import aiohttp
import discord

logger = logging.getLogger(__name__)
DELIVERY_ERRORS = (discord.HTTPException, asyncio.TimeoutError, aiohttp.ClientError, OSError)


async def safe_defer(interaction, *, ephemeral=False, thinking=True):
    try:
        if interaction.response.is_done():
            return True
        options = dict(thinking=thinking)
        if ephemeral:
            options["ephemeral"] = True
        await asyncio.wait_for(interaction.response.defer(**options), 2.5)
        return True
    except discord.InteractionResponded:
        return True
    except DELIVERY_ERRORS as error:
        logger.warning('discord_ack_failed exception_type=%s', type(error).__name__)
        return False


async def safe_followup_send(interaction, *args, ephemeral=False, **kwargs):
    try:
        await asyncio.wait_for(interaction.followup.send(*args, ephemeral=ephemeral, **kwargs), 30)
        return True
    except DELIVERY_ERRORS as error:
        logger.warning('discord_followup_failed exception_type=%s', type(error).__name__)
        # Only definite rejection permits fallback. Network/5xx outcomes may
        # already have delivered the message; do not duplicate an operation card.
        definite = isinstance(error, (discord.NotFound, discord.Forbidden))
        if not ephemeral and definite and getattr(interaction, 'channel', None) is not None:
            return await safe_channel_send(interaction.channel, *args, **kwargs) is not None
        return False


async def safe_channel_send(channel, *args, **kwargs):
    try:
        return await asyncio.wait_for(channel.send(*args, **kwargs), 30)
    except DELIVERY_ERRORS as error:
        logger.warning('discord_send_failed exception_type=%s', type(error).__name__)
        return None


async def safe_message_edit(message, **kwargs):
    try:
        await asyncio.wait_for(message.edit(**kwargs), 30)
        return True
    except DELIVERY_ERRORS as error:
        logger.warning('discord_edit_failed exception_type=%s', type(error).__name__)
        return False


async def safe_original_response_edit(interaction, content):
    """更新同一則指令提示；編輯失敗不補發，避免新增重複公開訊息。"""
    try:
        await asyncio.wait_for(interaction.edit_original_response(
            content=content, allowed_mentions=discord.AllowedMentions.none(),
        ), 10)
        return True
    except DELIVERY_ERRORS as error:
        logger.warning('discord_original_edit_failed exception_type=%s', type(error).__name__)
        return False


async def safe_error_reply(interaction, report):
    from presentation.error_presenter import build_error_embeds
    content = f'❌ {report.code.value}｜查詢代碼：{report.reference_id}'
    kwargs = dict(ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), embeds=build_error_embeds(report))
    async def private_followup():
        # 公開 defer 的第一則 followup 會繼承公開設定，不能直接放錯誤卡片。
        # 先結束公開等待訊息（只放中性文字），再傳送真正的私人錯誤。
        if getattr(interaction.response, 'type', None) == discord.InteractionResponseType.deferred_channel_message:
            original = await asyncio.wait_for(interaction.original_response(), 10)
            if original.flags.loading and not original.flags.ephemeral:
                await asyncio.wait_for(interaction.edit_original_response(
                    content='本次操作未完成。請查看私人回覆，或使用 /status 查詢。',
                    embeds=[], attachments=[], view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                ), 10)
        return await safe_followup_send(interaction, content, **kwargs)
    try:
        if interaction.response.is_done():
            return await private_followup()
        try:
            await asyncio.wait_for(interaction.response.send_message(content, **kwargs), 2.5)
            return True
        except discord.InteractionResponded:
            return await private_followup()
    except DELIVERY_ERRORS as error:
        logger.warning('discord_error_reply_failed exception_type=%s', type(error).__name__)
        return False
