"""不丟棄內容的訊息與 Embed 分段。"""
from dataclasses import dataclass
import re
import discord
from presentation.agent_profiles import AgentProfile, SYSTEM


@dataclass(frozen=True)
class EmbedSection:
    name: str
    value: str


def split_discord_message(content: str, max_length: int) -> list[str]:
    if max_length <= 0:
        raise ValueError("Discord 訊息長度必須大於 0。")
    chunks = []
    while len(content) > max_length:
        cut = content.rfind("\n", 0, max_length)
        if cut >= 0:
            cut += 1
        else:
            cut = max_length
        chunks.append(content[:cut])
        content = content[cut:]
    if content or not chunks:
        chunks.append(content)
    return chunks


def _formatted_chunks(text: str, limit: int) -> list[str]:
    # Each displayed chunk closes and reopens a Markdown fence. The body is
    # otherwise preserved byte-for-byte; synthetic fences use their own lines.
    result = []
    opened = None
    remaining = text
    while remaining:
        prefix = f"```{opened}\n" if opened is not None else ""
        budget = limit - len(prefix) - 5
        if budget < 8:
            raise ValueError("分段長度不足以保留 Markdown 格式。")
        cut = min(len(remaining), budget)
        if cut < len(remaining):
            boundary = remaining.rfind("\n", 0, cut)
            if boundary >= 0:
                cut = boundary + 1
            # Do not split a fence marker itself.
            marker = remaining.rfind("```", 0, cut + 2)
            if marker >= 0 and marker < cut < marker + 3:
                cut = marker if marker else marker + 3
        body = remaining[:cut]
        for match in re.finditer(r"```([^\n`]*)", body):
            language = match.group(1)
            opened = (language if len(language) <= 80 else "") if opened is None else None
        suffix = "\n```" if opened is not None else ""
        result.append(prefix + body + suffix)
        remaining = remaining[cut:]
    return result or ["（無）"]


def paginate_embed(*, title: str, description: str = "", sections: list[EmbedSection] | None = None,
                   profile: AgentProfile = SYSTEM, footer: str = "") -> list[discord.Embed]:
    """Each page contains one embed; all metadata is included in its budget."""
    sections = list(sections or [])
    title = str(title)
    footer = str(footer)
    if len(title) > 235:
        sections.insert(0, EmbedSection("完整標題", title))
        title = "完整內容"
    if len(footer) > 1800:
        sections.append(EmbedSection("補充資訊", footer))
        footer = "補充資訊見內文。"
    fields = []
    for section in sections:
        name = str(section.name) or "內容"
        value = str(section.value) or "（無）"
        if len(name) > 240:
            value = name + "\n" + value
            name = "完整欄位"
        chunks = _formatted_chunks(value, 1024)
        for i, chunk in enumerate(chunks):
            fields.append((name + (f"（續 {i + 1}）" if i else ""), chunk))
    author = f"{profile.emoji} {profile.display_name}"
    # Reserve space for final page labels before packing.
    fixed = len(title) + len(author) + len(footer) + 45
    descriptions = _formatted_chunks(description, min(3900, 5500-fixed)) if description else []
    pages = []
    for chunk in descriptions:
        pages.append(discord.Embed(title=title, description=chunk, color=profile.color))
    if not pages:
        pages.append(discord.Embed(title=title, color=profile.color))
    for name, value in fields:
        page = pages[-1]
        if len(page.fields) >= 25 or len(page) + len(name) + len(value) + fixed > 5500:
            page = discord.Embed(title=title, color=profile.color)
            pages.append(page)
        page.add_field(name=name, value=value, inline=False)
    for index, page in enumerate(pages, 1):
        page.title = f"{title}｜{index}/{len(pages)}"
        page.set_author(name=author)
        page.set_footer(text=f"{footer}\n第 {index}/{len(pages)} 頁".strip())
        if len(page) > 6000 or len(page.title) > 256 or any(len(f.name) > 256 or len(f.value) > 1024 for f in page.fields):
            raise ValueError("Embed 超過 Discord 限制。")
    return pages
