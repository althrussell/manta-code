from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class Intent(StrEnum):
    QUESTION = "question"
    CODE_CHANGE = "code_change"
    REVIEW = "review"
    SECURITY_REVIEW = "security_review"
    PLAN = "plan"
    UNKNOWN = "unknown"


class Complexity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RouteName(StrEnum):
    SIMPLE_ANSWER = "simple_answer"
    TRIVIAL_CODE_CHANGE = "trivial_code_change"
    NORMAL_CODE_CHANGE = "normal_code_change"
    COMPLEX_ARCHITECTURE = "complex_architecture"
    SECURITY_SENSITIVE = "security_sensitive"


class RouteDecision(BaseModel):
    intent: Intent
    complexity: Complexity
    risk: Risk
    route: RouteName
    needs_planning: bool = False
    needs_review: bool = False
    needs_security_review: bool = False
    pipeline: list[str]
    max_budget_usd: float = Field(ge=0)
    reason: str


class ModelPrice(BaseModel):
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)
    context_window: int = Field(gt=0)


class TokenUsage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class CostRecord(BaseModel):
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: str
    role: str
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0)
    route: str
    reason: str = ""


class SessionEvent(BaseModel):
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ContextManifest(BaseModel):
    session_id: str
    route: str
    repo_root: str
    selected_files: list[str] = Field(default_factory=list)
    excluded_paths: list[str] = Field(default_factory=list)
    role_token_estimates: dict[str, int] = Field(default_factory=dict)
    selection_reason: str = ""


class ToolRequest(BaseModel):
    tool: str
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    cwd: str | None = None


class ToolDecision(BaseModel):
    decision: Literal["allow", "approval_required", "block"]
    reason: str


class ReviewFinding(BaseModel):
    file: str | None = None
    line: int | None = None
    severity: Literal["low", "medium", "high"]
    category: str
    issue: str
    required_fix: str


class ReviewReport(BaseModel):
    approved: bool
    findings: list[ReviewFinding] = Field(default_factory=list)


class RoleResult(BaseModel):
    role: str
    status: Literal["completed", "blocked", "failed", "skipped"]
    output: dict[str, Any] = Field(default_factory=dict)
    cost: float = 0


def new_session_id(prefix: str = "manta") -> str:
    return f"{prefix}-{uuid4().hex[:12]}"
