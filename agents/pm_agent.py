"""負責整理專案目標與執行工作的 PM Agent。"""

from __future__ import annotations

from typing import Annotated, Literal
from dataclasses import replace
import json

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, create_model

from models.user_decision import DecisionQuestion, DecisionImpact

from agents.base_agent import AgentConfig, AgentLLMServiceProtocol
from agents.structured_agent import StructuredAgent, StructuredAgentResponse


# 移除模型不小心產生的前後空白，並拒絕空字串。
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SectionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=240),
]
SummaryText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]


class PMAnalysis(BaseModel):
    """PM 對一項專案需求的結構化分析。"""

    model_config = ConfigDict(extra="forbid")

    goal: NonEmptyText
    constraints: list[NonEmptyText]
    work_items: list[NonEmptyText]
    disagreements: list[NonEmptyText]


class PMProposalSections(BaseModel):
    """整合草案固定使用的五個文章章節。"""

    model_config = ConfigDict(extra="forbid")

    background_and_goal: SectionText
    integrated_solution: SectionText
    execution_plan: SectionText
    risks_and_responses: SectionText
    acceptance_criteria: SectionText


class PMDecision(BaseModel):
    """記錄 PM 對團隊意見的採用、拒絕或折衷決定。"""

    model_config = ConfigDict(extra="forbid")

    topic: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    decision: Literal["採用", "拒絕", "折衷"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    sources: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=45)]] = Field(
        min_length=1,
        max_length=3,
    )


class PMProposalDraft(BaseModel):
    """PM 將兩輪 Agent 意見整合後產生的固定格式提案。"""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    summary: SummaryText
    sections: PMProposalSections
    decisions: list[PMDecision] = Field(min_length=1, max_length=4)


class UserChoiceProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal: PMProposalDraft
    option_id: Literal["A", "B", "C"]
    decision_impact: list[DecisionImpact] = Field(min_length=1, max_length=5)


class DecisionSectionRewrite(BaseModel):
    """只改寫一個未反映使用者選擇的方案章節。"""

    model_config = ConfigDict(extra="forbid")
    rewritten_text: SectionText


class PMAgent(StructuredAgent[PMAnalysis]):
    """將需求整理為目標、限制、工作項目與分歧點。"""

    def __init__(self, llm_service: AgentLLMServiceProtocol) -> None:
        config = AgentConfig(
            name="PM Agent",
            role="專案經理",
            system_prompt=(
                "分析使用者提出的專案需求。整理一個明確目標、已知限制、"
                "可以執行的工作項目，以及需要團隊討論的分歧點。"
                "沒有分歧時 disagreements 使用空陣列，不可自行捏造資訊。"
                "輸入包含 requirement_change 時，必須針對新限制補充、反對或"
                "修正第一輪方案，不得只表示同意。"
                "若輸入包含 project_priority 與 recent_experiences，請以優先目標"
                "安排工作項目，參考同類專案經驗但不可照抄或當成本案事實。"
            ),
            temperature=0.2,
            seed=42,
            max_output_tokens=1000,
            # 直接從 Pydantic 模型產生 Ollama 的 JSON Schema，避免兩份格式漂移。
            json_schema=PMAnalysis.model_json_schema(),
        )
        super().__init__(config, llm_service, PMAnalysis)
        proposal_config = AgentConfig(
            name="PM Integration Agent",
            role="負責整合多角色意見的專案經理",
            system_prompt=(
                "根據已完成的一輪或兩輪摘要與重要原文，整理成一份可直接閱讀的專案提案。"
                "必須處理互相衝突的建議，不可只是逐項重述。"
                "sections 必須完整填寫專案背景與目標、整合方案、執行計畫、"
                "風險與對策、驗收標準五個固定章節。"
                "摘要最多 300 字，每個章節最多 240 字，重要決策最多 4 項。"
                "每章以 120～180 字為目標，保留收尾空間；使用完整短句並以句號結束，禁止在字元上限處切斷句子。"
                "每項重要取捨都要寫入 decisions，decision 只能是採用、拒絕或折衷，"
                "並在 sources 標示原意見的 Agent 與輪次。不可捏造輸入中沒有的事實。"
                "若有 allowed_sources，sources 必須逐字使用其中的標籤，不可自行推測輪次。"
                "若輸入包含 review_issues 與 revision_output，必須明確解決審查問題，"
                "並將指定 Agent 的修改整合進最終方案。"
                "必須依輸入中的 project_priority 說明重要取捨；"
                "recent_experiences 只作為參考，不可捏造本案事實。"
            ),
            # 整合兩輪摘要比單次需求分析耗時，避免沿用 60 秒預設值而過早取消。
            timeout_seconds=180.0,
            temperature=0.2,
            seed=42,
            max_output_tokens=1600,
            json_schema=PMProposalDraft.model_json_schema(),
        )
        self._proposal_agent = StructuredAgent(
            proposal_config,
            llm_service,
            PMProposalDraft,
        )

        self._decision_agent = StructuredAgent(AgentConfig(
            name="PM Decision Agent", role="整理真實需求取捨的專案經理",
            system_prompt=("從輸入討論找一項重要分歧或真實需求取捨，輸出 2～3 個可執行的不同選項。"
                "不得捏造衝突、放寬預算或硬性期限。sources 使用討論的 step_index 並逐字引用 output 內一段原文。"
                "選項依序 A、B、C，說明做法、好處、代價及會改變的章節。每個選項必須實際改變原方案。"
                "每個選項都必須滿足 requirement 與 requirement_change 的全部硬性驗收條件，不能把必要功能當成可刪除的取捨。"),
            temperature=0.2, seed=42, timeout_seconds=180, max_output_tokens=1400,
            json_schema=DecisionQuestion.model_json_schema()), llm_service, DecisionQuestion)
        self._choice_agent = StructuredAgent(AgentConfig(
            name="PM Choice Agent", role="落實使用者決策的專案經理",
            system_prompt=("依照使用者選擇實際修改原方案，輸出 proposal、option_id 與 decision_impact。"
                "影響必須落在選項 expected_changes 章節，不能只重述選擇。保留五章節與決策來源。"
                "遵守需求、預算、期限及優先目標。若有 review_issues 或 revision_output，整合修正且維持使用者選擇。"
                "expected_changes 內的每個章節都必須改寫成與選項一致的具體內容；即使原方案方向相同，也要加入新的可驗證條件、里程碑、數值或執行要求，不能複製原文。"
                "先比較 original_proposal 與新 proposal，再確認每個 expected_changes 章節的文字確實不同；decision_impact 必須列出實際改寫的章節與理由。"
                "必須重新核對 summary、五章節及 decisions：刪除與所選方向矛盾的舊取捨，不能只更新 expected_changes。"
                "sources 必須逐字使用 allowed_sources 的標籤。"
                "每章重新摘要成60～100字並用句號完整收尾，不可照抄接近上限的原章節或在字元上限處截斷。"
                "summary 最多300字，每章節最多240字；decision_impact 說明修改理由。"),
            temperature=0.2, seed=42, timeout_seconds=180, max_output_tokens=1800,
            text_budget_guidance="本任務每章以60～100字、2～3個完整短句為目標，摘要最多100字，理由最多60字。保留硬性需求與所選方向，省略重複背景；不要湊滿Schema上限。",
            json_schema=UserChoiceProposal.model_json_schema()), llm_service, UserChoiceProposal)
        self._section_rewrite_agent = StructuredAgent(AgentConfig(
            name="PM Choice Section Agent", role="落實使用者選擇的專案經理",
            system_prompt=(
                "你只負責改寫單一方案章節。依 choice 的 description、benefits、tradeoffs，"
                "改寫 section 對應的 current_text。保留需求中的硬性要求與數字，"
                "不可添加沒有根據的承諾；必須寫出選項的具體差異，不能只替換同義詞或照抄原文。"
                "同時核對 whole_proposal 的 summary、其餘章節與 decisions；遵守 choice 的已選方向，不可把被拒絕的方向當備案重新帶回。"
                "只輸出 rewritten_text JSON。"
                "以120～180字為目標，完整收尾並以句號結束。"
            ),
            temperature=0.2, seed=42, timeout_seconds=180, max_output_tokens=450,
            json_schema=DecisionSectionRewrite.model_json_schema(),
        ), llm_service, DecisionSectionRewrite)

    async def create_decision(self, meeting_input: str) -> DecisionQuestion:
        return await self._decision_agent.respond(meeting_input)

    async def apply_decision(self, meeting_input: str) -> StructuredAgentResponse[UserChoiceProposal]:
        return await self._choice_agent.respond_with_metadata(meeting_input)

    async def apply_decision_with_validation_feedback(self, meeting_input: str, validation_feedback: str):
        """只修正本次失敗欄位；不覆寫共用設定或提高輸出上限。"""
        return await self._choice_agent.respond_with_validation_feedback(meeting_input, validation_feedback)

    async def repair_decision_fields(self, meeting_input: str, invalid_response, incomplete_fields):
        """第二次只重寫未收尾章節，再驗證合併後的整份方案。"""
        paths = incomplete_fields or []
        sections = invalid_response.output.proposal.sections.model_dump()
        keys = [path.removeprefix('proposal.sections.') for path in paths]
        if not keys or any(not path.startswith('proposal.sections.') or key not in sections
                           for path, key in zip(paths, keys)):
            return await self.apply_decision_with_validation_feedback(meeting_input, '未收尾欄位：' + '、'.join(paths[:5]))
        output_model = create_model('DecisionFieldRepair', __config__=ConfigDict(extra='forbid'),
            **{key: (SectionText, ...) for key in keys})
        context = json.loads(meeting_input)
        original = invalid_response.output.model_dump(mode='json')
        prompt = json.dumps({
            'requirement': context['requirement'],
            'requirement_change': context.get('requirement_change'),
            'user_choice': context['user_choice'],
            'project_priority': context.get('project_priority'),
            'review_issues': context.get('review_issues', []),
            'revision_output': context.get('revision_output'),
            # 需求與使用者選項才是本次重写的依據。不要把已截斷的長段落
            # 再餵給模型，否則可能再次照抄到 Schema 上限附近。
            'fields_to_rewrite': keys,
        }, ensure_ascii=False)
        config = replace(self._choice_agent.config, json_schema=output_model.model_json_schema(),
            system_prompt='只輸出指定欄位的 JSON。原文字未完整收尾，必須重新摘要而非延續被截斷的句子。每欄以60～100字、2～3個完整短句為目標並以句號結束；保留相關硬性需求、數字與使用者所選方向，不增加保證或無根據事實。')
        repaired = await StructuredAgent(config, self.llm_service, output_model).respond_with_metadata(prompt)
        original['proposal']['sections'].update(repaired.output.model_dump(mode='json'))
        output = UserChoiceProposal.model_validate(original)
        # 使用同一完整性檢查，不能把局部修正當作免驗證通行證。
        from core.text_quality import incomplete_paths
        from agents.structured_agent import AgentSchemaValidationError
        if incomplete_paths(output.model_dump(mode='json')):
            raise AgentSchemaValidationError('局部修正後仍有未完整收尾內容。')
        return StructuredAgentResponse(output, repaired.usage, repaired.max_output_tokens)

    async def rewrite_decision_section(
        self, meeting_input: str,
    ) -> StructuredAgentResponse[DecisionSectionRewrite]:
        return await self._section_rewrite_agent.respond_with_metadata(meeting_input)

    async def rewrite_decision_section_with_validation_feedback(self, meeting_input: str, validation_feedback: str):
        return await self._section_rewrite_agent.respond_with_validation_feedback(meeting_input, validation_feedback)

    async def integrate(self, meeting_input: str) -> PMProposalDraft:
        """將兩輪討論整合成固定章節與決策紀錄。"""

        return await self._proposal_agent.respond(meeting_input)

    async def integrate_with_metadata(
        self,
        meeting_input: str,
    ) -> StructuredAgentResponse[PMProposalDraft]:
        """建立整合草案，並保留 Token 使用量與 1600 Token 上限。"""

        return await self._proposal_agent.respond_with_metadata(meeting_input)
