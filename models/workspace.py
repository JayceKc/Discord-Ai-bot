"""Guild workspace settings and stable project priority values."""

from dataclasses import dataclass
from enum import Enum


class ProjectPriority(str, Enum):
    GROWTH = "growth"
    COST_EFFICIENCY = "cost_efficiency"
    BRAND_IMPACT = "brand_impact"
    INNOVATION = "innovation"

    @property
    def label(self) -> str:
        return {
            self.GROWTH: "成長",
            self.COST_EFFICIENCY: "成本效益",
            self.BRAND_IMPACT: "品牌影響",
            self.INNOVATION: "創新",
        }[self]

    @property
    def guidance(self) -> str:
        return {
            self.GROWTH: "優先評估觸及、使用者成長與可擴展性。",
            self.COST_EFFICIENCY: "優先評估預算、投入產出與維運成本。",
            self.BRAND_IMPACT: "優先評估品牌一致性、信任與對外形象。",
            self.INNOVATION: "優先評估差異化、新方法與可驗證的創新。",
        }[self]


@dataclass(frozen=True)
class Workspace:
    guild_id: int
    name: str
    default_priority: ProjectPriority
    completed_custom_projects: int = 0
    projects_with_final_proposal: int = 0
    current_project_id: str | None = None
