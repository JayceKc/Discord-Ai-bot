"""核對已保存的發言來源，找出可重現的文字截斷特徵。"""
import copy
import re

from agents.structured_agent import AgentSchemaValidationError


def source_catalogue(record):
    return sorted({f"{step.agent_name} 第{step.round_number}輪"
        for step in record.steps
        if step.round_number in {1, 2} and step.output_data is not None
        and getattr(step.status, "value", step.status) == "completed"})


def checked_sources(proposal, catalogue):
    if not catalogue:
        return proposal
    result = copy.deepcopy(proposal)
    for decision in result.get("decisions", []):
        if "sources" not in decision:
            continue
        normalized = []
        for source in decision.get("sources", []):
            agent = re.search(r"(?:PM|Research|Creative|Finance) Agent", source)
            round_match = re.search(r"(?:第\s*|Round(?:_number)?\s*[:：]?\s*)([\d一二]+)\s*(?:輪)?", source, re.I)
            matches = [label for label in catalogue if agent and label.startswith(agent.group() + " 第")]
            if round_match:
                round_text = round_match.group(1)
                round_number = {"一": 1, "二": 2}.get(round_text)
                if round_number is None:
                    round_number = int(round_text)
                matches = [label for label in matches if label == f"{agent.group()} 第{round_number}輪"]
            if len(matches) != 1:
                raise AgentSchemaValidationError("決策來源必須對應已完成的 Agent 與輪次；請使用 allowed_sources 中的完整標籤。")
            normalized.append(matches[0])
        decision["sources"] = list(dict.fromkeys(normalized))
    return result


def incomplete_sections(proposal):
    findings = []
    for section, value in proposal.get("sections", {}).items():
        if not isinstance(value, str):
            continue
        text = value.strip().rstrip('」』"\'）)')
        ended = text.endswith(tuple("。！？.!?；;"))
        dangling = re.search(r"(?:並|以及|包含|包括|以便|例如|交付驗收：本|整理包含防重)$", text)
        if not ended and (dangling or len(text) >= 230):
            findings.append(section)
    return findings


def stale_cli_decisions(proposal, requirement, user_choice):
    """針對已重現的 CLI／即時 Web 矛盾做窄範圍防線，不猜測一般語意。"""
    constraint = (requirement or "") + str(user_choice or "")
    compact = re.sub(r"\s+", "", constraint)
    if "CLI" not in compact or not re.search(r"(?:禁止|不得|不提供).{0,8}即時Web", compact, re.I):
        return False
    for decision in proposal.get("decisions", []):
        reason = str(decision.get("reason", ""))
        if re.search(r"(?:優先|优先|先採用|先使用).{0,20}(?:Streamlit|即時Web)", reason, re.I) or re.search(r"(?:超支|超出預算).{0,15}(?:才|再)改.{0,8}CLI", reason, re.I):
            return True
    return False
