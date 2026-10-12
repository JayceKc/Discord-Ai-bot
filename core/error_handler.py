"""Report failures without re-running business operations."""
from dataclasses import replace
from core.errors import ErrorContext, classify_error
from core.logging_security import log_error
from core.recovery import build_recovery_action


class ErrorHandler:
    async def handle(self, error, context=None, *, interaction=None, channel=None):
        from presentation.discord_delivery import safe_error_reply, safe_channel_send
        import discord
        context = context or ErrorContext(write_outcome='unknown')
        if interaction is not None and context.command is None:
            command = getattr(getattr(interaction, 'command', None), 'name', None)
            context = replace(context, command=command)
        report = classify_error(error, context)
        if report.write_outcome == 'unknown' and report.code.value.startswith('DB_'):
            report = replace(report, next_action=build_recovery_action(replace(context, write_outcome='unknown')))
        log_error(report, context, error)
        if interaction is not None:
            await safe_error_reply(interaction, report)
        elif channel is not None:
            from presentation.error_presenter import build_error_embeds
            await safe_channel_send(channel, f'❌ {report.code.value}｜查詢代碼：{report.reference_id}',
                embeds=build_error_embeds(report), allowed_mentions=discord.AllowedMentions.none())
        return report
