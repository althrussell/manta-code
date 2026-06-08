"""Deep Agents runtime adapter.

This is the *only* module allowed to import Deep Agents. Everything else in
Manta talks to the :class:`~manta_cli.agents.base.AgentRuntime` protocol, so the
product is free to swap Deep Agents for LangGraph or a custom runtime later
(see ``docs/13-deepagents-integration.md`` and ADR 0001).

The Deep Agents import is intentionally lazy (inside :meth:`run_role`) so that
constructing the runtime, importing this module, and running the test-suite do
not require the optional ``[agent]`` extra to be installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from manta_cli.roles import RoleSpec
from manta_cli.schemas import ContextManifest, ReviewReport, RoleResult, TokenUsage

from .tools import build_tools

# Roles that must return a structured ReviewReport and stay read-only.
REVIEWER_ROLES = {"code_reviewer", "security_reviewer"}

# Map role names to their prompt file stems where they differ.
PROMPT_ALIASES = {"release": "release_agent"}


class DeepAgentsRuntime:
    """Adapter that runs a single Manta role as a Deep Agents agent."""

    def __init__(self, root: Path | None = None, prompts_dir: Path | None = None) -> None:
        self.root = (root or Path.cwd()).resolve()
        self.prompts_dir = prompts_dir or (Path(__file__).resolve().parents[3] / "prompts" / "roles")

    def _system_prompt(self, role: RoleSpec) -> str:
        stem = PROMPT_ALIASES.get(role.name, role.name)
        prompt_file = self.prompts_dir / f"{stem}.md"
        if prompt_file.is_file():
            return prompt_file.read_text(encoding="utf-8")
        return f"You are Manta's {role.name} agent. {role.purpose}"

    def _build_user_message(self, role: RoleSpec, prompt: str, context: ContextManifest) -> str:
        lines = [f"Task: {prompt}", "", f"Route: {context.route}", "Selected context files:"]
        lines.extend(f"- {f}" for f in context.selected_files)
        if role.name in REVIEWER_ROLES:
            lines += [
                "",
                "Review the current diff against the task and acceptance criteria.",
                "Return a structured ReviewReport (approved + findings).",
            ]
        return "\n".join(lines)

    def run_role(self, role: RoleSpec, prompt: str, context: ContextManifest) -> RoleResult:
        try:
            from deepagents import create_deep_agent
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "Deep Agents is not installed. Install with: pip install -e '.[agent]'"
            ) from exc

        is_reviewer = role.name in REVIEWER_ROLES
        tools = build_tools(role, root=self.root)
        kwargs: dict[str, Any] = {
            "model": role.model,
            "system_prompt": self._system_prompt(role),
            "tools": tools,
        }
        if is_reviewer:
            kwargs["response_format"] = ReviewReport

        agent = create_deep_agent(**kwargs)
        result = agent.invoke(
            {"messages": [{"role": "user", "content": self._build_user_message(role, prompt, context)}]}
        )

        usage = _aggregate_usage(result)
        if is_reviewer:
            report = _coerce_review_report(result)
            return RoleResult(
                role=role.name,
                status="completed" if report.approved else "blocked",
                output={"review": report.model_dump(mode="json")},
                usage=usage,
            )
        return RoleResult(
            role=role.name,
            status="completed",
            output={"message": _final_text(result)},
            usage=usage,
        )


def _messages(result: Any) -> list[Any]:
    if isinstance(result, dict):
        return list(result.get("messages", []))
    return list(getattr(result, "messages", []) or [])


def _msg_attr(message: Any, key: str) -> Any:
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def _aggregate_usage(result: Any) -> TokenUsage:
    input_tokens = 0
    output_tokens = 0
    for message in _messages(result):
        meta = _msg_attr(message, "usage_metadata")
        if isinstance(meta, dict):
            input_tokens += int(meta.get("input_tokens", 0) or 0)
            output_tokens += int(meta.get("output_tokens", 0) or 0)
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)


def _final_text(result: Any) -> str:
    messages = _messages(result)
    if not messages:
        return ""
    content = _msg_attr(messages[-1], "content")
    if isinstance(content, list):  # content blocks
        parts = [block.get("text", "") if isinstance(block, dict) else str(block) for block in content]
        return "".join(parts)
    return str(content or "")


def _coerce_review_report(result: Any) -> ReviewReport:
    structured = result.get("structured_response") if isinstance(result, dict) else None
    if isinstance(structured, ReviewReport):
        return structured
    if isinstance(structured, dict):
        return ReviewReport.model_validate(structured)
    # Fall back to parsing the final message as JSON; default to a blocking,
    # finding-free report if the model returned unstructured text.
    import json

    try:
        return ReviewReport.model_validate(json.loads(_final_text(result)))
    except (json.JSONDecodeError, ValueError):
        return ReviewReport(approved=False, findings=[])
