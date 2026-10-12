"""穩定的 Agent 顯示身分；不依賴模型輸出。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentProfile:
    agent_name: str
    display_name: str
    emoji: str
    color: int


PROFILES = {
    "PM Agent": AgentProfile("PM Agent", "PM｜專案經理", "📋", 0x3498DB),
    "Research Agent": AgentProfile("Research Agent", "Research｜研究分析", "🔎", 0x1ABC9C),
    "Creative Agent": AgentProfile("Creative Agent", "Creative｜創意企劃", "💡", 0x9B59B6),
    "Finance Agent": AgentProfile("Finance Agent", "Finance｜財務評估", "💰", 0xE67E22),
    "Review Agent": AgentProfile("Review Agent", "Review｜品質審查", "✅", 0x2ECC71),
}
SYSTEM = AgentProfile("System", "系統通知", "🤖", 0x95A5A6)


def get_agent_profile(agent_name: str) -> AgentProfile:
    return PROFILES.get(agent_name, SYSTEM)
