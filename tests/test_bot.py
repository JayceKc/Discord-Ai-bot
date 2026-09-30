"""測試 Discord 指令流程；不會真的登入 Discord 或呼叫 Ollama。"""

import unittest  # Python 內建單元測試框架。
from datetime import date
from types import SimpleNamespace  # 快速建立只具有必要欄位的假物件。
from unittest.mock import AsyncMock, Mock  # 模擬 Discord、LLM 與 Repository 方法。

from bot import (
    TRUNCATION_SUFFIX,
    create_bot,
    format_discussion_messages,
    format_proposal_embed,
    format_review_embed,
)
from config import Settings
from models.meeting import MeetingStepRecord, MeetingStepStatus, ProposalMetrics
from models.project import Project, ProjectStatus, RequirementChange
from services.llm_service import LLMServiceError
from services.meeting_manager import MeetingManagerError, ReviewWorkflowResult
from services.quality_evaluator import evaluate_review


class BotCommandTest(unittest.IsolatedAsyncioTestCase):
    """使用假 LLMService 驗證 !hello 與 !ask 的行為。"""

    async def asyncSetUp(self) -> None:
        # 測試設定使用假的 Token；create_bot 不會拿它登入 Discord。
        self.settings = Settings(discord_token="fake-discord-token")
        self.fake_service = SimpleNamespace(chat=AsyncMock())  # 不連線 Ollama 的假服務。
        self.fake_project_repository = SimpleNamespace(list_projects=Mock(return_value=[]))
        self.fake_meeting_service = SimpleNamespace(start_project=AsyncMock())
        self.fake_discussion_manager = SimpleNamespace(
            start_first_round=AsyncMock(side_effect=self._run_fake_first_round),
            start_second_round=AsyncMock(side_effect=self._run_fake_second_round),
            create_proposal_draft=AsyncMock(
                return_value={
                    "title": "AI 客服提案",
                    "summary": "整合兩輪意見後，先完成核心問答功能。",
                    "sections": {},
                    "decisions": [
                        {
                            "topic": "交付範圍",
                            "decision": "採用",
                            "reason": "優先完成核心功能。",
                            "sources": ["PM Agent 第 2 輪"],
                        },
                        {
                            "topic": "進階報表",
                            "decision": "折衷",
                            "reason": "延後至第二階段。",
                            "sources": ["Finance Agent 第 2 輪"],
                        },
                    ],
                }
            ),
            get_proposal_metrics=Mock(
                return_value=ProposalMetrics(
                    input_characters=5100,
                    output_characters=3900,
                    prompt_tokens=1246,
                    completion_tokens=1520,
                    max_output_tokens=1600,
                    execution_time_seconds=72.35,
                )
            ),
            review_and_finalize=AsyncMock(
                return_value=ReviewWorkflowResult(
                    review={
                        "status": "需要修改",
                        "checklist": {
                            "completeness": {
                                "score": 2,
                                "reason": "缺少執行步驟",
                            },
                            "creativity": {"score": 4, "reason": "方案具體"},
                            "credibility": {"score": 4, "reason": "來源清楚"},
                            "feasibility": {"score": 4, "reason": "可執行"},
                        },
                        "issues": [
                            {
                                "problem": "缺少執行步驟",
                                "required_change": "補上具體步驟",
                                "priority": "高",
                                "assigned_agent": "Creative Agent",
                            }
                        ],
                        "revision_allowed": True,
                    },
                    revision_performed=True,
                    revision_agent_name="Creative Agent",
                    final_proposal={
                        "title": "AI 客服最終提案",
                        "summary": "已補上執行步驟。",
                        "sections": {},
                        "decisions": [],
                    },
                )
            ),
            get_final_proposal_metrics=Mock(return_value=None),
        )
        self.bot = create_bot(
            self.settings,
            self.fake_service,
            self.fake_project_repository,
            self.fake_meeting_service,
            self.fake_discussion_manager,
        )  # 注入假設定和假服務。

    async def _run_fake_first_round(
        self,
        guild_id: int,
        project_id: str,
        requirement: str,
        *,
        on_step,
    ) -> SimpleNamespace:
        """模擬四位 Agent 完成後依序通知 Discord callback。"""

        outputs = (
            ("PM Agent", {"goal": "完成品牌網站"}),
            ("Research Agent", {"known_information": ["需要首頁"]}),
            ("Creative Agent", {"proposals": ["互動式品牌故事"]}),
            ("Finance Agent", {"risks": ["預算可能不足"]}),
        )
        for current, (agent_name, output_data) in enumerate(outputs, start=1):
            await on_step(
                current,
                len(outputs),
                MeetingStepRecord(
                    agent_name=agent_name,
                    order=current - 1,
                    status=MeetingStepStatus.COMPLETED,
                    input_text=requirement,
                    output_data=output_data,
                    execution_time_seconds=float(current),
                ),
            )
        return SimpleNamespace(status="completed")

    async def _run_fake_second_round(
        self,
        guild_id: int,
        requirement_change: RequirementChange,
        *,
        on_step,
    ) -> SimpleNamespace:
        """模擬需求變更後四位 Agent 各回應一次。"""

        outputs = (
            ("PM Agent", {"goal": "縮減範圍"}),
            ("Research Agent", {"items_to_verify": ["確認必要功能"]}),
            ("Creative Agent", {"proposals": ["精簡版方案"]}),
            ("Finance Agent", {"alternatives": ["分階段交付"]}),
        )
        for current, (agent_name, output_data) in enumerate(outputs, start=1):
            await on_step(
                current,
                len(outputs),
                MeetingStepRecord(
                    agent_name=agent_name,
                    order=current + 3,
                    round_number=2,
                    status=MeetingStepStatus.COMPLETED,
                    input_text=requirement_change.description,
                    output_data=output_data,
                    execution_time_seconds=float(current),
                ),
            )
        return SimpleNamespace(status="completed")

    async def asyncTearDown(self) -> None:
        # 關閉 Bot 內部資源，避免不同測試互相影響。
        await self.bot.close()

    async def test_hello_mentions_user(self) -> None:
        """!hello 應該標記使用者並向他打招呼。"""

        ctx = SimpleNamespace(
            author=SimpleNamespace(mention="@Jayce"),
            send=AsyncMock(),
        )

        # get_command 找到 !hello；callback 直接執行裝飾器包住的原始函式。
        await self.bot.get_command("hello").callback(ctx)

        ctx.send.assert_awaited_once_with("你好，@Jayce！")  # 驗證只傳送過一次正確訊息。

    async def test_slash_hello_defers_then_sends_ephemeral_followup(self) -> None:
        """/hello 應該先 defer，再以私密 Followup 向使用者打招呼。"""

        interaction = SimpleNamespace(
            user=SimpleNamespace(mention="@Jayce"),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        # Slash Command 保存在 bot.tree，callback 可直接執行指令函式。
        await self.bot.tree.get_command("hello").callback(interaction)

        interaction.response.defer.assert_awaited_once_with(
            thinking=True,
            ephemeral=True,
        )
        interaction.followup.send.assert_awaited_once_with(
            "你好，@Jayce！",
            ephemeral=True,
        )

    async def test_projects_reads_repository_and_sends_ephemeral_embed(self) -> None:
        """/projects 應該將 Repository 的專案顯示為私密 Embed。"""

        self.fake_project_repository.list_projects.return_value = [
            Project(
                id="PRJ-001",
                category="Discord Bot",
                title="AI 客服機器人",
                requirements=("使用 Qwen 回答問題",),
                budget=120000,
                deadline=date(2026, 11, 30),
                acceptance_criteria=("Slash Command 可正常使用",),
                status=ProjectStatus.PENDING,
            )
        ]
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await self.bot.tree.get_command("projects").callback(interaction)

        self.fake_project_repository.list_projects.assert_called_once_with()
        send_call = interaction.response.send_message.await_args
        embed = send_call.kwargs["embed"]
        self.assertTrue(send_call.kwargs["ephemeral"])
        self.assertEqual(embed.title, "📁 專案清單")
        self.assertEqual(embed.description, "目前共有 1 個專案。")
        self.assertEqual(embed.fields[0].name, "PRJ-001｜AI 客服機器人")
        self.assertIn("NT$ 120,000", embed.fields[0].value)

    async def test_start_displays_complete_first_round_in_order(self) -> None:
        """/start 應公開依序顯示四位 Agent 的第一輪發言。"""

        self.fake_meeting_service.start_project.return_value = Project(
            id="PRJ-001",
            category="企業網站",
            title="咖啡店品牌官網",
            requirements=("製作首頁",),
            budget=80000,
            deadline=date(2026, 10, 15),
            acceptance_criteria=("手機版可正常瀏覽",),
            status=ProjectStatus.IN_PROGRESS,
        )
        interaction = SimpleNamespace(
            guild_id=123456,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await self.bot.tree.get_command("start").callback(
            interaction,
            project_id="PRJ-001",
        )

        interaction.response.defer.assert_awaited_once_with(
            thinking=True,
        )
        self.fake_meeting_service.start_project.assert_awaited_once_with(
            123456,
            "PRJ-001",
        )
        discussion_call = self.fake_discussion_manager.start_first_round.await_args
        self.assertEqual(
            discussion_call.args,
            (123456, "PRJ-001", "製作首頁"),
        )
        self.assertTrue(callable(discussion_call.kwargs["on_step"]))

        send_calls = interaction.followup.send.await_args_list
        self.assertEqual(send_calls[0].kwargs["embed"].title, "✅ 專案會議已啟動")
        displayed_text = [call.args[0] for call in send_calls[1:5]]
        self.assertIn("1/4 PM 需求拆解", displayed_text[0])
        self.assertIn("（1.00 秒）", displayed_text[0])
        self.assertIn("2/4 Research 研究觀點", displayed_text[1])
        self.assertIn("3/4 Creative 創意提案", displayed_text[2])
        self.assertIn("4/4 Finance 財務評估", displayed_text[3])
        self.assertEqual(send_calls[5].args[0], "✅ 第一輪討論完成。")

    async def test_change_displays_complete_second_round_in_order(self) -> None:
        """/change 應套用需求變更並公開顯示四位 Agent 的第二輪回應。"""

        change = RequirementChange(
            id="CHG-001",
            project_id="PRJ-001",
            description="預算縮減 20%",
            reason="客戶調整預算",
            requested_at=date(2026, 9, 17),
            status="待評估",
        )
        self.fake_project_repository.list_projects.return_value = [
            Project(
                id="PRJ-001",
                category="企業網站",
                title="咖啡店品牌官網",
                requirements=("製作首頁",),
                budget=80000,
                deadline=date(2026, 10, 15),
                acceptance_criteria=("手機版可正常瀏覽",),
                status=ProjectStatus.IN_PROGRESS,
                requirement_changes=(change,),
            )
        ]
        interaction = SimpleNamespace(
            guild_id=123456,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await self.bot.tree.get_command("change").callback(
            interaction,
            change_id="chg-001",
        )

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        second_round_call = self.fake_discussion_manager.start_second_round.await_args
        self.assertEqual(second_round_call.args, (123456, change))
        self.assertTrue(callable(second_round_call.kwargs["on_step"]))
        sent_text = [call.args[0] for call in interaction.followup.send.await_args_list]
        self.assertIn("套用需求變更 CHG-001", sent_text[0])
        self.assertIn("第 2 輪 1/4 PM 需求拆解", sent_text[1])
        self.assertIn("（1.00 秒）", sent_text[1])
        self.assertIn("第 2 輪 2/4 Research 研究觀點", sent_text[2])
        self.assertIn("第 2 輪 3/4 Creative 創意提案", sent_text[3])
        self.assertIn("第 2 輪 4/4 Finance 財務評估", sent_text[4])
        self.assertEqual(sent_text[5], "✅ 第二輪討論完成。")

    async def test_draft_displays_pm_proposal_summary_and_decisions(self) -> None:
        """/draft 應保存整合結果並用 Embed 顯示摘要與決策統計。"""

        interaction = SimpleNamespace(
            guild_id=123456,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await self.bot.tree.get_command("draft").callback(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        self.fake_discussion_manager.create_proposal_draft.assert_awaited_once_with(
            123456
        )
        embed = interaction.followup.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "📄 AI 客服提案")
        self.assertIn("核心問答功能", embed.description)
        self.assertEqual(embed.fields[0].value, "採用 1｜拒絕 0｜折衷 1")
        self.assertIn("輸入 5,100/6,000 字元（85.0%）", embed.footer.text)
        self.assertIn("輸出 3,900/4,000 字元（97.5%）", embed.footer.text)
        self.assertIn("Prompt Token 1,246", embed.footer.text)
        self.assertIn("輸出 Token 1,520/1,600（95.0%）", embed.footer.text)
        self.assertIn("處理耗時 72.35 秒", embed.footer.text)

    async def test_draft_failure_displays_safe_reason_and_elapsed_time(self) -> None:
        """/draft 失敗時應顯示安全原因與已等待時間。"""

        self.fake_discussion_manager.create_proposal_draft.side_effect = (
            MeetingManagerError("Agent 等待模型回覆逾時。")
        )
        interaction = SimpleNamespace(
            guild_id=123456,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await self.bot.tree.get_command("draft").callback(interaction)

        message = interaction.followup.send.await_args.args[0]
        self.assertIn("Agent 等待模型回覆逾時", message)
        self.assertIn("處理耗時", message)

    async def test_review_displays_rejection_revision_and_final_proposal(self) -> None:
        """/review 顯示四項審查、指定修改人與 PM 最終方案。"""

        result = self.fake_discussion_manager.review_and_finalize.return_value
        result.review["quality_evaluation"] = evaluate_review(result.review, "growth")
        final_review = {
            "status": "通過",
            "checklist": {
                key: {"score": 4, "reason": "修改後符合要求"}
                for key in ("completeness", "creativity", "credibility", "feasibility")
            },
            "issues": [],
        }
        final_review["quality_evaluation"] = evaluate_review(final_review, "growth")
        result.review["post_revision_review"] = final_review

        interaction = SimpleNamespace(
            guild_id=123456,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await self.bot.tree.get_command("review").callback(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        self.fake_discussion_manager.review_and_finalize.assert_awaited_once_with(
            123456
        )
        calls = interaction.followup.send.await_args_list
        review_embed = calls[0].kwargs["embed"]
        self.assertIn("需要修改", review_embed.title)
        self.assertIn("草案", review_embed.title)
        self.assertIn("完整度", review_embed.description)
        self.assertIn("Creative Agent", next(
            field.value for field in review_embed.fields if field.name == "修改要求"
        ))
        self.assertIn("草案品質指標", [field.name for field in review_embed.fields])
        self.assertIn("修改次數已達 1/1", calls[1].args[0])
        final_review_embed = calls[2].kwargs["embed"]
        self.assertIn("最終方案", final_review_embed.title)
        final_score = next(field for field in final_review_embed.fields if field.name == "最終方案品質指標")
        self.assertIn("80.0/100", final_score.value)
        final_embed = calls[3].kwargs["embed"]
        self.assertEqual(final_embed.title, "📄 AI 客服最終提案")

    async def test_review_embed_displays_quality_index_and_explanation(self) -> None:
        review = {
            "status": "通過",
            "checklist": {
                "completeness": {"score": 4, "reason": "完整"},
                "creativity": {"score": 5, "reason": "有差異化"},
                "credibility": {"score": 3, "reason": "來源待補強"},
                "feasibility": {"score": 4, "reason": "可執行"},
            },
            "issues": [],
        }
        review["quality_evaluation"] = evaluate_review(review, "innovation")

        embed = format_review_embed(review)

        self.assertIn("完整度 4/5", embed.description)
        quality = next(field for field in embed.fields if field.name == "整體品質指標")
        self.assertIn("創新", quality.value)
        self.assertIn("86.0/100", quality.value)
        explanation = next(field for field in embed.fields if field.name == "計算說明與改善建議")
        self.assertIn("可信度", explanation.value)

    async def test_meeting_runs_full_workflow_and_displays_final_proposal(self) -> None:
        """/meeting 應以單一指令串接完整流程並顯示最終方案。"""

        change = RequirementChange(
            id="CHG-001",
            project_id="PRJ-001",
            description="預算縮減 20%",
            reason="客戶調整預算",
            requested_at=date(2026, 9, 17),
            status="待評估",
        )
        project = Project(
            id="PRJ-001",
            category="企業網站",
            title="咖啡店品牌官網",
            requirements=("製作首頁",),
            budget=80000,
            deadline=date(2026, 10, 15),
            acceptance_criteria=("手機版可正常瀏覽",),
            status=ProjectStatus.IN_PROGRESS,
            requirement_changes=(change,),
        )
        self.fake_project_repository.list_projects.return_value = [project]
        self.fake_meeting_service.prepare_project = AsyncMock(return_value=project)
        self.fake_discussion_manager.run_full_meeting = AsyncMock(
            return_value=SimpleNamespace(
                record=SimpleNamespace(meeting_id="meeting-21"),
                review={
                    "status": "通過",
                    "checklist": {
                        "completeness": {"score": 4, "reason": "完整"},
                        "creativity": {"score": 4, "reason": "具體"},
                        "credibility": {"score": 4, "reason": "可信"},
                        "feasibility": {"score": 4, "reason": "可行"},
                    },
                    "issues": [],
                },
                revision_performed=False,
                revision_agent_name=None,
                final_proposal={
                    "title": "AI 客服最終提案",
                    "summary": "完整流程已完成。",
                    "decisions": [],
                },
            )
        )
        interaction = SimpleNamespace(
            guild_id=123456,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await self.bot.tree.get_command("meeting").callback(
            interaction,
            project_id="prj-001",
            change_id="chg-001",
        )

        self.fake_meeting_service.prepare_project.assert_awaited_once_with(
            123456, "PRJ-001"
        )
        call = self.fake_discussion_manager.run_full_meeting.await_args
        self.assertEqual(call.args[:4], (123456, "PRJ-001", "製作首頁", change))
        self.assertTrue(callable(call.kwargs["on_step"]))
        calls = interaction.followup.send.await_args_list
        self.assertIn("開始完整會議", calls[0].args[0])
        self.assertIn("通過", calls[1].kwargs["embed"].title)
        self.assertEqual(calls[2].kwargs["embed"].title, "📄 AI 客服最終提案")

    def test_proposal_embed_limits_decision_field_for_discord(self) -> None:
        """草案決策再多也不可超過 Discord Embed 欄位上限。"""

        proposal = {
            "title": "提案",
            "summary": "摘要",
            "decisions": [
                {
                    "topic": "議題" * 100,
                    "decision": "採用",
                    "reason": "原因" * 200,
                }
                for _ in range(8)
            ],
        }

        embed = format_proposal_embed(proposal)

        self.assertLessEqual(len(embed.fields[1].value), 1024)

    def test_discussion_messages_split_without_losing_long_output(self) -> None:
        """完整內容超過上限時分段，每段都不得突破 Discord 限制。"""

        messages = format_discussion_messages(
            "Creative Agent",
            3,
            4,
            {"proposals": ["重點" * 1200]},
            max_length=1900,
            execution_time_seconds=12.345,
        )

        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 1900 for message in messages))
        self.assertEqual(sum(message.count("重") for message in messages), 1200)
        self.assertEqual(sum(message.count("點") for message in messages), 1200)
        self.assertTrue(all("12.35 秒" in message for message in messages))

    def test_discussion_messages_show_usage_and_limit_warnings(self) -> None:
        """Discord 首段顯示字元、Token 統計及 80%／95% 警示。"""

        messages = format_discussion_messages(
            "Finance Agent",
            4,
            4,
            {"risks": ["風險"]},
            max_length=1900,
            input_characters=5100,
            output_characters=3900,
            max_prompt_characters=6000,
            max_response_characters=4000,
            prompt_tokens=1246,
            completion_tokens=960,
            max_output_tokens=1000,
        )

        self.assertIn("⚠️ 輸入 5,100/6,000 字元（85.0%）", messages[0])
        self.assertIn("🚨 輸出 3,900/4,000 字元（97.5%）", messages[0])
        self.assertIn("Prompt Token 1,246", messages[0])
        self.assertIn("🚨 輸出 Token 960/1,000（96.0%）", messages[0])

    async def test_start_rejects_direct_message(self) -> None:
        """私訊沒有 Guild ID，因此不能建立 Guild 專案會議。"""

        interaction = SimpleNamespace(
            guild_id=None,
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await self.bot.tree.get_command("start").callback(
            interaction,
            project_id="PRJ-001",
        )

        interaction.response.send_message.assert_awaited_once_with(
            "❌ /start 只能在 Discord 伺服器中使用。",
            ephemeral=True,
        )
        self.fake_meeting_service.start_project.assert_not_awaited()

    async def test_ask_shows_processing_then_returns_answer(self) -> None:
        """!ask 應該先顯示處理中，再編輯為模型回答。"""

        processing_message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(send=AsyncMock(return_value=processing_message))
        # 指定假 chat() 被等待後要回傳的模型內容。
        self.fake_service.chat.return_value = SimpleNamespace(content="Qwen 的回答")

        await self.bot.get_command("ask").callback(
            ctx,
            question="什麼是 Discord Bot？",
        )

        ctx.send.assert_awaited_once_with("⏳ Qwen 正在處理你的問題，請稍候……")
        self.fake_service.chat.assert_awaited_once_with("什麼是 Discord Bot？")  # 問題有交給服務。
        processing_message.edit.assert_awaited_once_with(content="Qwen 的回答")

    async def test_ask_rejects_question_over_input_limit(self) -> None:
        """超過 500 字的問題應該被拒絕，而且不能呼叫模型。"""

        ctx = SimpleNamespace(send=AsyncMock())

        await self.bot.get_command("ask").callback(
            ctx,
            question="問" * (self.settings.max_ask_input_length + 1),
        )

        ctx.send.assert_awaited_once_with("問題過長，請限制在 500 字以內。")
        self.fake_service.chat.assert_not_awaited()  # 超長問題不可送進模型。

    async def test_ask_truncates_long_answer(self) -> None:
        """模型回答超過 1900 字時應該附上截斷提示。"""

        processing_message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(send=AsyncMock(return_value=processing_message))
        self.fake_service.chat.return_value = SimpleNamespace(content="答" * 2500)

        await self.bot.get_command("ask").callback(ctx, question="請回答")

        # 從 edit(content=...) 的呼叫紀錄取出實際送給 Discord 的內容。
        edited_content = processing_message.edit.await_args.kwargs["content"]
        self.assertEqual(len(edited_content), self.settings.max_ask_output_length)
        self.assertTrue(edited_content.endswith(TRUNCATION_SUFFIX))

    async def test_ask_displays_ollama_not_running_error(self) -> None:
        """Ollama 未啟動時應該顯示可理解的連線錯誤。"""

        processing_message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(send=AsyncMock(return_value=processing_message))
        self.fake_service.chat.side_effect = LLMServiceError(
            "無法連線到 Ollama，請確認服務是否已啟動。"
        )

        # 捕捉 WARNING 日誌，避免測試畫面出現預期中的錯誤訊息。
        with self.assertLogs("bot", level="WARNING") as captured_logs:
            await self.bot.get_command("ask").callback(ctx, question="請回答")

        processing_message.edit.assert_awaited_once_with(
            content="❌ 無法連線到 Ollama，請確認服務是否已啟動。"
        )
        self.assertNotIn(
            self.settings.discord_token,
            "\n".join(captured_logs.output),
        )

    async def test_ask_displays_timeout_error(self) -> None:
        """Ollama 回應逾時時應該把逾時原因顯示在 Discord。"""

        processing_message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(send=AsyncMock(return_value=processing_message))
        self.fake_service.chat.side_effect = LLMServiceError(
            "Ollama 回應逾時，請稍後再試。"
        )

        with self.assertLogs("bot", level="WARNING") as captured_logs:
            await self.bot.get_command("ask").callback(ctx, question="請回答")

        processing_message.edit.assert_awaited_once_with(
            content="❌ Ollama 回應逾時，請稍後再試。"
        )
        self.assertNotIn(
            self.settings.discord_token,
            "\n".join(captured_logs.output),
        )


if __name__ == "__main__":
    unittest.main()
