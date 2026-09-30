"""Day 25 的可驗證選項與方案影響。"""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=180)]
Section = Literal["background_and_goal", "integrated_solution", "execution_plan", "risks_and_responses", "acceptance_criteria"]

class DecisionSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_index: Annotated[int, Field(strict=True, ge=0)]
    quote: Text

class DecisionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_id: Literal["A", "B", "C"]
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    description: Text
    benefits: Text
    tradeoffs: Text
    expected_changes: list[Section] = Field(min_length=1, max_length=5)

class DecisionQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: Text
    why_user_decision_needed: Text
    sources: list[DecisionSource] = Field(min_length=1, max_length=3)
    options: list[DecisionOption] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def unique_options(self):
        if [o.option_id for o in self.options] != list("ABC"[:len(self.options)]):
            raise ValueError("選項必須依序為 A、B、C，且不可重複。")
        if len({o.description for o in self.options}) != len(self.options):
            raise ValueError("選項內容不可重複。")
        return self

class DecisionImpact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: Section
    reason: Text
