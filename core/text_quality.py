"""文字預算保留完整句子；完整性檢查不猜測或補寫模型原文。"""
import json
import re

ENDINGS = tuple("。！？.!?；;")
NARRATIVE_FIELDS = {
    "goal", "constraints", "work_items", "disagreements", "description",
    "implementation_steps", "known_information", "reasonable_inferences",
    "items_to_verify", "cost_considerations", "risks", "alternatives",
    "summary", "background_and_goal", "integrated_solution", "execution_plan",
    "risks_and_responses", "acceptance_criteria", "reason", "problem",
    "required_change", "why_user_decision_needed", "benefits", "tradeoffs",
    "rewritten_text",
}


def incomplete_text(text):
    clean = text.strip().rstrip('」』"\'）)')
    # 長段中文必須有句尾；短標籤、引用和識別碼不依此規則判定。
    body = clean.rstrip("".join(ENDINGS))
    dangling = re.search(r"(?:並|以及|包含|包括|以便|例如|流暢體|整理包含防重|交付驗收：本)$", body)
    if dangling:
        return True
    return bool(clean) and not clean.endswith(ENDINGS) and len(clean) >= 40 and bool(re.search(r"[\u4e00-\u9fff]", clean))


def incomplete_paths(value, path=(), field=None):
    if isinstance(value, str):
        return [".".join(map(str, path))] if field in NARRATIVE_FIELDS and incomplete_text(value) else []
    if isinstance(value, dict):
        return [item for key, child in value.items()
            for item in incomplete_paths(child, (*path, key), key)]
    if isinstance(value, list):
        return [item for index, child in enumerate(value)
            for item in incomplete_paths(child, (*path, index), field)]
    return []


def sentence_excerpt(text, budget, *, marker="…（其餘省略）"):
    """超出預算時只保留完整句子，無完整句子就明確省略。"""
    if len(text) <= budget:
        return text
    room = max(0, budget - len(marker))
    endings = [m.end() for m in re.finditer(r"[。！？.!?；;]", text[:room])]
    return text[:endings[-1]] + marker if endings else marker[:budget]


def bounded_json(value, budget):
    """縮減字串／項目而非切 JSON；省略旗標讓讀者知道資料不完整。"""
    def compact(item, limit):
        if isinstance(item, str):
            return sentence_excerpt(item, limit)
        if isinstance(item, list):
            return [compact(child, limit) for child in item[:2]]
        if isinstance(item, dict):
            return {key: compact(child, limit) for key, child in item.items()}
        return item
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= budget:
        return encoded
    for limit in (120, 60, 20):
        encoded = json.dumps({"content": compact(value, limit), "omitted": True}, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= budget:
            return encoded
    return '{"omitted":true}'


def whole_lines_excerpt(text, budget):
    """輪摘要可能嵌入 JSON：只保留整行，不切 JSON 裡的句號。"""
    if len(text) <= budget:
        return text
    marker = "…（其餘完整摘要省略；原文仍保存於 steps）"
    selected = []
    for line in text.splitlines():
        if len("\n".join([*selected, line, marker])) <= budget:
            selected.append(line)
    return "\n".join([*selected, marker]) if budget >= len(marker) else marker[:budget]
