from __future__ import annotations

from dataclasses import dataclass, field

from .config import MantaConfig


@dataclass(frozen=True)
class RoleSpec:
    name: str
    purpose: str
    model: str
    tools: tuple[str, ...] = field(default_factory=tuple)
    skills: tuple[str, ...] = field(default_factory=tuple)
    can_write: bool = False
    shell: str = "denied"
    max_budget_usd: float = 0.0


def default_roles(config: MantaConfig) -> dict[str, RoleSpec]:
    return {
        "router": RoleSpec(
            name="router",
            purpose="Classify intent, risk, route, pipeline, and budget.",
            model=config.models.router,
            tools=(),
            max_budget_usd=0.01,
        ),
        "cheap_responder": RoleSpec(
            name="cheap_responder",
            purpose="Answer simple questions cheaply.",
            model=config.models.cheap_responder,
            tools=("read_file",),
            max_budget_usd=0.02,
        ),
        "planner": RoleSpec(
            name="planner",
            purpose="Create task plan, acceptance criteria, context manifest, and risk notes.",
            model=config.models.planner,
            tools=("read_file", "grep", "glob"),
            skills=("repo-map",),
            max_budget_usd=2.0,
        ),
        "builder": RoleSpec(
            name="builder",
            purpose="Implement focused code changes using patch and allowlisted tests.",
            model=config.models.builder,
            tools=("read_file", "apply_patch", "shell", "git_diff"),
            skills=("implement-change", "test-runner"),
            can_write=True,
            shell="allowlisted",
            max_budget_usd=1.5,
        ),
        "code_reviewer": RoleSpec(
            name="code_reviewer",
            purpose="Review diff for correctness, maintainability, and tests.",
            model=config.models.reviewer,
            tools=("read_file", "git_diff"),
            skills=("code-review",),
            max_budget_usd=1.0,
        ),
        "security_reviewer": RoleSpec(
            name="security_reviewer",
            purpose="Review diff for security risks.",
            model=config.models.security_reviewer,
            tools=("read_file", "git_diff", "dependency_scan", "secrets_scan"),
            skills=("security-review",),
            max_budget_usd=1.0,
        ),
        "release": RoleSpec(
            name="release",
            purpose="Create summary, commit message, and PR body.",
            model=config.models.release,
            tools=("git_diff",),
            skills=("release-notes",),
            max_budget_usd=0.1,
        ),
    }
