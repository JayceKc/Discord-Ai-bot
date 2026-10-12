"""Recovery suggestions only use durable state."""
def build_recovery_action(context, record=None):
    if context.write_outcome == 'unknown':
        return '先 /status 或 /projects 查證；不要重送建立或批准。'
    if record is None:
        return '先 /status 確認目前階段，再使用原指令。'
    from presentation.progress import next_action
    return next_action(record)
