"""將已保存的業務資料轉成可讀、完整的中文 Embed。"""
from presentation.agent_profiles import get_agent_profile, SYSTEM
from presentation.message_chunks import EmbedSection, paginate_embed

LABELS = {
    "goal": "目標", "constraints": "限制", "work_items": "工作項目", "disagreements": "分歧",
    "known_information": "已知資訊", "reasonable_inferences": "合理推論", "items_to_verify": "待查證事項",
    "proposals": "創意方案", "title": "名稱", "description": "說明", "research_basis": "研究依據",
    "implementation_steps": "執行步驟", "cost_considerations": "成本考量", "risks": "風險", "alternatives": "替代方案",
    "background_and_goal": "背景與目標", "integrated_solution": "整合方案", "execution_plan": "執行計畫",
    "risks_and_responses": "風險與對策", "acceptance_criteria": "驗收標準", "completeness": "完整度",
    "creativity": "創意", "credibility": "可信度", "feasibility": "可行性", "problem": "問題",
    "required_change": "修改要求", "priority": "優先度", "assigned_agent": "負責 Agent",
    "reason": "理由", "sources": "來源", "decision": "取捨", "topic": "主題",
    "quality_index": "品質指標", "risk_level": "風險等級", "improvement_suggestions": "改善建議",
    "dimension": "面向", "label": "項目", "level": "程度", "suggestion": "建議",
    "formula_version": "公式版本", "outputs": "計算結果", "inputs": "評估依據", "weights": "權重",
    "project_priority": "優先目標", "priority_snapshot": "優先目標", "issues": "問題與修改要求",
    "revision_allowed": "可自動修改", "score": "分數", "passed": "通過", "status": "狀態",
    "advisories": "非必要改善建議", "basis": "判斷依據",
}
SECTION_LABELS = {key: LABELS[key] for key in (
    "background_and_goal", "integrated_solution", "execution_plan", "risks_and_responses", "acceptance_criteria")}


def readable(value):
    if value is None:
        return "（無）"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, dict):
        return "\n".join(f"{LABELS.get(str(k), str(k))}：{readable(LABELS.get(v, v) if k == 'dimension' and isinstance(v, str) else v)}" for k, v in value.items()) or "（無）"
    if isinstance(value, (list, tuple)):
        return "\n".join(f"• {readable(item)}" for item in value) or "（無）"
    return str(value)


def metadata(record):
    if record is None:
        return ""
    return f"專案 {record.project_id}｜會議 {record.meeting_id}｜版本 {record.decision_version}"


def _sections(data):
    return [EmbedSection(LABELS.get(str(key), str(key)), readable(value)) for key, value in data.items()]


def build_project_embeds(project):
    action = {
        "pending": f"使用 /start project_id:{project.id} 開始討論。",
        "in_progress": f"專案 {project.id} 進行中；使用 /status 查看進度與接續方式。",
        "completed": f"專案 {project.id} 已完成；可回專案入口建立下一個專案。",
        "cancelled": f"專案 {project.id} 已取消；可回專案入口建立下一個專案。",
    }.get(project.status.value, "使用 /status 查看目前狀態。")
    return paginate_embed(title=f"專案｜{project.id}｜{project.title}", sections=[
        EmbedSection("類型", project.category), EmbedSection("需求", readable(project.requirements)),
        EmbedSection("預算", f"NT$ {project.budget:,}"), EmbedSection("期限", project.deadline.isoformat()),
        EmbedSection("狀態", project.status.label), EmbedSection("驗收條件", readable(project.acceptance_criteria)),
        EmbedSection("需求變更", readable([f"{c.id}：{c.description}" for c in project.requirement_changes])),
        EmbedSection("下一步", action),
    ])


def build_discussion_embeds(step, record=None, *, current=None, total=None):
    progress = f"｜{current}/{total}" if current is not None else ""
    return paginate_embed(title=f"第 {step.round_number} 輪{progress}｜Agent 討論",
        sections=_sections(step.output_data or {}), profile=get_agent_profile(step.agent_name), footer=metadata(record))


def build_proposal_embeds(proposal, record=None, *, stage="PM 草案", next_step=None):
    proposal = proposal or {}
    sections = _sections(proposal.get("sections", {}))
    sections.append(EmbedSection("PM 取捨與來源", readable(proposal.get("decisions", []))))
    sections.extend(_sections({k: v for k, v in proposal.items() if k not in {"title", "summary", "sections", "decisions"}}))
    next_action = "請先閱讀完整方案，再查看決策與評估。"
    if stage == "PM 草案":
        next_action = "使用 /review 審查並產生使用者選項；需要第二輪時先用 /change。"
    sections.append(EmbedSection("下一步", next_step or next_action))
    return paginate_embed(title=f"{stage}｜{proposal.get('title', '方案')}", description=str(proposal.get("summary", "")),
        sections=sections, profile=get_agent_profile("PM Agent"), footer=metadata(record))


def build_review_embeds(review, record=None, *, stage="草案"):
    review = review or {}
    sections = []
    for key, item in review.get("checklist", {}).items():
        if not isinstance(item, dict):
            sections.append(EmbedSection(LABELS.get(key, key), readable(item)))
            continue
        score = item.get("score")
        result = f"{score}/5" if isinstance(score, int) and not isinstance(score, bool) else ("通過" if item.get("passed") else "未通過")
        sections.append(EmbedSection(f"{LABELS.get(key, key)}｜{result}", readable({k: v for k, v in item.items() if k not in {"score", "passed"}})))
    evaluation = review.get("quality_evaluation")
    if evaluation:
        outputs = evaluation.get("outputs", {})
        priority = evaluation.get("priority", {})
        index = outputs.get("quality_index", "未知")
        sections.append(EmbedSection(f"{stage}品質指標", f"{index}/100｜{outputs.get('grade', '')}\n優先目標：{priority.get('label', '未記錄')}\n{evaluation.get('explanation', '')}"))
        sections.append(EmbedSection("剩餘風險", readable(evaluation.get("risks", []))))
        sections.append(EmbedSection("改善建議", readable(evaluation.get("improvements", []))))
    sections.append(EmbedSection("問題與修改要求", readable(review.get("issues", []))))
    sections.extend(_sections({k: v for k, v in review.items() if k not in {"status", "checklist", "quality_evaluation", "issues", "post_revision_review", "proposal_digest", "decision_version"}}))
    sections.append(EmbedSection("下一步", "審查結果供判斷風險；需完成使用者選擇與批准，專案才正式完成。"))
    pages = paginate_embed(title=f"{stage} Review 審查｜{review.get('status', '未知')}", sections=sections,
        profile=get_agent_profile("Review Agent"), footer=metadata(record))
    return pages


def _operation_card(record, title, action):
    owner = f"<@{record.decision_owner_user_id}>" if record.decision_owner_user_id else "原會議啟動者"
    card = paginate_embed(title=title, sections=[EmbedSection("操作者", owner), EmbedSection("下一步", action)],
        profile=SYSTEM, footer=metadata(record))[-1]
    card.color = 0x2ECC71 if record.decision_status == "approved" else 0xE67E22
    return card


def build_decision_embeds(record):
    data = record.user_decision
    question = data["question"]
    sources = []
    for source in question["sources"]:
        index = source["step_index"]
        step = record.steps[index] if 0 <= index < len(record.steps) else None
        sources.append(f"{step.agent_name if step else '來源待確認'} 第 {step.round_number if step else '?'} 輪：{source['quote']}")
    sections = [EmbedSection("討論依據", "\n".join(sources))]
    last = data["history"][-1] if data.get("history") else None
    for option in question["options"]:
        rejected = last and last.get("outcome") == "rejected" and last["selection"]["option_id"] == option["option_id"]
        sections.append(EmbedSection(f"{option['option_id']}｜{option['label']}" + ("（已駁回，請改選）" if rejected else ""),
            f"{option['description']}\n好處：{option['benefits']}\n代價：{option['tradeoffs']}\n影響：" + "、".join(SECTION_LABELS.get(k, k) for k in option["expected_changes"])))
    pages = paginate_embed(title="需要你決定｜" + question["topic"], description=question["why_user_decision_needed"],
        sections=sections, profile=get_agent_profile("PM Agent"), footer=metadata(record))
    pages.append(_operation_card(record, "🗳️ 等待選擇方向", "閱讀上方選項後按 A／B／C。備用：/decide choice:A（或畫面中的 B／C）。"))
    return pages


def build_approval_embeds(record):
    approved = record.decision_status == "approved"
    stage = "已批准方案" if approved else "待批准方案"
    pages = build_proposal_embeds(record.final_proposal if approved else record.candidate_proposal, record, stage=stage)
    pages.extend(build_review_embeds(record.candidate_review, record, stage=stage))
    before = (record.review_result or {}).get("quality_evaluation", {}).get("outputs", {}).get("quality_index")
    after = (record.candidate_review or {}).get("quality_evaluation", {}).get("outputs", {}).get("quality_index")
    comparison = f"{before} → {after}（{after-before:+.1f}）" if isinstance(before, (int, float)) and isinstance(after, (int, float)) else "舊資料無可比較評分。"
    sections = [EmbedSection("選擇", str(record.user_decision["selection"]["option_id"])), EmbedSection("品質指標變化", comparison)]
    for impact in record.user_decision.get("impact", []):
        sections.append(EmbedSection(SECTION_LABELS.get(impact["section"], impact["section"]),
            f"修改前：{impact['before']}\n修改後：{impact['after']}\n理由：{impact['reason']}"))
    pages.extend(paginate_embed(title="決策對方案的影響", sections=sections, footer=metadata(record)))
    pages.append(_operation_card(record, "✅ 專案已批准完成" if approved else "🗳️ 等待批准",
        "可回專案入口建立下一個專案。" if approved else "確認完整方案、評估與剩餘風險後，按批准或駁回。備用：/decide choice:approve 或 reject。"))
    return pages
