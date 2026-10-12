from presentation.message_chunks import EmbedSection, paginate_embed


def build_error_embeds(report):
    pages = paginate_embed(title='❌ 操作未完成', sections=[
        EmbedSection('發生什麼', report.user_message),
        EmbedSection('目前保存狀態', report.text().split('\n')[1].partition('：')[2]),
        EmbedSection('下一步', report.next_action),
        EmbedSection('查詢代碼', f'{report.reference_id}（{report.code.value}）')])

    for page in pages:
        page.color = 0xE74C3C
    return pages
