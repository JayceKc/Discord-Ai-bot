"""Resolve configured channels only within the requesting Guild."""
import logging
import asyncio
import re
import discord
from presentation.discord_delivery import DELIVERY_ERRORS
from core.logging_security import redact_sensitive

logger = logging.getLogger(__name__)


class ChannelRouter:
    def __init__(self, bot, settings, status_store=None):
        self.bot = bot
        self.settings = settings
        self.status_messages = {}
        self.status_locks = {}
        self.status_store = status_store

    def _matches_status(self, message, key):
        bot_id = getattr(getattr(self.bot, "user", None), "id", None)
        if bot_id is None or getattr(getattr(message, "author", None), "id", None) != bot_id:
            return False
        if getattr(getattr(message, "channel", None), "id", None) != key[1] or getattr(message, "components", []):
            return False
        for embed in getattr(message, "embeds", []):
            if "專案狀態" not in str(getattr(embed, "title", "")):
                continue
            footer = str(getattr(getattr(embed, "footer", None), "text", ""))
            meeting = re.search(r"會議\s+([^｜\s]+)", footer)
            page = re.search(r"第\s*(\d+)/\d+\s*頁", footer)
            if meeting and meeting.group(1) == key[2] and (page.group(1) if page else "1") == key[3]:
                return True
        return False

    async def _restore_status(self, target, key):
        message_id = self.status_store.get(key)
        if message_id:
            try:
                message = await asyncio.wait_for(target.fetch_message(message_id), 30)
                if self._matches_status(message, key):
                    return message
            except discord.NotFound:
                pass
        # 升級前的舊卡沒有索引：只查最近 100 則，不掃描整個伺服器。
        async def find():
            async for message in target.history(limit=100):
                if self._matches_status(message, key):
                    return message
            return None
        return await asyncio.wait_for(find(), 30)

    def _remember_status(self, key, message):
        self.status_messages[key] = message
        if self.status_store and getattr(message, "channel", None) is not None and message.channel.id == key[1]:
            try:
                self.status_store.put(key, message.id)
            except OSError:
                logger.warning("狀態卡索引保存失敗 guild_id=%s", key[0])

    def resolve(self, guild_id, purpose, origin_channel):
        origin_guild = getattr(getattr(origin_channel, "guild", None), "id", None)
        if origin_guild is not None and origin_guild != guild_id:
            return None
        ids = {
            "meeting": self.settings.agent_meeting_channel_id,
            "status": self.settings.project_status_channel_id,
            "dev_log": self.settings.dev_log_channel_id,
        }
        channel_id = ids[purpose]
        if purpose == "dev_log" and channel_id in {
            self.settings.project_intake_channel_id, ids["meeting"], ids["status"], getattr(origin_channel, "id", None)
        }:
            return None
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is not None and callable(getattr(channel, "send", None)) and getattr(getattr(channel, "guild", None), "id", None) == guild_id:
            permissions = channel.permissions_for(channel.guild.me)
            if permissions.view_channel and permissions.send_messages and permissions.embed_links:
                return channel
        return None if purpose == "dev_log" else origin_channel

    async def send(self, guild_id, purpose, origin_channel, *, content=None, embed=None, view=None):
        if purpose == "dev_log" and content is not None:
            content = redact_sensitive(content)
        target = self.resolve(guild_id, purpose, origin_channel)
        if target is None:
            return None
        footer = str(getattr(getattr(embed, "footer", None), "text", ""))
        meeting = re.search(r"會議\s+([^｜\s]+)", footer)
        if purpose == "status" and meeting and view is None:
            page = re.search(r"第\s*(\d+)/\d+\s*頁", footer)
            key = (guild_id, target.id, meeting.group(1), page.group(1) if page else "1")
            lock = self.status_locks.setdefault(key, asyncio.Lock())
            async with lock:
                message = self.status_messages.get(key)
                if message is None and self.status_store is not None:
                    try:
                        message = await self._restore_status(target, key)
                    except DELIVERY_ERRORS as error:
                        # 無法確認舊卡是否存在時不補發，避免網路錯誤導致重複。
                        logger.warning("狀態卡恢復失敗 guild_id=%s error_type=%s", guild_id, type(error).__name__)
                        return None
                if message is not None:
                    try:
                        await asyncio.wait_for(message.edit(content=content, embed=embed, allowed_mentions=discord.AllowedMentions.none()), 30)
                        self._remember_status(key, message)
                        return message
                    except discord.NotFound:
                        self.status_messages.pop(key, None)
                    except DELIVERY_ERRORS as error:
                        logger.warning("狀態卡更新失敗 guild_id=%s error_type=%s", guild_id, type(error).__name__)
                        return None
                message = await self._send(target, guild_id, purpose, origin_channel, content, embed, view)
                if message is not None:
                    self._remember_status(key, message)
                return message
        return await self._send(target, guild_id, purpose, origin_channel, content, embed, view)

    async def _send(self, target, guild_id, purpose, origin_channel, content, embed, view):
        kwargs = {"allowed_mentions": discord.AllowedMentions.none()}
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        try:
            return await asyncio.wait_for(target.send(content, **kwargs), 30)
        except DELIVERY_ERRORS as error:
            logger.warning("頻道傳送失敗 purpose=%s guild_id=%s error_type=%s", purpose, guild_id, type(error).__name__)
            if isinstance(error, (discord.Forbidden, discord.NotFound)) and purpose != "dev_log" and origin_channel is not None and target != origin_channel:
                try:
                    return await asyncio.wait_for(origin_channel.send(content, **kwargs), 30)
                except DELIVERY_ERRORS:
                    logger.warning("原頻道傳送亦失敗 guild_id=%s", guild_id)
            return None
